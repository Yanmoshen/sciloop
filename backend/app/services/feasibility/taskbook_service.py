# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""任务书服务与锁定（WP08-T6，附录 A.4 ``taskbooks``）。

- 创建默认 ``status='draft'``，可 ``PATCH`` 编辑；
- ``POST /taskbooks/{id}/lock`` 置 ``locked`` 并写 ``locked_at``；
- **锁定后只读**：任何 ``PATCH`` 一律 409（:class:`TaskbookLockedError`）。

护栏不可放宽（contracts.forbidden_actions 第 2 条）
---------------------------------------------------
``compute_budget.max_llm_cost_usd`` **不得超过**环境配置的硬护栏值
（``PIPELINE_MAX_LLM_COST_USD``，默认 8.0），``demo_cost_quota_usd`` 不得高于硬护栏；
``sample_size`` 上限 50 由执行器把关。试图放宽即 422 拒绝，不做静默截断。

锁定前置条件
------------
任务书是流水线的唯一执行依据，因此锁定前校验：① idea 存在；② idea **至少绑定 1 条证据**
（无证据的 idea 不允许进入执行链，与 ``/ideas/generate`` 的丢弃规则同源）。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Idea, Taskbook
from app.services.ideation.evidence_binder import idea_evidence_ids

logger = logging.getLogger("sciloop.wp08.taskbook")

#: 交付形态白名单（附录 A.4 示例）
DELIVERABLE_OPTIONS: tuple[str, ...] = ("paper_draft", "code", "experiment_log", "slides")

STATUS_DRAFT = "draft"
STATUS_LOCKED = "locked"
STATUSES = (STATUS_DRAFT, STATUS_LOCKED)

#: 硬上限兜底（环境变量缺失时使用；实际值优先读 settings）
FALLBACK_MAX_LLM_COST_USD = 8.0
FALLBACK_DEMO_QUOTA_USD = 3.0

ITERATION_BOUNDS = {
    "max_iterations": (1, 10),
    "max_retry": (0, 5),
    "score_threshold": (0.0, 100.0),
    "marginal_gain_threshold": (0.0, 50.0),
}


class TaskbookError(Exception):
    """任务书业务错误（API 层转 4xx）。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class TaskbookLockedError(TaskbookError):
    """锁定后只读（API 层必须返回 409）。"""

    def __init__(self, taskbook_id: int, action: str = "patch") -> None:
        super().__init__(
            "taskbook_locked",
            f"任务书 {taskbook_id} 已锁定（locked）为流水线唯一执行依据，不允许 {action}；"
            "如需修改请新建任务书",
            {"taskbook_id": int(taskbook_id), "action": action, "status": STATUS_LOCKED},
        )


def _limits() -> tuple[float, float]:
    """成本双线：硬护栏 / 演示配额（读环境配置，不硬编码）。"""
    settings = get_settings()
    hard = float(getattr(settings, "pipeline_max_llm_cost_usd", FALLBACK_MAX_LLM_COST_USD) or FALLBACK_MAX_LLM_COST_USD)
    quota = float(
        getattr(settings, "pipeline_demo_cost_quota_usd", FALLBACK_DEMO_QUOTA_USD)
        or FALLBACK_DEMO_QUOTA_USD
    )
    return hard, quota


def default_rounds() -> dict[str, Any]:
    """默认轮次配置（读环境变量，与附录 A.4 默认值一致时亦如实回显来源）。"""
    settings = get_settings()
    return {
        "max_iterations": int(getattr(settings, "pipeline_max_iterations", 3) or 3),
        "score_threshold": float(getattr(settings, "pipeline_score_threshold", 80) or 80),
        "marginal_gain_threshold": float(
            getattr(settings, "pipeline_marginal_gain_threshold", 2) or 2
        ),
        "max_retry": int(getattr(settings, "pipeline_max_retry", 2) or 2),
    }


def default_compute_budget() -> dict[str, Any]:
    settings = get_settings()
    hard, quota = _limits()
    return {
        "max_llm_cost_usd": hard,
        "demo_cost_quota_usd": quota,
        "max_stage_minutes": int(getattr(settings, "pipeline_max_stage_minutes", 20) or 20),
    }


def _num(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TaskbookError("invalid_number", f"{field} 必须是数值，收到 {value!r}") from exc


def _check_rounds(rounds: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = {**default_rounds(), **{k: v for k, v in (rounds or {}).items() if v is not None}}
    for field, (low, high) in ITERATION_BOUNDS.items():
        value = _num(merged[field], field)
        if not (low <= value <= high):
            raise TaskbookError(
                "invalid_round_config",
                f"{field}={value} 超出允许区间 [{low}, {high}]",
                {"field": field, "value": value, "min": low, "max": high},
            )
        merged[field] = int(value) if field in ("max_iterations", "max_retry") else round(value, 2)
    return merged


def _check_compute_budget(budget: Mapping[str, Any] | None) -> dict[str, Any]:
    hard, quota = _limits()
    merged = {**default_compute_budget(), **{k: v for k, v in (budget or {}).items() if v is not None}}
    max_cost = _num(merged["max_llm_cost_usd"], "compute_budget.max_llm_cost_usd")
    demo_quota = _num(merged["demo_cost_quota_usd"], "compute_budget.demo_cost_quota_usd")
    stage_minutes = _num(merged["max_stage_minutes"], "compute_budget.max_stage_minutes")

    if max_cost <= 0:
        raise TaskbookError("invalid_compute_budget", "max_llm_cost_usd 必须大于 0")
    if max_cost > hard:
        raise TaskbookError(
            "guardrail_relaxed",
            f"max_llm_cost_usd={max_cost} 超过硬护栏 {hard}：禁止放宽成本护栏"
            "（contracts.forbidden_actions：护栏阈值不可被参数覆盖）",
            {"requested": max_cost, "hard_limit_usd": hard},
        )
    if demo_quota > hard:
        raise TaskbookError(
            "guardrail_relaxed",
            f"demo_cost_quota_usd={demo_quota} 高于硬护栏 {hard}：演示配额不得越过熔断线",
            {"requested": demo_quota, "hard_limit_usd": hard},
        )
    if stage_minutes <= 0 or stage_minutes > 1200:
        raise TaskbookError(
            "invalid_compute_budget",
            f"max_stage_minutes={stage_minutes} 不在 (0, 1200] 内（契约 stage_timeout_seconds=1200）",
        )
    return {
        "max_llm_cost_usd": round(max_cost, 4),
        "demo_cost_quota_usd": round(demo_quota, 4),
        "max_stage_minutes": int(stage_minutes),
        "guardrail": {
            "hard_limit_usd": hard,
            "demo_quota_usd": quota,
            "rule": "硬线熔断；演示配额只告警不熔断",
            "relaxable": False,
        },
    }


def _check_deliverables(deliverables: Sequence[str] | None) -> list[str]:
    if not deliverables:
        raise TaskbookError(
            "missing_deliverables",
            "deliverables 不能为空（可选：" + " / ".join(DELIVERABLE_OPTIONS) + "）",
        )
    unknown = [item for item in deliverables if item not in DELIVERABLE_OPTIONS]
    if unknown:
        raise TaskbookError(
            "invalid_deliverables",
            f"deliverables 含未知项 {unknown}",
            {"allowed": list(DELIVERABLE_OPTIONS)},
        )
    return list(dict.fromkeys(deliverables))


def _taskbook_row(row: Taskbook) -> dict[str, Any]:
    locked = str(row.status) == STATUS_LOCKED
    return {
        "id": int(row.id),
        "taskbook_id": int(row.id),
        "project_id": int(row.project_id),
        "idea_id": int(row.idea_id),
        "research_question": row.research_question,
        "target_datasets": list(row.target_datasets or []),
        "baselines": list(row.baselines or []),
        "metrics": list(row.metrics or []),
        "compute_budget": row.compute_budget or {},
        "deliverables": list(row.deliverables or []),
        "max_iterations": row.max_iterations,
        "score_threshold": None if row.score_threshold is None else float(row.score_threshold),
        "marginal_gain_threshold": (
            None if row.marginal_gain_threshold is None else float(row.marginal_gain_threshold)
        ),
        "max_retry": row.max_retry,
        "status": row.status,
        "locked": locked,
        "read_only": locked,
        "locked_at": row.locked_at.isoformat() if row.locked_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "editable_fields": (
            []
            if locked
            else [
                "research_question",
                "target_datasets",
                "baselines",
                "metrics",
                "compute_budget",
                "deliverables",
                "max_iterations",
                "score_threshold",
                "marginal_gain_threshold",
                "max_retry",
            ]
        ),
    }


async def create_taskbook(
    session: AsyncSession,
    *,
    project_id: int,
    idea_id: int,
    research_question: str,
    target_datasets: Sequence[str] | None = None,
    baselines: Sequence[str] | None = None,
    metrics: Sequence[str] | None = None,
    compute_budget: Mapping[str, Any] | None = None,
    deliverables: Sequence[str] | None = None,
    rounds: Mapping[str, Any] | None = None,
    require_idea_evidence: bool = False,
) -> dict[str, Any]:
    """创建任务书（默认 ``draft``）。"""
    if not str(research_question or "").strip():
        raise TaskbookError("missing_research_question", "research_question 不能为空")
    idea = (await session.execute(select(Idea).where(Idea.id == int(idea_id)))).scalars().first()
    if idea is None:
        raise TaskbookError(
            "idea_not_found", f"idea {idea_id} 不存在，禁止凭空创建任务书", {"idea_id": int(idea_id)}
        )

    if require_idea_evidence:
        ids = await idea_evidence_ids(session, int(idea_id))
        if not ids:
            raise TaskbookError(
                "idea_without_evidence",
                f"idea {idea_id} 尚未绑定任何 Evidence：无证据的 idea 不允许进入执行链",
                {"idea_id": int(idea_id), "evidence_ids": []},
            )

    budget = _check_compute_budget(compute_budget)
    round_cfg = _check_rounds(rounds)
    row = Taskbook(
        project_id=int(project_id),
        idea_id=int(idea_id),
        research_question=str(research_question).strip(),
        target_datasets=list(target_datasets or []),
        baselines=list(baselines or []),
        metrics=list(metrics or []),
        compute_budget=budget,
        deliverables=_check_deliverables(deliverables or ["paper_draft", "experiment_log"]),
        max_iterations=round_cfg["max_iterations"],
        score_threshold=Decimal(str(round_cfg["score_threshold"])),
        marginal_gain_threshold=Decimal(str(round_cfg["marginal_gain_threshold"])),
        max_retry=round_cfg["max_retry"],
        status=STATUS_DRAFT,
    )
    session.add(row)
    await session.commit()
    logger.info(
        "create_taskbook id=%s project=%s idea=%s budget=%s rounds=%s",
        row.id,
        project_id,
        idea_id,
        budget,
        round_cfg,
    )
    return {
        "item": _taskbook_row(row),
        "guardrail": budget["guardrail"],
        "round_config": round_cfg,
        "next_step": "编辑完成后 POST /taskbooks/{id}/lock 锁定为流水线执行依据",
    }


async def get_taskbook(session: AsyncSession, taskbook_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(select(Taskbook).where(Taskbook.id == int(taskbook_id)))
    ).scalars().first()
    return None if row is None else _taskbook_row(row)


async def list_taskbooks(
    session: AsyncSession, *, project_id: int | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = select(Taskbook).order_by(Taskbook.id.desc()).limit(max(1, int(limit)))
    if project_id is not None:
        stmt = (
            select(Taskbook)
            .where(Taskbook.project_id == int(project_id))
            .order_by(Taskbook.id.desc())
            .limit(max(1, int(limit)))
        )
    rows = (await session.execute(stmt)).scalars().all()
    return [_taskbook_row(row) for row in rows]


async def update_taskbook(
    session: AsyncSession, taskbook_id: int, patch: Mapping[str, Any]
) -> dict[str, Any]:
    """编辑任务书；**已锁定则 409**（:class:`TaskbookLockedError`）。"""
    row = (
        await session.execute(select(Taskbook).where(Taskbook.id == int(taskbook_id)))
    ).scalars().first()
    if row is None:
        raise TaskbookError(
            "taskbook_not_found", f"任务书 {taskbook_id} 不存在", {"taskbook_id": int(taskbook_id)}
        )
    if str(row.status) == STATUS_LOCKED:
        raise TaskbookLockedError(int(taskbook_id), action="patch")

    data = {key: value for key, value in (patch or {}).items() if value is not None}
    changed: list[str] = []
    if "research_question" in data:
        text = str(data["research_question"]).strip()
        if not text:
            raise TaskbookError("missing_research_question", "research_question 不能为空")
        row.research_question = text
        changed.append("research_question")
    for field in ("target_datasets", "baselines", "metrics"):
        if field in data:
            setattr(row, field, list(data[field] or []))
            changed.append(field)
    if "deliverables" in data:
        row.deliverables = _check_deliverables(data["deliverables"])
        changed.append("deliverables")
    if "compute_budget" in data:
        current = dict(row.compute_budget or {})
        row.compute_budget = _check_compute_budget({**current, **data["compute_budget"]})
        changed.append("compute_budget")
    if any(
        field in data
        for field in ("max_iterations", "score_threshold", "marginal_gain_threshold", "max_retry")
    ):
        current = {
            "max_iterations": row.max_iterations,
            "score_threshold": None if row.score_threshold is None else float(row.score_threshold),
            "marginal_gain_threshold": (
                None
                if row.marginal_gain_threshold is None
                else float(row.marginal_gain_threshold)
            ),
            "max_retry": row.max_retry,
        }
        merged = _check_rounds({**current, **data})
        row.max_iterations = merged["max_iterations"]
        row.score_threshold = Decimal(str(merged["score_threshold"]))
        row.marginal_gain_threshold = Decimal(str(merged["marginal_gain_threshold"]))
        row.max_retry = merged["max_retry"]
        changed.extend(
            [
                key
                for key in (
                    "max_iterations",
                    "score_threshold",
                    "marginal_gain_threshold",
                    "max_retry",
                )
                if key in data
            ]
        )
    await session.commit()
    logger.info("update_taskbook id=%s changed=%s", taskbook_id, changed)
    return {"item": _taskbook_row(row), "changed_fields": changed}


async def lock_taskbook(session: AsyncSession, taskbook_id: int) -> dict[str, Any]:
    """锁定任务书（幂等：重复锁定返回当前状态并标 ``already_locked``）。"""
    row = (
        await session.execute(select(Taskbook).where(Taskbook.id == int(taskbook_id)))
    ).scalars().first()
    if row is None:
        raise TaskbookError(
            "taskbook_not_found", f"任务书 {taskbook_id} 不存在", {"taskbook_id": int(taskbook_id)}
        )
    if str(row.status) == STATUS_LOCKED:
        return {"item": _taskbook_row(row), "already_locked": True, "locked_now": False}
    if not str(row.research_question or "").strip():
        raise TaskbookError("missing_research_question", "research_question 为空，不允许锁定")

    evidence_ids = await idea_evidence_ids(session, int(row.idea_id))
    if not evidence_ids:
        raise TaskbookError(
            "idea_without_evidence",
            f"idea {row.idea_id} 无 Evidence：任务书不允许锁定（无证据的 idea 不得进入执行链）",
            {"idea_id": int(row.idea_id), "evidence_ids": []},
        )

    row.status = STATUS_LOCKED
    row.locked_at = datetime.datetime.now(datetime.UTC)
    await session.commit()
    logger.info("lock_taskbook id=%s idea=%s evidences=%s", taskbook_id, row.idea_id, evidence_ids)
    return {
        "item": _taskbook_row(row),
        "already_locked": False,
        "locked_now": True,
        "idea_evidence_ids": evidence_ids,
        "next_step": "任务书已锁定为流水线唯一执行依据；后续 PATCH 将返回 409",
    }


__all__ = [
    "DELIVERABLE_OPTIONS",
    "FALLBACK_DEMO_QUOTA_USD",
    "FALLBACK_MAX_LLM_COST_USD",
    "ITERATION_BOUNDS",
    "STATUSES",
    "STATUS_DRAFT",
    "STATUS_LOCKED",
    "TaskbookError",
    "TaskbookLockedError",
    "create_taskbook",
    "default_compute_budget",
    "default_rounds",
    "get_taskbook",
    "list_taskbooks",
    "lock_taskbook",
    "update_taskbook",
]
