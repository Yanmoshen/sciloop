"""取消令牌：运行时、模型流与工具执行共享的取消信号。

设计约束（验收书 §8）：
- 所有异步任务可取消；
- 取消后不得遗留活动 Turn 锁；
- 测试进程结束后不得有后台任务泄漏。

因此令牌是**可订阅**的：工具执行器可以注册回调，在收到取消信号时
向真实进程发送终止指令（Agent 1 不实现该逻辑，只提供信号）。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from typing import Any

from .errors import AgentV2Error


class CancelledError(AgentV2Error):
    """取消信号触发的异常。``asyncio.CancelledError`` 之外的显式取消类型。"""

    code = "cancelled"


class CancelToken:
    """线程安全的取消令牌。

    - :meth:`cancel` 幂等，重复调用只记录首个原因；
    - :meth:`subscribe` 注册的回调在取消时立即执行（包括取消后才注册的，
      会立刻被调用一次，避免"竞态窗口内注册"导致收不到信号）；
    - :meth:`raise_if_cancelled` 供运行循环在每个 await 点前调用。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancelled = False
        self._reason: str | None = None
        self._callbacks: list[Callable[[str], None]] = []

    # ---- 状态 ----
    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    # ---- 操作 ----
    def cancel(self, reason: str = "cancelled") -> bool:
        """发出取消信号。返回是否由本次调用触发状态翻转。"""
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = reason
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for cb in callbacks:
            # 回调失败不得影响取消传播
            with contextlib.suppress(Exception):
                cb(reason)
        return True

    def subscribe(self, callback: Callable[[str], None]) -> Callable[[], None]:
        """注册取消回调，返回反注册函数。"""
        with self._lock:
            if self._cancelled:
                fire_now = True
                reason = self._reason or "cancelled"
            else:
                fire_now = False
                self._callbacks.append(callback)
                reason = ""
        if fire_now:
            with contextlib.suppress(Exception):
                callback(reason)
            return lambda: None

        def unsubscribe() -> None:
            with self._lock, contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return unsubscribe

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._cancelled:
                raise CancelledError(self._reason or "cancelled")

    def to_dict(self) -> dict[str, Any]:
        return {"cancelled": self.cancelled, "reason": self.reason}


def never_cancelled() -> CancelToken:
    """返回一个永远不取消的令牌（默认参数用，避免可变默认值共享）。"""
    return CancelToken()


__all__ = ["CancelToken", "CancelledError", "never_cancelled"]
