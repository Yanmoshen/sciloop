# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""风险自适应状态机（WP09-T1）。

状态枚举与合法迁移严格取自 ``contracts.enums.project_status`` 与计划书 §2.3 状态图::

    DRAFT --> TASKBOOK_LOCKED --> RUNNING --> RISK_EVALUATING
    RISK_EVALUATING --> RUNNING | WAIT_HUMAN | CIRCUIT_BREAK
    WAIT_HUMAN --> RUNNING | ABORTED
    RUNNING --> REVIEWING
    REVIEWING --> RUNNING（轮次 +1，未达停止条件） | DONE（达停止条件）
    CIRCUIT_BREAK --> WAIT_HUMAN（产出失败报告后等待人工） | DONE（用户接受当前结果）
    DONE / ABORTED 为终态

硬约束：

- 非法迁移抛 :class:`IllegalTransition`（绝不静默纠正）
- 每次迁移都写**结构化日志**（JSON 行，含 wp_id / project_id / from / to / reason / actor）
- ``RISK_EVALUATING`` 只由风险策略驱动进入/退出（由 engine 调用，本模块只负责守卫）
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sciloop.pipeline.state_machine")

WP_ID = "WP09"

#: ``contracts.enums.project_status`` 的完整枚举（顺序即展示顺序）
PROJECT_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "TASKBOOK_LOCKED",
    "RUNNING",
    "RISK_EVALUATING",
    "WAIT_HUMAN",
    "CIRCUIT_BREAK",
    "REVIEWING",
    "DONE",
    "ABORTED",
)

#: 合法迁移表。计划书 §2.3 图中的边 + 运维必需边（stop/pause/熔断后的人工接受）：
#: - ``ABORTED`` 允许从任意非终态进入（``POST /pipelines/{pid}/stop`` 是任意时刻可用的急停）
#: - ``CIRCUIT_BREAK -> DONE`` 对应「用户接受当前结果」
TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"TASKBOOK_LOCKED", "ABORTED"}),
    "TASKBOOK_LOCKED": frozenset({"RUNNING", "ABORTED"}),
    "RUNNING": frozenset({"RISK_EVALUATING", "REVIEWING", "WAIT_HUMAN", "CIRCUIT_BREAK", "ABORTED"}),
    "RISK_EVALUATING": frozenset({"RUNNING", "WAIT_HUMAN", "CIRCUIT_BREAK", "ABORTED"}),
    "WAIT_HUMAN": frozenset({"RUNNING", "ABORTED", "CIRCUIT_BREAK", "DONE"}),
    "CIRCUIT_BREAK": frozenset({"WAIT_HUMAN", "DONE", "ABORTED"}),
    "REVIEWING": frozenset({"RUNNING", "WAIT_HUMAN", "CIRCUIT_BREAK", "DONE", "ABORTED"}),
    "DONE": frozenset(),
    "ABORTED": frozenset(),
}

TERMINAL_STATUSES: frozenset[str] = frozenset({"DONE", "ABORTED"})

#: 允许恢复继续跑的状态（``resume`` / ``intervene approve`` 的目标前提）
RESUMABLE_STATUSES: frozenset[str] = frozenset({"WAIT_HUMAN", "RUNNING"})

#: 运行中状态（可被 pause/stop 控制的状态集合）
ACTIVE_STATUSES: frozenset[str] = frozenset({"RUNNING", "RISK_EVALUATING", "REVIEWING"})


class IllegalTransition(RuntimeError):
    """非法状态迁移。

    ``code`` 固定为 ``illegal_state_transition``，供 API 层直接映射为契约错误体
    ``{code,message,detail}``。
    """

    code = "illegal_state_transition"

    def __init__(self, current: str, target: str, *, project_id: int | None = None, reason: str = "") -> None:
        self.current = current
        self.target = target
        self.project_id = project_id
        self.reason = reason
        allowed = sorted(TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"非法状态迁移：{current} -> {target}"
            f"（project_id={project_id}，允许的目标={allowed or '无（终态）'}，原因={reason or '未提供'}）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "detail": {
                "project_id": self.project_id,
                "from": self.current,
                "to": self.target,
                "allowed": sorted(TRANSITIONS.get(self.current, frozenset())),
                "reason": self.reason,
            },
        }


def structured_log(event: str, **fields: Any) -> None:
    """写一条结构化日志（JSON 行，便于 ``grep`` 与日志采集）。

    契约要求日志含 ``wp_id`` / ``stage`` / ``cost`` 字段（code_style.logging），
    故这里统一补 ``wp_id`` 并把其余字段原样序列化。
    """
    payload: dict[str, Any] = {"event": event, "wp_id": WP_ID}
    payload.update({key: value for key, value in fields.items() if value is not None})
    logger.info(json.dumps(payload, ensure_ascii=False, default=str))


def is_known_status(status: str) -> bool:
    return status in PROJECT_STATUSES


def can_transition(current: str, target: str) -> bool:
    """``current -> target`` 是否合法（未知状态一律 False）。"""
    if not is_known_status(current) or not is_known_status(target):
        return False
    return target in TRANSITIONS[current]


def assert_transition(
    current: str,
    target: str,
    *,
    project_id: int | None = None,
    reason: str = "",
) -> None:
    """非法迁移抛 :class:`IllegalTransition` 并记结构化日志（warn 级，含被拒详情）。"""
    if can_transition(current, target):
        return
    structured_log(
        "state_transition_rejected",
        project_id=project_id,
        from_status=current,
        to_status=target,
        reason=reason,
        allowed=sorted(TRANSITIONS.get(current, frozenset())),
    )
    raise IllegalTransition(current, target, project_id=project_id, reason=reason)


def transition(
    project: Any,
    target: str,
    *,
    reason: str,
    actor: str = "pipeline_engine",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在 ORM ``Project`` 实例上执行一次状态迁移（调用方负责 flush/commit）。

    返回迁移审计记录（可直接回给 API 或写日志）。
    """
    current = str(getattr(project, "status", "") or "")
    project_id = getattr(project, "id", None)
    assert_transition(current, target, project_id=project_id, reason=reason)
    project.status = target
    record = {
        "event": "state_transition",
        "wp_id": WP_ID,
        "project_id": project_id,
        "from_status": current,
        "to_status": target,
        "reason": reason,
        "actor": actor,
    }
    if extra:
        record.update(extra)
    logger.info(json.dumps(record, ensure_ascii=False, default=str))
    return record


async def transition_project(
    session: AsyncSession,
    project_id: int,
    target: str,
    *,
    reason: str,
    actor: str = "pipeline_engine",
    extra: dict[str, Any] | None = None,
) -> Any:
    """按 ``project_id`` 载入 Project 并迁移（不 commit，由调用方决定事务边界）。"""
    from db.models import Project  # 局部导入：避免模块导入期触碰 ORM 注册顺序

    project = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise LookupError(f"project {project_id} 不存在")
    transition(project, target, reason=reason, actor=actor, extra=extra)
    return project


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


__all__ = [
    "ACTIVE_STATUSES",
    "PROJECT_STATUSES",
    "RESUMABLE_STATUSES",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "IllegalTransition",
    "assert_transition",
    "can_transition",
    "is_known_status",
    "is_terminal",
    "structured_log",
    "transition",
    "transition_project",
]
