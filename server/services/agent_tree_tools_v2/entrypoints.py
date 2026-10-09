# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""子 Agent 工具入口（Agent 2 / WP-06）。

五个工具：``agent.spawn`` / ``agent.send`` / ``agent.wait`` / ``agent.interrupt`` / ``agent.close``。

只做两件事：**工具入口**与**参数校验**。真正的子 Agent 生命周期由 Agent 1 的
:class:`services.agent_threads_v2.tree.AgentTree` 完成：

- ``agent.spawn``  → ``AgentTree.create_child`` + 把任务投进子线程邮箱；
- ``agent.send``   → ``AgentTree.send_message``（写**收件线程**邮箱，不碰父历史）；
- ``agent.wait``   → ``AgentTree.wait_for``（子失败/中断**不会**抛异常）；
- ``agent.interrupt`` → ``AgentTree.interrupt_child``；
- ``agent.close``  → 先中断活动 Turn，再把子线程归档。

三条约束（文档 WP-06）：不直接写父历史、不伪造子 Agent 完成事件、工具失败只返回
``ToolResult``（不自动结束父 Turn）。
"""

from __future__ import annotations

from typing import Any

from services.tool_registry_v2.models import (
    IdempotencyMode,
    PermissionClass,
    SideEffect,
    ToolContext,
    ToolDefinition,
)
from services.tool_registry_v2.schemas import ANY_OBJECT, STRING_LIST_PROP, strict_object

from .validation import is_own_child, validate

#: 子 Agent 工具的审计字段
AGENT_AUDIT_FIELDS: tuple[str, ...] = ("agent_id", "name", "task", "content", "reason")


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"__error__": {"code": code, "message": message, **extra}}


def _require_tree(ctx: ToolContext) -> tuple[Any | None, dict[str, Any] | None]:
    tree = ctx.agent_tree
    if tree is None:
        return None, _error("backend_unavailable", "AgentTree 未接入")
    return tree, None


def _validate(tool: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    problems = validate(tool, arguments)
    if problems:
        return _error("invalid_arguments", "参数不符合子 Agent 工具契约", problems=problems)
    return None


# ---------------------------------------------------------------------------- 定义
def spawn_definition() -> ToolDefinition:
    return ToolDefinition(
        name="agent.spawn",
        permission=PermissionClass.EXEC,
        description="创建子 Agent（独立 Thread），并把任务投进它的邮箱",
        version="1.0.0",
        side_effect=SideEffect.EXTERNAL,
        timeout_ms=30_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=AGENT_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("name", "task"),
            properties={
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "task": {"type": "string", "minLength": 1},
                "cwd": {"type": "string"},
                "model": {"type": "string"},
                "settings": {"type": "object"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_spawn,
    )


def send_definition() -> ToolDefinition:
    return ToolDefinition(
        name="agent.send",
        permission=PermissionClass.EXEC,
        description="给子 Agent 发消息（投递到收件线程邮箱）",
        version="1.0.0",
        side_effect=SideEffect.EXTERNAL,
        timeout_ms=30_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=AGENT_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("agent_id", "content"),
            properties={
                "agent_id": {"type": "string", "minLength": 1},
                "content": {"type": "string", "minLength": 1},
                "kind": {"type": "string"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_send,
    )


def wait_definition() -> ToolDefinition:
    return ToolDefinition(
        name="agent.wait",
        permission=PermissionClass.EXEC,
        description="等待一个或多个子 Agent 收敛（失败/中断也如实返回，不抛错）",
        version="1.0.0",
        side_effect=SideEffect.EXTERNAL,
        timeout_ms=120_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=AGENT_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("agent_ids",),
            properties={
                "agent_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_wait,
    )


def interrupt_definition() -> ToolDefinition:
    return ToolDefinition(
        name="agent.interrupt",
        permission=PermissionClass.EXEC,
        description="中断子 Agent 的活动 Turn（不改变父 Agent 状态）",
        version="1.0.0",
        side_effect=SideEffect.EXTERNAL,
        timeout_ms=30_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=AGENT_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("agent_id",),
            properties={
                "agent_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_interrupt,
    )


def close_definition() -> ToolDefinition:
    return ToolDefinition(
        name="agent.close",
        permission=PermissionClass.EXEC,
        description="关闭子 Agent（先中断活动 Turn，再归档子线程）",
        version="1.0.0",
        side_effect=SideEffect.EXTERNAL,
        timeout_ms=30_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=AGENT_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("agent_id",),
            properties={
                "agent_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_close,
    )


# ---------------------------------------------------------------------------- 实现
async def run_spawn(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if (problem := _validate("agent.spawn", args)) is not None:
        return problem
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None

    child = tree.create_child(
        ctx.thread_id,
        str(args["name"]).strip(),
        settings=dict(args.get("settings") or {}) or None,
        cwd=args.get("cwd"),
        model=args.get("model"),
    )
    message = tree.send_message(ctx.thread_id, child.thread_id, str(args["task"]), kind="task")

    runner = ctx.service("agent_runner")
    status = "spawned"
    if runner is not None:
        try:
            scheduled = runner(child.thread_id, str(args["task"]))
            if hasattr(scheduled, "__await__"):
                scheduled = await scheduled
            status = "scheduled"
        except Exception as exc:  # noqa: BLE001 - 调度失败不拖垮父 Agent
            return {
                "agent_id": child.thread_id,
                "thread_id": child.thread_id,
                "parent_thread_id": ctx.thread_id,
                "name": child.name,
                "path": list(child.path),
                "status": "spawn_failed",
                "task_message": message,
                "__error__": {"code": "spawn_failed", "message": f"{type(exc).__name__}: {exc}"},
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


async def run_send(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if (problem := _validate("agent.send", args)) is not None:
        return problem
    tree, blocked = _require_tree(ctx)
    if blocked:
        return blocked
    assert tree is not None
    agent_id = str(args["agent_id"])
    if not tree.repo.exists(agent_id):
        return _error("agent_not_found", f"子 Agent {agent_id} 不存在")
    if not is_own_child(tree, parent_id=ctx.thread_id, agent_id=agent_id):
        return _error("not_my_child", f"{agent_id} 不是本线程的子 Agent")
    message = tree.send_message(
        ctx.thread_id, agent_id, str(args["content"]), kind=str(args.get("kind") or "message")
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


async def run_wait(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if (problem := _validate("agent.wait", args)) is not None:
        return problem
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


async def run_interrupt(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if (problem := _validate("agent.interrupt", args)) is not None:
        return problem
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


async def run_close(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if (problem := _validate("agent.close", args)) is not None:
        return problem
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
        spawn_definition(),
        send_definition(),
        wait_definition(),
        interrupt_definition(),
        close_definition(),
    ]


__all__ = [
    "agent_tree_tool_definitions",
    "spawn_definition",
    "send_definition",
    "wait_definition",
    "interrupt_definition",
    "close_definition",
    "run_spawn",
    "run_send",
    "run_wait",
    "run_interrupt",
    "run_close",
    "STRING_LIST_PROP",
]
