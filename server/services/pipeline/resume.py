# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""断点续跑（WP09-T5）与 ``stage_outputs`` 幂等写入。

恢复点定义（计划书 §2.2 / 附录 A.5）：

- **唯一依据是 ``stage_outputs.status``**：从第一个 ``status != 'done'`` 的环节继续
- 已是 ``done`` 的环节直接复用其 ``output_json``（不重复消耗 LLM 与外部 IO）
- ``plan_review`` 判 ``revise`` ⇒ 该环节视为未完成（同一 attempt 覆盖写 report），
  并把被回退的环节（``revert_to``，通常为 ``plan``）的 ``attempt + 1`` 重新进入
- 写入必须幂等：``UNIQUE (pipeline_run_id, stage, attempt)`` + ``ON CONFLICT DO UPDATE``，
  **任何时刻同一 run 内最多一条 ``status='running'``**
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PipelineRun, StageOutput
from services.pipeline.stages.base import STAGE_ORDER

logger = logging.getLogger("sciloop.pipeline.resume")

_UNSET: Any = object()

#: 可复用的状态（断点续跑唯一判据）
DONE = "done"
RUNNING = "running"


@dataclass
class StagePlanItem:
    """一个环节的续跑计划项。"""

    stage: str
    attempt: int
    status: str
    stage_output_id: int | None = None
    output_json: dict[str, Any] | None = None
    error: str | None = None
    #: 被上游判 ``revise``/``revert_to`` 回退时的说明（透明留痕）
    revert_reason: str | None = None
    #: 该环节历史已用过的 attempt 列表（审计用）
    attempts_seen: list[int] = field(default_factory=list)

    @property
    def reusable(self) -> bool:
        """``done`` 且产出可解析 ⇒ 直接复用，不再执行。"""
        return self.status == DONE and isinstance(self.output_json, dict)

    @property
    def needs_execution(self) -> bool:
        return not self.reusable

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "attempt": self.attempt,
            "status": self.status,
            "stage_output_id": self.stage_output_id,
            "reusable": self.reusable,
            "revert_reason": self.revert_reason,
            "attempts_seen": list(self.attempts_seen),
            "has_output": self.output_json is not None,
        }


async def load_stage_rows(session: AsyncSession, run_id: int) -> list[StageOutput]:
    """读取某 run 的全部 ``stage_outputs``（按环节顺序 + attempt 升序）。"""
    rows = (
        (
            await session.execute(
                select(StageOutput)
                .where(StageOutput.pipeline_run_id == run_id)
                .order_by(StageOutput.stage, StageOutput.attempt)
            )
        )
        .scalars()
        .all()
    )
    order = {name: index for index, name in enumerate(STAGE_ORDER)}
    return sorted(rows, key=lambda row: (order.get(str(row.stage), 99), int(row.attempt or 1)))


def _revert_map(rows: list[StageOutput]) -> dict[str, str]:
    """扫描 ``output_json.revert_to``：被回退的环节 → 回退原因。"""
    reverted: dict[str, str] = {}
    for row in rows:
        if str(row.status) not in {"failed", "pending", "waiting_human"}:
            continue
        payload = row.output_json if isinstance(row.output_json, dict) else None
        if not payload:
            continue
        target = payload.get("revert_to")
        if isinstance(target, str) and target in STAGE_ORDER:
            reason = str(payload.get("revert_reason") or f"{row.stage} 判定 revise，回退 {target}")
            reverted[target] = reason
            # 回退发起环节自身「视为未完成」：保持同一 attempt，下次覆盖写 report
    return reverted


async def build_resume_plan(session: AsyncSession, run_id: int) -> list[StagePlanItem]:
    """构造六环节续跑计划（顺序固定，done 复用，其余待执行）。"""
    rows = await load_stage_rows(session, run_id)
    reverted = _revert_map(rows)

    by_stage: dict[str, list[StageOutput]] = {name: [] for name in STAGE_ORDER}
    for row in rows:
        by_stage.setdefault(str(row.stage), []).append(row)

    plan: list[StagePlanItem] = []
    for stage in STAGE_ORDER:
        stage_rows = by_stage.get(stage) or []
        attempts_seen = [int(row.attempt or 1) for row in stage_rows]
        if not stage_rows:
            plan.append(StagePlanItem(stage=stage, attempt=1, status="pending"))
            continue
        latest = stage_rows[-1]
        status = str(latest.status)
        output = latest.output_json if isinstance(latest.output_json, dict) else None
        item = StagePlanItem(
            stage=stage,
            attempt=int(latest.attempt or 1),
            status=status,
            stage_output_id=int(latest.id),
            output_json=output,
            error=latest.error,
            attempts_seen=attempts_seen,
        )
        if stage in reverted and status == DONE:
            # 被下游回退：不复用，以 attempt+1 重跑（保留旧版本，便于审计对比）
            item.attempt = int(latest.attempt or 1) + 1
            item.status = "pending"
            item.revert_reason = reverted[stage]
            item.output_json = None
        elif stage in reverted and status in {"failed", "waiting_human", "pending", "running"}:
            item.status = "pending"
            item.revert_reason = reverted[stage]
        elif status in {RUNNING}:
            # 上一进程崩在 running：视为未完成，同 attempt 覆盖重跑
            item.status = "pending"
            item.revert_reason = "上一次执行中断于 running（进程重启），同 attempt 覆盖重跑"
        plan.append(item)
    return plan


async def next_pending(session: AsyncSession, run_id: int) -> StagePlanItem | None:
    """第一个需要执行的环节（第一个 ``status != done``）。"""
    for item in await build_resume_plan(session, run_id):
        if item.needs_execution:
            return item
    return None


async def completed_outputs(session: AsyncSession, run_id: int) -> dict[str, dict[str, Any]]:
    """已完成环节的产出（供下游环节作为输入；**只复用 done**）。"""
    outputs: dict[str, dict[str, Any]] = {}
    for item in await build_resume_plan(session, run_id):
        if item.reusable and item.output_json is not None:
            outputs[item.stage] = item.output_json
    return outputs


async def upsert_stage_output(
    session: AsyncSession,
    *,
    pipeline_run_id: int,
    stage: str,
    attempt: int,
    status: str,
    output_json: Any = _UNSET,
    output_text: Any = _UNSET,
    cost_usd: Any = _UNSET,
    duration_ms: Any = _UNSET,
    error: Any = _UNSET,
    started_at: Any = _UNSET,
    finished_at: Any = _UNSET,
) -> StageOutput:
    """幂等写入：``INSERT ... ON CONFLICT (pipeline_run_id, stage, attempt) DO UPDATE``。

    只更新本次显式传入的字段，未传入的列保持原值（避免把已有产出擦掉）。
    """
    if stage not in STAGE_ORDER:
        raise ValueError(f"未知环节 {stage!r}，合法值 {list(STAGE_ORDER)}")

    values: dict[str, Any] = {
        "pipeline_run_id": int(pipeline_run_id),
        "stage": stage,
        "attempt": int(attempt),
        "status": status,
        "updated_at": func.now(),
    }
    updates: dict[str, Any] = {"status": status, "updated_at": func.now()}
    optional = {
        "output_json": output_json,
        "output_text": output_text,
        "cost_usd": cost_usd,
        "duration_ms": duration_ms,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    for key, value in optional.items():
        if value is _UNSET:
            continue
        values[key] = value
        updates[key] = value

    statement = (
        pg_insert(StageOutput)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["pipeline_run_id", "stage", "attempt"], set_=updates
        )
        .returning(StageOutput.id)
    )
    row_id = (await session.execute(statement)).scalar_one()
    await session.flush()
    row = (
        await session.execute(select(StageOutput).where(StageOutput.id == int(row_id)))
    ).scalar_one()
    logger.debug(
        "stage_output 幂等写入 run=%s stage=%s attempt=%s status=%s",
        pipeline_run_id,
        stage,
        attempt,
        status,
    )
    return row


async def reset_stale_running(
    session: AsyncSession, run_id: int, *, keep: tuple[str, int] | None = None
) -> list[dict[str, Any]]:
    """把同一 run 内**其它**残留 ``running`` 记录重置为 ``failed``。

    保证「任何时刻同一 run 最多一条 running」——进程被 kill 后重启时，
    死掉的 running 行必须显式收尾（而不是让两条 running 并存）。
    """
    rows = (
        (
            await session.execute(
                select(StageOutput).where(
                    StageOutput.pipeline_run_id == run_id, StageOutput.status == RUNNING
                )
            )
        )
        .scalars()
        .all()
    )
    reset: list[dict[str, Any]] = []
    for row in rows:
        if keep and str(row.stage) == keep[0] and int(row.attempt or 1) == keep[1]:
            continue
        await upsert_stage_output(
            session,
            pipeline_run_id=run_id,
            stage=str(row.stage),
            attempt=int(row.attempt or 1),
            status="failed",
            error="进程重启：上一次执行的 running 状态未收尾，已标记 failed 并以同 attempt 重跑",
            finished_at=func.now(),
        )
        reset.append({"stage": str(row.stage), "attempt": int(row.attempt or 1)})
    if reset:
        logger.warning("检测到残留 running 记录并已收尾 run=%s %s", run_id, reset)
    return reset


async def stage_summary(session: AsyncSession, run_id: int) -> list[dict[str, Any]]:
    """六环节最新状态摘要（``GET /pipelines/{pid}/stages`` 的数据源）。"""
    rows = await load_stage_rows(session, run_id)
    latest: dict[str, StageOutput] = {}
    for row in rows:
        latest[str(row.stage)] = row
    summary: list[dict[str, Any]] = []
    for stage in STAGE_ORDER:
        row = latest.get(stage)
        if row is None:
            summary.append(
                {
                    "stage": stage,
                    "status": "pending",
                    "attempt": 1,
                    "cost_usd": 0.0,
                    "duration_ms": None,
                    "error": None,
                    "has_output": False,
                    "output_json": None,
                    "updated_at": None,
                }
            )
            continue
        summary.append(
            {
                "stage": stage,
                "status": str(row.status),
                "attempt": int(row.attempt or 1),
                "stage_output_id": int(row.id),
                "cost_usd": float(row.cost_usd or 0),
                "duration_ms": int(row.duration_ms) if row.duration_ms is not None else None,
                "error": row.error,
                "has_output": isinstance(row.output_json, dict),
                "output_json": row.output_json,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    return summary


async def latest_run(session: AsyncSession, project_id: int) -> PipelineRun | None:
    """项目最近一次流水线运行（按 iteration/id 倒序）。"""
    return (
        await session.execute(
            select(PipelineRun)
            .where(PipelineRun.project_id == project_id)
            .order_by(PipelineRun.iteration.desc(), PipelineRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def active_run(session: AsyncSession, project_id: int) -> PipelineRun | None:
    """当前未结束的 run（``running`` / ``paused`` / ``circuit_break``）。"""
    return (
        await session.execute(
            select(PipelineRun)
            .where(
                PipelineRun.project_id == project_id,
                PipelineRun.status.in_(("running", "paused", "circuit_break")),
            )
            .order_by(PipelineRun.iteration.desc(), PipelineRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_cost_total(session: AsyncSession, run_id: int) -> float:
    """该 run 已落库的环节成本合计（真实值，来自 stage_outputs.cost_usd）。"""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(StageOutput.cost_usd), 0)).where(
                StageOutput.pipeline_run_id == run_id
            )
        )
    ).scalar_one()
    return round(float(total or 0), 6)


__all__ = [
    "DONE",
    "RUNNING",
    "StagePlanItem",
    "active_run",
    "build_resume_plan",
    "completed_outputs",
    "latest_run",
    "load_stage_rows",
    "next_pending",
    "reset_stale_running",
    "run_cost_total",
    "stage_summary",
    "upsert_stage_output",
]
