"""契约层的最小 fake 实现与协议声明。

计划书 §5 要求契约包提供「Python 类型和最小 fake 实现」。这里的 fake
只服务于契约与运行时测试：

- :class:`ModelProvider` / :class:`ToolExecutor` 是**接口协议**，
  Agent 2（工具入口）与真实供应商适配器分别实现它们；
- :class:`FakeProvider` / :class:`FakeToolExecutor` 提供可脚本化的假实现，
  让 TurnRuntime 的测试完全离线、无网络、无真实模型。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .cancellation import CancelledError, CancelToken
from .enums import ErrorClass, StopReason, ToolCallStatus, ToolKind
from .errors import ModelStreamError
from .models import (
    ModelRequest,
    ReasoningDelta,
    StreamCompleted,
    StreamError,
    StreamItem,
    TextDelta,
    ToolCall,
    ToolCallCompleted,
    ToolCallDelta,
    ToolResult,
    ToolSpec,
    Usage,
)


# --------------------------------------------------------------------------------------
# 接口协议
# --------------------------------------------------------------------------------------
@runtime_checkable
class ModelProvider(Protocol):
    """模型供应商适配器接口。

    供应商差异（鉴权、报文格式、异常类型）必须收敛在实现内部：
    实现负责把自家异常翻译成 :class:`StreamError`（携带 ``error_class``），
    或抛出 :class:`ModelStreamError`。TurnRuntime 不得出现供应商分支。
    """

    def stream(
        self, request: ModelRequest, cancel: CancelToken | None = None
    ) -> AsyncIterator[StreamItem]: ...


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行器接口（Agent 1 不实现具体工具）。"""

    def specs(self) -> Sequence[ToolSpec]: ...

    def spec(self, name: str) -> ToolSpec | None: ...

    async def execute(
        self, call: ToolCall, cancel: CancelToken | None = None
    ) -> ToolResult: ...


# --------------------------------------------------------------------------------------
# 流条目构造小工具
# --------------------------------------------------------------------------------------
def text_response(text: str, *, stop_reason: str = StopReason.END_TURN) -> list[StreamItem]:
    """构造「纯文本」应答流。"""
    return [TextDelta(text), StreamCompleted(stop_reason)]


def reasoning_response(
    reasoning: str, text: str, *, stop_reason: str = StopReason.END_TURN
) -> list[StreamItem]:
    """构造「先推理再回答」的流。"""
    return [
        ReasoningDelta(reasoning),
        TextDelta(text),
        StreamCompleted(stop_reason),
    ]


def tool_call_response(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
    text_prefix: str | None = None,
    stop_reason: str = StopReason.TOOL_USE,
    stream_deltas: bool = True,
) -> list[StreamItem]:
    """构造「一次工具调用」的流（可选先输出一段文本）。"""
    items: list[StreamItem] = []
    if text_prefix:
        items.append(TextDelta(text_prefix))
    if stream_deltas:
        # 模拟增量拼接：分两段下发参数，验证运行时的增量累积
        raw = json.dumps(arguments, ensure_ascii=False)
        mid = max(1, len(raw) // 2)
        items.append(ToolCallDelta(call_id=call_id, name=name, arguments_delta=raw[:mid]))
        items.append(ToolCallDelta(call_id=call_id, name=None, arguments_delta=raw[mid:]))
    items.append(ToolCallCompleted(call_id=call_id, name=name, arguments=dict(arguments)))
    items.append(StreamCompleted(stop_reason))
    return items


def error_response(
    error_class: str,
    message: str = "synthetic failure",
    *,
    retry_after_s: float = 0.0,
) -> list[StreamItem]:
    """构造「错误流」。"""
    return [StreamError(error_class=error_class, message=message, retry_after_s=retry_after_s)]


def usage_item(
    input_tokens: int = 10, output_tokens: int = 5, *, cost_usd: float | None = None
) -> StreamItem:
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd)


def assert_error_class(value: Any, *, field: str = "error_class") -> str:
    """校验错误分类合法，返回其字符串值。"""
    cls = value.value if hasattr(value, "value") else str(value)
    if cls not in {m.value for m in ErrorClass}:
        raise ValueError(f"{field}: {value!r} is not a valid ErrorClass")
    return cls


# --------------------------------------------------------------------------------------
# Fake 实现
# --------------------------------------------------------------------------------------
@dataclass
class FakeProvider:
    """可脚本化的假供应商。

    :param script: 每次 ``stream()`` 调用依次消耗一个「条目列表」；
        用尽后重复最后一项（便于「压缩后继续十轮」这类测试）。
    :param delay_s: 每个条目之间插入的等待，用于制造可中断的时间窗。
    :param raise_on_exhausted: 脚本用尽且为 True 时抛错，而不是重复最后一项。
    """

    script: list[list[StreamItem]] = field(default_factory=list)
    delay_s: float = 0.0
    raise_on_exhausted: bool = False
    calls: list[ModelRequest] = field(default_factory=list)
    #: 每次调用是否在 yield 前检查取消令牌
    honor_cancel: bool = True

    def queue(self, items: Sequence[StreamItem]) -> FakeProvider:
        self.script.append(list(items))
        return self

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def stream(
        self, request: ModelRequest, cancel: CancelToken | None = None
    ) -> AsyncIterator[StreamItem]:
        self.calls.append(request)
        if not self.script:
            raise ModelStreamError(ErrorClass.FATAL.value, "fake provider script is empty")
        if len(self.calls) <= len(self.script):
            items = self.script[len(self.calls) - 1]
        elif self.raise_on_exhausted:
            raise ModelStreamError(ErrorClass.FATAL.value, "fake provider script exhausted")
        else:
            items = self.script[-1]
        batch_id = len(self.script)
        for item in items:
            if self.honor_cancel and cancel is not None:
                cancel.raise_if_cancelled()
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            else:
                await asyncio.sleep(0)
            yield item
        del batch_id


@dataclass
class FakeToolExecutor:
    """可脚本化的假工具执行器。

    :param specs_list: 工具声明；``kind`` 决定并行/串行调度。
    :param results: ``name -> (status, output)``；未命中时返回成功空输出。
    :param delay_s: 执行耗时，用于观测并行性。
    :param fail_names: 直接抛出异常的工具名（模拟执行器崩溃）。
    """

    specs_list: list[ToolSpec] = field(default_factory=list)
    results: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    delay_s: float = 0.0
    fail_names: set[str] = field(default_factory=set)
    calls: list[ToolCall] = field(default_factory=list)
    #: 记录并发峰值（>1 说明发生了并行）
    max_concurrency: int = 0
    _active: int = 0

    def __post_init__(self) -> None:
        if not self.specs_list:
            self.specs_list = [
                ToolSpec(name="read_file", kind=ToolKind.READ_ONLY, description="读取文件"),
                ToolSpec(name="list_dir", kind=ToolKind.READ_ONLY, description="列目录"),
                ToolSpec(name="write_file", kind=ToolKind.SIDE_EFFECT, description="写文件"),
                ToolSpec(name="run_command", kind=ToolKind.SIDE_EFFECT, description="执行命令"),
            ]

    def specs(self) -> Sequence[ToolSpec]:
        return list(self.specs_list)

    def spec(self, name: str) -> ToolSpec | None:
        for s in self.specs_list:
            if s.name == name:
                return s
        return None

    def calls_for(self, name: str) -> list[ToolCall]:
        return [c for c in self.calls if c.name == name]

    async def execute(
        self, call: ToolCall, cancel: CancelToken | None = None
    ) -> ToolResult:
        self._active += 1
        self.max_concurrency = max(self.max_concurrency, self._active)
        try:
            if call.name in self.fail_names:
                raise RuntimeError(f"tool {call.name} exploded")
            if cancel is not None and cancel.cancelled:
                return ToolResult(
                    call_id=call.call_id,
                    name=call.name,
                    status=ToolCallStatus.CANCELLED,
                    error={"code": "cancelled", "message": "cancelled before execution"},
                )
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            else:
                await asyncio.sleep(0)
            if cancel is not None:
                cancel.raise_if_cancelled()
            self.calls.append(call)
            status, output = self.results.get(call.name, (ToolCallStatus.SUCCEEDED.value, {}))
            status_enum = ToolCallStatus(status)
            if status_enum is ToolCallStatus.SUCCEEDED:
                return ToolResult(
                    call_id=call.call_id, name=call.name, status=status_enum, output=dict(output)
                )
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=status_enum,
                error={"code": status_enum.value, "message": f"synthetic {status_enum.value}"},
            )
        except CancelledError:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.CANCELLED,
                error={"code": "cancelled", "message": "cancelled during execution"},
            )
        # 注意：**不吞 asyncio.CancelledError**。协程必须如实向上传播异步取消，
        # 否则 asyncio.wait_for 的超时语义会被破坏（超时会被误判成 cancelled）。
        finally:
            self._active -= 1


__all__ = [
    "ModelProvider",
    "ToolExecutor",
    "FakeProvider",
    "FakeToolExecutor",
    "text_response",
    "reasoning_response",
    "tool_call_response",
    "error_response",
    "usage_item",
    "assert_error_class",
]
