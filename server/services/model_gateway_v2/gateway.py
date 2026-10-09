"""供应商无关的模型网关（Agent 1 / WP-03）。

职责：
1. **归一化**——把 provider 抛出的任何异常翻译成统一的 ``StreamError`` 条目
   （携带封闭的 ``error_class``），或让取消信号原样穿透；
2. **分类兜底**——provider 未提供分类器时，用 :func:`classify_default` 兜底；
3. **不做重试**。

为什么重试不放在网关里：计划书 §4.3 要求 TurnRuntime 负责「重试、压缩、中断和完成」。
重试需要写入 ``model/retry_scheduled`` 事件、需要与压缩联动、需要遵守取消信号——
这些都只有运行时掌握。网关只做一次「分类 + 翻译」，避免两层各自重试导致放大。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from contracts.agent_v2.cancellation import CancelledError, CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ErrorClass, ModelStreamKind
from contracts.agent_v2.errors import ModelStreamError
from contracts.agent_v2.fake import ModelProvider
from contracts.agent_v2.models import ModelRequest, StreamError, StreamItem


@dataclass
class RetryPolicy:
    """重试策略（由运行时使用）。"""

    max_attempts: int = 3
    base_delay_s: float = 0.0
    max_delay_s: float = 0.0
    #: 可重试的最大迭代次数（同一次模型请求内）
    def delay_for(self, attempt: int) -> float:
        if self.base_delay_s <= 0:
            return 0.0
        delay = self.base_delay_s * (2 ** max(0, attempt - 1))
        if self.max_delay_s:
            delay = min(delay, self.max_delay_s)
        return delay


def classify_default(exc: BaseException) -> ErrorClass:
    """兜底异常分类。

    真实适配器应自带更精确的分类器（例如按 HTTP 状态码区分 429 / 400 / 401）。
    这里只处理与语言运行时相关的通用情形，且**不包含任何供应商特有判定**。
    """
    if isinstance(exc, asyncio.CancelledError):
        return ErrorClass.FATAL  # 调用方应先处理取消，走到这里说明不该被分类
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return ErrorClass.RETRYABLE
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return ErrorClass.INVALID_REQUEST
    return ErrorClass.FATAL


class ModelGateway:
    """统一模型网关。"""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        classifier: Callable[[BaseException], ErrorClass] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.provider = provider
        self.classifier = classifier or classify_default
        self.clock = clock or SystemClock()

    # ---- 便于测试与日志 ----
    @property
    def call_count(self) -> int:
        return int(getattr(self.provider, "call_count", 0))

    @property
    def calls(self) -> list[ModelRequest]:
        return list(getattr(self.provider, "calls", []))

    async def stream(
        self, request: ModelRequest, cancel: CancelToken | None = None
    ) -> AsyncIterator[StreamItem]:
        """产出统一流条目。

        - provider 抛出的异常被翻译为 ``StreamError`` 条目（错误分类落在条目里）；
        - 取消信号（:class:`CancelledError` / ``asyncio.CancelledError``）**原样抛出**，
          交由运行时决定「中断而不成功」。
        """
        try:
            iterator = self.provider.stream(request, cancel)
            async for item in iterator:
                if cancel is not None and cancel.cancelled:
                    raise CancelledError(cancel.reason or "cancelled")
                if not isinstance(item, StreamItem):
                    yield StreamError(
                        error_class=ErrorClass.FATAL.value,
                        message=f"provider yielded non-StreamItem: {type(item).__name__}",
                    )
                    return
                yield item
        except (CancelledError, asyncio.CancelledError):
            raise
        except GeneratorExit:
            # 消费方提前退出（aclose）时必须原样穿透，绝不能被归一化逻辑吞掉，
            # 否则异步生成器会忽略 GeneratorExit 并留下未回收的任务。
            raise
        except ModelStreamError as exc:
            yield StreamError(
                error_class=str(exc.error_class),
                message=str(exc),
                retry_after_s=float(getattr(exc, "retry_after_s", 0.0)),
            )
        except Exception as exc:  # noqa: BLE001 - 归一化是网关的核心职责
            error_class = self.classifier(exc)
            yield StreamError(
                error_class=error_class.value if hasattr(error_class, "value") else str(error_class),
                message=f"{type(exc).__name__}: {exc}",
            )


def error_class_of(item: StreamItem) -> ErrorClass | None:
    """若条目是错误流，返回其错误分类。"""
    if not isinstance(item, StreamError):
        return None
    value = item.error_class.value if hasattr(item.error_class, "value") else str(item.error_class)
    try:
        return ErrorClass(value)
    except ValueError:
        return ErrorClass.FATAL


def is_error(item: StreamItem) -> bool:
    return getattr(item, "kind", None) == ModelStreamKind.ERROR


__all__ = [
    "ModelGateway",
    "RetryPolicy",
    "classify_default",
    "error_class_of",
    "is_error",
]
