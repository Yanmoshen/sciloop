# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""工具事件 → Agent 1 事件（Agent 2 / WP-04）。

验收书 §2 要求「工具开始、输出、完成、失败和取消均可转换为 Agent 1 事件」。
Agent 1 的事件模型是契约里的 :class:`Event`（``tool/started`` / ``tool/output`` /
``tool/completed`` …），因此这里提供纯函数做转换——**不自己落盘**，
落盘由 Agent 1 的 ``ThreadRepository`` 负责（职责不重叠）。
"""

from __future__ import annotations

from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import EventType, ToolCallStatus
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import Event, ToolCall, ToolResult

#: 工具状态 → 事件类型（与 Agent 1 运行时使用的映射保持一致）
EVENT_TYPE_BY_STATUS: dict[ToolCallStatus, EventType] = {
    ToolCallStatus.REQUESTED: EventType.TOOL_STARTED,
    ToolCallStatus.RUNNING: EventType.TOOL_STARTED,
    ToolCallStatus.SUCCEEDED: EventType.TOOL_COMPLETED,
    ToolCallStatus.FAILED: EventType.TOOL_FAILED,
    ToolCallStatus.TIMEOUT: EventType.TOOL_TIMEOUT,
    ToolCallStatus.CANCELLED: EventType.TOOL_CANCELLED,
    ToolCallStatus.INVALID_ARGUMENTS: EventType.TOOL_INVALID_ARGUMENTS,
}


def event_type_for(status: ToolCallStatus | str) -> EventType:
    """工具状态 → 事件类型。"""
    return EVENT_TYPE_BY_STATUS[ToolCallStatus(status)]


def started_event(
    *,
    sequence: int,
    thread_id: str,
    call: ToolCall,
    turn_id: str | None = None,
    item_id: str | None = None,
    clock: Clock | None = None,
) -> Event:
    """``tool/started``。"""
    moment = (clock or SystemClock()).now_iso()
    return Event(
        event_id=new_id("event"),
        sequence=sequence,
        type=EventType.TOOL_STARTED.value,
        created_at=moment,
        thread_id=thread_id,
        turn_id=turn_id or call.turn_id,
        item_id=item_id or call.item_id,
        call_id=call.call_id,
        payload={
            "tool": call.name,
            "kind": call.kind.value,
            "arguments": dict(call.arguments),
            "attempt": call.attempt,
        },
    )


def output_event(
    *,
    sequence: int,
    thread_id: str,
    call: ToolCall,
    channel: str,
    text: str,
    truncated: bool = False,
    turn_id: str | None = None,
    clock: Clock | None = None,
) -> Event:
    """``tool/output``：增量输出（宿主命令的 stdout / stderr 分块）。"""
    moment = (clock or SystemClock()).now_iso()
    return Event(
        event_id=new_id("event"),
        sequence=sequence,
        type=EventType.TOOL_OUTPUT.value,
        created_at=moment,
        thread_id=thread_id,
        turn_id=turn_id or call.turn_id,
        call_id=call.call_id,
        payload={"channel": channel, "text": text, "truncated": truncated},
    )


def result_event(
    *,
    sequence: int,
    thread_id: str,
    call: ToolCall,
    result: ToolResult,
    turn_id: str | None = None,
    item_id: str | None = None,
    clock: Clock | None = None,
) -> Event:
    """按结果状态生成 ``tool/completed|failed|timeout|cancelled|invalid_arguments``。"""
    moment = (clock or SystemClock()).now_iso()
    return Event(
        event_id=new_id("event"),
        sequence=sequence,
        type=event_type_for(result.status).value,
        created_at=moment,
        thread_id=thread_id,
        turn_id=turn_id or call.turn_id,
        item_id=item_id or call.item_id,
        call_id=call.call_id,
        payload={
            "tool": result.name,
            "status": result.status.value,
            "output": result.output,
            "error": result.error,
            "duration_ms": result.duration_ms,
            "result_call_id": result.call_id,
        },
    )


def lifecycle_events(
    *,
    start_sequence: int,
    thread_id: str,
    call: ToolCall,
    result: ToolResult,
    outputs: list[dict[str, Any]] | None = None,
    clock: Clock | None = None,
) -> list[Event]:
    """一次完整调用的「开始 → 输出… → 结果」事件序列（序号连续递增）。"""
    events: list[Event] = [
        started_event(
            sequence=start_sequence, thread_id=thread_id, call=call, clock=clock
        )
    ]
    sequence = start_sequence + 1
    for chunk in outputs or []:
        events.append(
            output_event(
                sequence=sequence,
                thread_id=thread_id,
                call=call,
                channel=str(chunk.get("channel", "stdout")),
                text=str(chunk.get("text", "")),
                truncated=bool(chunk.get("truncated", False)),
                clock=clock,
            )
        )
        sequence += 1
    events.append(
        result_event(
            sequence=sequence, thread_id=thread_id, call=call, result=result, clock=clock
        )
    )
    return events


__all__ = [
    "EVENT_TYPE_BY_STATUS",
    "event_type_for",
    "started_event",
    "output_event",
    "result_event",
    "lifecycle_events",
]
