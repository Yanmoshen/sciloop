# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SSE 事件总线（WP09-T7）。

职责
----
1. 提供进程内**按 project 路由**的事件总线：``publish(project_id, event, payload)``
2. 订阅 :data:`app.llm.events.default_bus`，把 WP02 的 ``llm_fallback`` / ``demo_mode`` /
   ``llm_error`` / ``replay_miss`` 转发为 SSE（WP02 的契约请求）
3. 事件名与载荷**严格对齐** ``contracts.sse_events``（缺字段时打 warning，不阻塞推送）
4. 提供全局单调 ``id`` + 环形缓冲，便于前端断线后按 ``Last-Event-ID`` 补齐
   （仅补齐事件流；**状态一律用 ``/status`` / ``/stages`` 补齐**，不依赖事件回放）

禁止 WebSocket（contracts.stack.explicitly_banned）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("sciloop.pipeline.sse")

WP_ID = "WP09"

#: ``contracts.sse_events`` 的事件名 → 必需载荷字段
CONTRACT_EVENTS: dict[str, tuple[str, ...]] = {
    "stage_progress": ("stage", "percent", "message"),
    "stage_done": ("stage",),
    "decision": ("decision_point", "chosen", "rationale"),
    "risk_policy": ("decision_id", "risk", "confidence", "reversibility", "action"),
    "passport_ready": ("passport_id", "status", "is_replay"),
    "review_calibrated": ("sample_size", "metric", "value"),
    "guardrail": ("type", "used_usd", "limit_usd", "quota_usd", "ok"),
    "need_human": ("node", "payload"),
    "circuit_break": ("reason", "report_url"),
    "demo_mode": ("snapshot", "replay"),
}

#: WP02 事件总线 → SSE 的转发名（超出 contracts 的**附加**事件，仅作审计展示）
LLM_FORWARD_EVENTS: tuple[str, ...] = ("llm_fallback", "llm_error", "replay_miss")

BUFFER_SIZE = 512
KEEPALIVE_SECONDS = 15.0


@dataclass
class SSEEvent:
    """一条待推送事件。"""

    id: int
    event: str
    payload: dict[str, Any]
    project_id: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event": self.event,
            "payload": self.payload,
            "project_id": self.project_id,
            "created_at": self.created_at,
        }

    def format(self) -> str:
        """SSE 帧（``id:`` + ``event:`` + ``data:``）。"""
        data = json.dumps(
            {"event": self.event, "payload": self.payload, "id": self.id, "created_at": self.created_at},
            ensure_ascii=False,
            default=str,
        )
        return f"id: {self.id}\nevent: {self.event}\ndata: {data}\n\n"


class EventHub:
    """按 project 路由的进程内事件总线（多 worker 时每个 worker 各自持有）。"""

    def __init__(self, buffer_size: int = BUFFER_SIZE) -> None:
        self._subscribers: dict[int | None, set[asyncio.Queue[SSEEvent]]] = {}
        self._buffer: deque[SSEEvent] = deque(maxlen=buffer_size)
        self._counter = 0

    # ---------------- 发布 ----------------
    def publish(
        self, event: str, payload: dict[str, Any] | None = None, *, project_id: int | None = None
    ) -> SSEEvent:
        """发布事件（同步安全，可在任意上下文调用）。

        ``project_id is None`` 表示广播（例如 WP02 的 ``demo_mode`` 事件不带项目）。
        """
        data = dict(payload or {})
        self._warn_missing_fields(event, data)
        self._counter += 1
        item = SSEEvent(id=self._counter, event=event, payload=data, project_id=project_id)
        self._buffer.append(item)
        targets: list[asyncio.Queue[SSEEvent]] = []
        if project_id is None:
            for subscribers in self._subscribers.values():
                targets.extend(subscribers)
        else:
            targets.extend(self._subscribers.get(project_id, set()))
            targets.extend(self._subscribers.get(None, set()))
        for queue in targets:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - 慢消费者
                logger.warning("SSE 队列已满，丢弃事件 event=%s project_id=%s", event, project_id)
        return item

    async def publish_async(
        self, event: str, payload: dict[str, Any] | None = None, *, project_id: int | None = None
    ) -> SSEEvent:
        return self.publish(event, payload, project_id=project_id)

    # ---------------- 订阅 ----------------
    def subscribe(self, project_id: int | None) -> asyncio.Queue[SSEEvent]:
        queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=1000)
        self._subscribers.setdefault(project_id, set()).add(queue)
        logger.debug("SSE 订阅建立 project_id=%s subscribers=%d", project_id, self.subscriber_count())
        return queue

    def unsubscribe(self, project_id: int | None, queue: asyncio.Queue[SSEEvent]) -> None:
        subscribers = self._subscribers.get(project_id)
        if subscribers and queue in subscribers:
            subscribers.discard(queue)
        if subscribers is not None and not subscribers:
            self._subscribers.pop(project_id, None)

    def subscriber_count(self, project_id: int | None = None) -> int:
        if project_id is None:
            return sum(len(items) for items in self._subscribers.values())
        return len(self._subscribers.get(project_id, set()))

    # ---------------- 回放 ----------------
    def buffer(self, project_id: int | None = None, limit: int = 100) -> list[SSEEvent]:
        items = [
            item
            for item in self._buffer
            if project_id is None or item.project_id in (None, project_id)
        ]
        return items[-limit:]

    def events_since(self, last_id: int, project_id: int | None = None, limit: int = 200) -> list[SSEEvent]:
        items = [
            item
            for item in self._buffer
            if item.id > int(last_id) and (project_id is None or item.project_id in (None, project_id))
        ]
        return items[:limit]

    # ---------------- 契约校验 ----------------
    def _warn_missing_fields(self, event: str, payload: dict[str, Any]) -> None:
        required = CONTRACT_EVENTS.get(event)
        if required is None:
            if event not in LLM_FORWARD_EVENTS:
                logger.warning("SSE 事件名不在 contracts.sse_events 中（附加事件）event=%s", event)
            return
        missing = [key for key in required if key not in payload]
        if missing:
            logger.warning(
                "SSE 事件载荷缺少契约字段 event=%s missing=%s payload_keys=%s",
                event,
                missing,
                sorted(payload),
            )


hub = EventHub()


# --------------------------------------------------------------------------- #
# 便捷发布函数（engine / 其他工作包共用）
# --------------------------------------------------------------------------- #
def publish(event: str, payload: dict[str, Any] | None = None, *, project_id: int | None = None) -> SSEEvent:
    """发布一条 SSE 事件（同步）。"""
    return hub.publish(event, payload, project_id=project_id)


async def publish_event(
    project_id: int | None, event: str, payload: dict[str, Any] | None = None
) -> SSEEvent:
    """异步发布（供环节 ``ctx.emit`` 使用）。"""
    return await hub.publish_async(event, payload, project_id=project_id)


def publish_guardrail(project_id: int, snapshot: dict[str, Any]) -> SSEEvent:
    """发布 ``guardrail`` 事件（只取契约字段，附加字段丢弃）。"""
    payload = {key: snapshot.get(key) for key in CONTRACT_EVENTS["guardrail"]}
    return publish("guardrail", payload, project_id=project_id)


def publish_risk_policy(project_id: int, decision: dict[str, Any]) -> SSEEvent:
    """发布 ``risk_policy`` 事件（``{decision_id,risk,confidence,reversibility,action}``）。

    ``risk`` / ``confidence`` / ``reversibility`` 在风险策略未挂载（stub）时为 ``null``，
    并附带 ``policy_version`` / ``degraded`` 便于审计（**不编造分数**）。
    """
    payload: dict[str, Any] = {
        "decision_id": decision.get("decision_log_id") or decision.get("decision_id"),
        "risk": decision.get("risk_score"),
        "confidence": decision.get("confidence_score"),
        "reversibility": decision.get("reversibility_score"),
        "action": decision.get("policy_action"),
    }
    for extra in ("policy_version", "degraded", "decision_point", "reason"):
        if extra in decision:
            payload[extra] = decision.get(extra)
    return publish("risk_policy", payload, project_id=project_id)


def format_event(item: SSEEvent) -> str:
    return item.format()


# --------------------------------------------------------------------------- #
# WP02 事件总线转发（llm_fallback / demo_mode / llm_error / replay_miss）
# --------------------------------------------------------------------------- #
_FORWARDER_INSTALLED = False


def install_llm_forwarder() -> bool:
    """订阅 ``app.llm.events.default_bus`` 并把事件转发为 SSE（幂等）。"""
    global _FORWARDER_INSTALLED
    if _FORWARDER_INSTALLED:
        return False
    try:
        from app.llm.events import default_bus
    except Exception:  # pragma: no cover - WP02 缺失（不应发生）
        logger.warning("无法导入 app.llm.events.default_bus，LLM 事件转发未启用")
        return False
    default_bus.subscribe(_forward_llm_event)
    _FORWARDER_INSTALLED = True
    logger.info("已订阅 app.llm.events.default_bus 并转发为 SSE（llm_fallback/demo_mode/llm_error/replay_miss）")
    return True


async def _forward_llm_event(event_type: str, payload: dict[str, Any]) -> None:
    project_id = payload.get("project_id") if isinstance(payload, dict) else None
    if not isinstance(project_id, int):
        project_id = None  # 广播（demo_mode 事件不带 project_id）
    try:
        normalized = dict(payload or {})
    except Exception:  # pragma: no cover
        normalized = {}
    if event_type == "demo_mode":
        # 载荷严格对齐 contracts.sse_events.demo_mode = {snapshot, replay}
        publish(
            "demo_mode",
            {"snapshot": bool(normalized.get("snapshot")), "replay": bool(normalized.get("replay"))},
            project_id=project_id,
        )
        return
    if event_type in LLM_FORWARD_EVENTS:
        publish(event_type, normalized, project_id=project_id)
        return
    logger.debug("未转发的 LLM 事件 event=%s", event_type)


install_llm_forwarder()


__all__ = [
    "BUFFER_SIZE",
    "CONTRACT_EVENTS",
    "KEEPALIVE_SECONDS",
    "LLM_FORWARD_EVENTS",
    "EventHub",
    "SSEEvent",
    "format_event",
    "hub",
    "install_llm_forwarder",
    "publish",
    "publish_event",
    "publish_guardrail",
    "publish_risk_policy",
]
