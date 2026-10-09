# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""子 Agent 工具的**参数校验**（Agent 2 / WP-06）。

校验与入口分开，便于：
- 单独测试"非法参数在执行前被拒绝"；
- 集成方按同一套规则校验来自其他通道（如 Agent 3 的控制请求）的参数。
"""

from __future__ import annotations

from typing import Any

#: 单个字段的长度上限（防止把整篇论文塞进消息里撑爆事件）
MAX_NAME_LEN = 120
MAX_CONTENT_LEN = 20_000


def _require(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return f"缺少必填字段 {key}"
    if not isinstance(value, str):
        return f"{key} 必须是字符串"
    if not value.strip():
        return f"{key} 不能为空白"
    return None


def validate_spawn(arguments: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("name", "task"):
        if (problem := _require(arguments, key)) is not None:
            problems.append(problem)
    name = arguments.get("name")
    if isinstance(name, str) and len(name) > MAX_NAME_LEN:
        problems.append(f"name 超过 {MAX_NAME_LEN} 字符")
    task = arguments.get("task")
    if isinstance(task, str) and len(task) > MAX_CONTENT_LEN:
        problems.append(f"task 超过 {MAX_CONTENT_LEN} 字符")
    return problems


def validate_send(arguments: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key in ("agent_id", "content"):
        if (problem := _require(arguments, key)) is not None:
            problems.append(problem)
    content = arguments.get("content")
    if isinstance(content, str) and len(content) > MAX_CONTENT_LEN:
        problems.append(f"content 超过 {MAX_CONTENT_LEN} 字符")
    return problems


def validate_wait(arguments: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    ids = arguments.get("agent_ids")
    if not isinstance(ids, list) or not ids:
        problems.append("agent_ids 必须是非空数组")
    elif any(not isinstance(item, str) or not item for item in ids):
        problems.append("agent_ids 的每一项都必须是非空字符串")
    timeout = arguments.get("timeout_s")
    if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
        problems.append("timeout_s 必须是正数")
    return problems


def validate_agent_ref(arguments: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if (problem := _require(arguments, "agent_id")) is not None:
        problems.append(problem)
    return problems


#: 工具名 → 校验器
VALIDATORS: dict[str, Any] = {
    "agent.spawn": validate_spawn,
    "agent.send": validate_send,
    "agent.wait": validate_wait,
    "agent.interrupt": validate_agent_ref,
    "agent.close": validate_agent_ref,
}


def validate(tool: str, arguments: dict[str, Any]) -> list[str]:
    """按工具名校验，未知工具返回明确错误。"""
    validator = VALIDATORS.get(tool)
    if validator is None:
        return [f"未知的子 Agent 工具 {tool!r}"]
    return validator(dict(arguments or {}))


def is_own_child(tree: Any, *, parent_id: str, agent_id: str) -> bool:
    """目标是否是本线程的子 Agent（越权检查，工具与校验共用）。"""
    try:
        return tree.parent_of(agent_id) == parent_id or tree.is_ancestor(parent_id, agent_id)
    except Exception:  # noqa: BLE001 - 线程不存在等一律视为非本线程子 Agent
        return False


__all__ = [
    "MAX_NAME_LEN",
    "MAX_CONTENT_LEN",
    "VALIDATORS",
    "validate",
    "validate_spawn",
    "validate_send",
    "validate_wait",
    "validate_agent_ref",
    "is_own_child",
]
