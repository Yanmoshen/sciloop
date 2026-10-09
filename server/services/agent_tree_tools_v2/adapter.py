# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""子 Agent 工具适配（Agent 2 / WP-05 §4.5）。

只做两件事：**工具入口**与**参数校验**。真正的子 Agent 生命周期由 Agent 1 的
:class:`services.agent_threads_v2.tree.AgentTree` 完成：

- ``spawn_agent``  → ``AgentTree.create_child`` + 把任务投进子线程邮箱；
- ``send_message`` → ``AgentTree.send_message``（写**收件线程**邮箱，不碰父历史）；
- ``wait_agent``   → ``AgentTree.wait_for``（子失败/中断**不会**抛异常）；
- ``interrupt_agent`` → ``AgentTree.interrupt_child``；
- ``close_agent``  → 先中断活动 Turn，再把子线程归档。

三条约束（验收书 §6）：参数符合契约、**不直接改父 Agent 历史**、结果带来源 ID。
"""

from __future__ import annotations

from typing import Any

from services.tool_registry_v2.definition import (
    IdempotencyMode,
    ToolCategory,
    ToolContext,
    ToolDefinition,
)

_OBJECT = {"type": "object"}


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"__error__": {"code": code, "message": message, **extra}}


def _tree(ctx: ToolContext) -> Any | None:
    return ctx.agent_tree


def _require_tree(ctx: ToolContext) -> tuple[Any | None, dict[str, Any] | None]:
    tree = _tree(ctx)
    if tree is None:
        return None, _error("backend_unavailable", "AgentTree 未接入")
    return tree, None


# ---------------------------------------------------------------------------- 定义
def spawn_agent_definition() -> ToolDefinition:
    return ToolDefinition(
        name="spawn_agent",
        category=ToolCategory.EXECUTION,
        description="创建子 Agent（独立 Thread），并把任务投进它的邮箱",
        timeout_s=30.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "task"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "task": {"type": "string", "minLength": 1},
                "cwd": {"type": "string"},
                "model": {"type": "string"},
                "settings": {"type": "object"},
            },
        },
        output_schema=_OBJECT,
        handler=run_spawn_agent,
    )


def send_message_definition() -> ToolDefinition:
    return ToolDefinition(
        name="send_message",
        category=ToolCategory.EXECUTION,
        description="给子 Agent 发消息（投递到收件线程邮箱）",
        timeout_s=30.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["agent_id", "content"],
            "properties": {
                "agent_id": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 1},
                "kind": {"type": "string"},
            },
        },
        output_schema=_OBJECT,
        handler=run_send_message,
    )


def wait_agent_definition() -> ToolDefinition:
    return ToolDefinition(
        name="wait_agent",
        category=ToolCategory.EXECUTION,
        description="等待一个或多个子 Agent 收敛（失败/中断也如实返回，不抛错）",
        timeout_s=120.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["agent_ids"],
            "properties": {
                "agent_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        output_schema=_OBJECT,
        handler=run_wait_agent,
    )


def interrupt_agent_definition() -> ToolDefinition:
    return ToolDefinition(
        name="interrupt_agent",
        category=ToolCategory.EXECUTION,
        description="中断子 Agent 的活动 Turn（不改变父 Agent 状态）",
        timeout_s=30.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string"},
            },
        },
        output_schema=_OBJECT,
        handler=run_interrupt_agent,
    )


def close_agent_definition() -> ToolDefinition:
    return ToolDefinition(
        name="close_agent",
        category=ToolCategory.EXECUTION,
        description="关闭子 Agent（先中断活动 Turn，再归档子线程）",
        timeout_s=30.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["agent_id"],
            "properties": {
                "agent_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string"},
            },
        },
        output_schema=_OBJECT,
        handler=run_close_agent,
    )


# ---------------------------------------------------------------------------- 实现
async def run_spawn_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None
    name = str(args["name"]).strip()
    task = str(args["task"])
    if not name:
        return _error("invalid_arguments", "name 不能为空")

    child = tree.create_child(
        ctx.thread_id,
        name,
        settings=dict(args.get("settings") or {}) or None,
        cwd=args.get("cwd"),
        model=args.get("model"),
    )
    # 任务投进**子线程**邮箱：父 Agent 的历史不因创建子 Agent 而被改写
    message = tree.send_message(ctx.thread_id, child.thread_id, task, kind="task")

    # 集成方可以注入 agent_runner(thread_id, task) 来真正驱动子 Agent；
    # 未注入时只登记任务（status=spawned），由上层决定何时推进。
    runner = ctx.service("agent_runner")
    status = "spawned"
    if runner is not None:
        try:
            scheduled = runner(child.thread_id, task)
            if hasattr(scheduled, "__await__"):
                scheduled = await scheduled
            status = "scheduled"
        except Exception as exc:  # noqa: BLE001 - 调度失败不拖垮父 Agent
            status = "spawn_failed"
            return {
                "agent_id": child.thread_id,
                "thread_id": child.thread_id,
                "parent_thread_id": ctx.thread_id,
                "name": child.name,
                "path": list(child.path),
                "status": status,
                "task_message": message,
                "__error__": {
                    "code": "spawn_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
    return {
        "agent_id": child.thread_id,
        "thread_id": child.thread_id,
        "parent_thread_id": ctx.thread_id,
        "name": child.name,
        "path": list(child.path),
        "status": status,
        "task_message": message,
    }


async def run_send_message(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None
    agent_id = str(args["agent_id"])
    if not tree.repo.exists(agent_id):
        return _error("agent_not_found", f"子 Agent {agent_id} 不存在")
    if not tree.is_ancestor(ctx.thread_id, agent_id) and tree.parent_of(agent_id) != ctx.thread_id:
        return _error(
            "not_my_child",
            f"{agent_id} 不是本线程的子 Agent（工具不得越权给别的 Agent 发消息）",
        )
    message = tree.send_message(
        ctx.thread_id,
        agent_id,
        str(args["content"]),
        kind=str(args.get("kind") or "message"),
    )
    return {
        "agent_id": agent_id,
        "from_thread_id": ctx.thread_id,
        "to_thread_id": agent_id,
        "sequence": message.get("sequence"),
        "created_at": message.get("created_at"),
        "content": message.get("content"),
        "kind": message.get("kind"),
    }


async def run_wait_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None
    agent_ids = [str(item) for item in args["agent_ids"]]
    missing = [item for item in agent_ids if not tree.repo.exists(item)]
    if missing:
        return _error("agent_not_found", f"子 Agent 不存在：{missing}", missing=missing)
    timeout = args.get("timeout_s")
    outcomes = await tree.wait_for(agent_ids, timeout_s=float(timeout) if timeout else None)
    results = {
        agent_id: {
            "agent_id": agent_id,
            "thread_id": agent_id,
            "status": outcome.status,
            "summary": outcome.summary,
            "result_item_id": outcome.result_item_id,
            "error": outcome.error,
            "parent_thread_id": tree.parent_of(agent_id),
        }
        for agent_id, outcome in outcomes.items()
    }
    return {
        "requested": agent_ids,
        "converged": sorted(item for item in results if results[item]["status"] != "timeout"),
        "timed_out": sorted(item for item in results if results[item]["status"] == "timeout"),
        "results": results,
    }


async def run_interrupt_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None
    agent_id = str(args["agent_id"])
    if not tree.repo.exists(agent_id):
        return _error("agent_not_found", f"子 Agent {agent_id} 不存在")
    reason = str(args.get("reason") or "parent_interrupt")
    tree.interrupt_child(agent_id, reason=reason)
    return {
        "agent_id": agent_id,
        "thread_id": agent_id,
        "parent_thread_id": tree.parent_of(agent_id),
        "status": tree.child_status(agent_id),
        "reason": reason,
    }


async def run_close_agent(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None
    repo = tree.repo
    agent_id = str(args["agent_id"])
    if not repo.exists(agent_id):
        return _error("agent_not_found", f"子 Agent {agent_id} 不存在")
    reason = str(args.get("reason") or "closed_by_parent")
    state = repo.state(agent_id)
    interrupted = False
    if state.active_turn is not None:
        tree.interrupt_child(agent_id, reason=reason)
        interrupted = True
    from contracts.agent_v2.enums import EventType

    repo.emit_event(
        agent_id,
        EventType.THREAD_ARCHIVED,
        payload={"reason": reason, "closed_by": ctx.thread_id},
    )
    return {
        "agent_id": agent_id,
        "thread_id": agent_id,
        "parent_thread_id": tree.parent_of(agent_id),
        "closed": True,
        "interrupted_active_turn": interrupted,
        "reason": reason,
    }


def agent_tree_tool_definitions() -> list[ToolDefinition]:
    return [
        spawn_agent_definition(),
        send_message_definition(),
        wait_agent_definition(),
        interrupt_agent_definition(),
        close_agent_definition(),
    ]


__all__ = [
    "agent_tree_tool_definitions",
    "spawn_agent_definition",
    "send_message_definition",
    "wait_agent_definition",
    "interrupt_agent_definition",
    "close_agent_definition",
    "run_spawn_agent",
    "run_send_message",
    "run_wait_agent",
    "run_interrupt_agent",
    "run_close_agent",
]
