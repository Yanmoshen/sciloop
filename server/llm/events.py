# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""LLM 层事件总线（进程内）。

WP02 只负责**广播**，不落库：

- ``llm_fallback``：主模型失败、已切换到备用模型 → **WP10 消费后补写 ``decision_logs``**
  （contracts 要求「主模型不可用时按配置顺序自动切换备用模型，并写 Decision Log」，
  但 ``decision_logs`` 的 owner 是 WP10，因此本包只发事件 + 在 ``llm_call_logs.error`` 留痕）
- ``demo_mode``：回放命中 → WP09 的 SSE 层转发为 ``demo_mode {snapshot, replay}``
- ``llm_error``：调用最终失败 → 供 WP17 失败报告使用

事件不会反向影响 LLM 调用结果：监听器抛错只记日志。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal

logger = logging.getLogger(__name__)

LLMEvent = Literal["llm_fallback", "demo_mode", "llm_error", "replay_miss"]

#: 监听器签名：``(event_type, payload)``，可返回 awaitable
Listener = Callable[[str, dict[str, Any]], "Awaitable[None] | None"]

_MAX_RECENT = 200


class EventBus:
    """极简异步事件总线（进程内，多 worker 部署时每个 worker 各自持有）。"""

    def __init__(self, max_recent: int = _MAX_RECENT) -> None:
        self._listeners: list[Listener] = []
        self._recent: deque[dict[str, Any]] = deque(maxlen=max_recent)

    def subscribe(self, listener: Listener) -> Listener:
        if listener not in self._listeners:
            self._listeners.append(listener)
        return listener

    def unsubscribe(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """广播事件；监听器异常被吞掉并记日志（绝不影响 LLM 主流程）。"""
        event = {"event": event_type, "payload": dict(payload or {})}
        self._recent.append(event)
        for listener in list(self._listeners):
            try:
                result = listener(event_type, event["payload"])
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001 - 监听器故障不得影响调用链
                logger.warning("LLM 事件监听器执行失败 event=%s", event_type, exc_info=True)
        return event

    def emit_nowait(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """在无事件循环的同步上下文中安全地广播（如 CLI 脚本）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._recent.append({"event": event_type, "payload": dict(payload or {})})
            return
        loop.create_task(self.emit(event_type, payload))

    def recent(self, limit: int = 50, event_type: str | None = None) -> list[dict[str, Any]]:
        items = [e for e in self._recent if event_type is None or e["event"] == event_type]
        return items[-limit:]

    def clear(self) -> None:
        self._recent.clear()


default_bus = EventBus()


def subscribe(listener: Listener, bus: EventBus | None = None) -> Listener:
    """注册监听器（WP09 的 SSE 层用这个）。"""
    return (bus or default_bus).subscribe(listener)


async def emit_event(
    event_type: str, payload: dict[str, Any] | None = None, bus: EventBus | None = None
) -> dict[str, Any]:
    return await (bus or default_bus).emit(event_type, payload)


async def emit_fallback(
    *,
    stage: str | None,
    from_ref: str,
    to_ref: str,
    reason: str,
    error_kind: str | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """发出 ``llm_fallback`` 降级事件（WP10 消费）。"""
    return await emit_event(
        "llm_fallback",
        {
            "stage": stage,
            "from_model_ref": from_ref,
            "to_model_ref": to_ref,
            "reason": reason,
            "error_kind": error_kind,
            "project_id": project_id,
        },
    )


async def emit_demo_mode(*, snapshot: bool, replay: bool, prompt_hash: str | None = None) -> dict[str, Any]:
    """发出 ``demo_mode`` 事件（SSE 载荷固定为 ``{snapshot, replay}``）。"""
    payload: dict[str, Any] = {"snapshot": snapshot, "replay": replay}
    if prompt_hash:
        payload["prompt_hash"] = prompt_hash
    return await emit_event("demo_mode", payload)


__all__ = [
    "EventBus",
    "LLMEvent",
    "Listener",
    "default_bus",
    "emit_demo_mode",
    "emit_event",
    "emit_fallback",
    "subscribe",
]
