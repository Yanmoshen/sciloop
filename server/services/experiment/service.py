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
"""实验编排服务（WP11-T4/T5/T7 的共用入口）。

一次实验的完整链路（**唯一入口**，API / 流水线环节 / 验收脚本都走这里）：

1. :func:`executor.execute_template` —— 受限执行器跑模板（限额 / 白名单 / 超时）
2. :mod:`services.experiment.metrics` —— 真实指标落 ``experiment_metrics``
3. 回填 ``stage_outputs.output_json``（有 ``pipeline_run_id`` 时）
4. :mod:`services.experiment.passport` —— 生成不可变 Passport

设计约束
--------
- 指标只来自模板返回的真实记录；本模块**不做任何补充、估算或平滑**
- ``persist=False`` 时只跑不落库（验收脚本核对用），此时不会生成 Passport
- 不吞异常：执行器错误如实向上抛（环节层再按 L1/L2/L3 分级）
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from executor import RunOutcome, execute_template
from services.experiment import metrics as M
from services.experiment import passport as passport_mod

logger = logging.getLogger("sciloop.experiment.service")


# --------------------------------------------------------------------------- #
# 主链路
# --------------------------------------------------------------------------- #
async def execute_experiment(
    template_id: str,
    config: Mapping[str, Any],
    *,
    project_id: int | None = None,
    stage: str = "experiment",
    attempt: int = 1,
    session: Any = None,
    run_id: int | None = None,
    experiment_id: int | None = None,
    replay_only: bool = False,
    fixture_source: Mapping[str, Any] | None = None,
    parent_passport_id: int | None = None,
    pipeline_run_id: int | None = None,
    note: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """执行一次实验并封成 Passport，返回 ``{run, metrics, passport, outcome}``。"""
    outcome: RunOutcome = await execute_template(
        template_id,
        dict(config),
        project_id=project_id,
        stage=stage,
        attempt=attempt,
        run_id=run_id,
        experiment_id=experiment_id,
        persist=persist,
        session=session,
        replay_only=replay_only,
        fixture_source=fixture_source,
    )

    result: dict[str, Any] = {
        "run": outcome.to_dict(),
        "metrics": [],
        "passport": None,
        "outcome": outcome,
    }
    if not persist:
        return result

    metric_meta = {
        "template_id": outcome.template_id,
        "experiment_run_id": outcome.run_id,
        "experiment_id": outcome.experiment_id,
        "stage": stage,
        "attempt": attempt,
        "is_replay": outcome.is_replay,
        "run_status": outcome.status,
        "sample_size": len(outcome.samples),
        "duration_ms": outcome.duration_ms,
        "cost_usd": outcome.cost_usd,
        "degradations": outcome.degradations,
    }
    persisted = await M.persist_run_metrics(
        run_id=outcome.run_id, metrics=outcome.metrics, meta=metric_meta, session=session
    )
    result["metrics"] = await M.load_run_metrics(run_id=outcome.run_id, session=session)

    block = {
        "experiment_id": outcome.experiment_id,
        "run_id": outcome.run_id,
        "template_id": outcome.template_id,
        "status": outcome.status,
        "is_replay": outcome.is_replay,
        "sample_size": len(outcome.samples),
        "cost_usd": outcome.cost_usd,
        "duration_ms": outcome.duration_ms,
        "metric_rows": persisted.get("rows"),
        "metric_names": persisted.get("metric_names"),
        "artifact_path": outcome.artifact_path,
    }
    # 环节执行期 stage_outputs 尚未落库（引擎在环节返回后才写）；只在行已存在时回填，
    # 避免每次都打一条误导性的告警。引擎落库后同样会带上 StageResult.output 的同一份 block。
    if await stage_output_exists(
        pipeline_run_id=pipeline_run_id, stage=stage, attempt=attempt, session=session
    ):
        block["stage_output_backfill"] = await M.backfill_stage_output(
            pipeline_run_id=pipeline_run_id,
            stage=stage,
            attempt=attempt,
            block=block,
            session=session,
        )
    else:
        block["stage_output_backfill"] = {
            "updated": False,
            "reason": "stage_output_not_written_yet（引擎在环节返回后落库，块内容已随 StageResult.output 一并写入）",
        }

    passport = await passport_mod.create_passport(
        outcome,
        session=session,
        parent_passport_id=parent_passport_id,
        note=note,
        metric_meta=metric_meta,
    )
    result["passport"] = passport
    logger.info(
        "实验完成 run=%s template=%s status=%s passport=%s passport_status=%s metrics=%s",
        outcome.run_id,
        outcome.template_id,
        outcome.status,
        passport.get("id"),
        passport.get("status"),
        len(result["metrics"]),
    )
    return result


# --------------------------------------------------------------------------- #
# 查询（API 用）
# --------------------------------------------------------------------------- #
async def list_templates(*, implemented_only: bool = False) -> list[dict[str, Any]]:
    """模板清单（``GET /experiments/templates``，如实暴露实现状态）。"""
    from services.experiment import registry

    return registry.list_templates(implemented_only=implemented_only)


async def list_experiments(*, session: Any = None, limit: int = 100) -> list[dict[str, Any]]:
    """实验定义清单（按 id 倒序），供前端定位最近一次实验。"""
    from sqlalchemy import select

    from db.models.pipeline import Experiment

    own_session, should_close = await _session_or_new(session)
    try:
        rows = (
            (await own_session.execute(select(Experiment).order_by(Experiment.id.desc()).limit(int(limit))))
            .scalars()
            .all()
        )
    finally:
        if should_close:
            await own_session.close()
    return [
        {
            "id": int(row.id),
            "template_id": row.template_id,
            "config": row.config,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def list_runs(
    experiment_id: int, *, page: int = 1, page_size: int = 20, session: Any = None
) -> dict[str, Any]:
    """某实验的 Run 列表（``GET /experiments/{id}/runs``，含每 Run 的 Passport 摘要）。"""
    from sqlalchemy import func, select

    from db.models.pipeline import Experiment, ExperimentMetric, ExperimentRun
    from db.models.review import ExperimentPassport

    own_session, should_close = await _session_or_new(session)
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    try:
        exists = (
            await own_session.execute(select(Experiment.id).where(Experiment.id == int(experiment_id)))
        ).scalar_one_or_none()
        if exists is None:
            raise LookupError(f"experiment id={experiment_id} 不存在")
        total = (
            await own_session.execute(
                select(func.count(ExperimentRun.id)).where(
                    ExperimentRun.experiment_id == int(experiment_id)
                )
            )
        ).scalar() or 0
        rows = (
            (
                await own_session.execute(
                    select(ExperimentRun)
                    .where(ExperimentRun.experiment_id == int(experiment_id))
                    .order_by(ExperimentRun.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        run_ids = [int(row.id) for row in rows]
        passports: dict[int, dict[str, Any]] = {}
        metric_names: dict[int, list[str]] = {}
        if run_ids:
            passport_rows = (
                (
                    await own_session.execute(
                        select(ExperimentPassport).where(
                            ExperimentPassport.experiment_run_id.in_(run_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in passport_rows:
                passports[int(row.experiment_run_id)] = {
                    "id": int(row.id),
                    "status": row.status,
                    "is_replay": bool(row.is_replay),
                    "parent_passport_id": int(row.parent_passport_id)
                    if row.parent_passport_id
                    else None,
                    "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
                }
            metric_rows = (
                await own_session.execute(
                    select(ExperimentMetric.experiment_run_id, ExperimentMetric.metric_name)
                    .where(ExperimentMetric.experiment_run_id.in_(run_ids))
                    .order_by(ExperimentMetric.id)
                )
            ).all()
            for run_id, name in metric_rows:
                metric_names.setdefault(int(run_id), []).append(str(name))
    finally:
        if should_close:
            await own_session.close()

    items = [
        {
            "id": int(row.id),
            "experiment_id": int(row.experiment_id),
            "attempt": int(row.attempt or 1),
            "status": row.status,
            "error": row.error,
            "duration_ms": int(row.duration_ms) if row.duration_ms is not None else None,
            "artifact_path": row.artifact_path,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "passport": passports.get(int(row.id)),
            "metric_names": metric_names.get(int(row.id), []),
        }
        for row in rows
    ]
    return {"items": items, "total": int(total), "page": page, "page_size": page_size}


async def get_run(run_id: int, *, session: Any = None) -> dict[str, Any]:
    """单个 Run 详情（不存在即抛 ``LookupError``）。"""
    from sqlalchemy import select

    from db.models.pipeline import ExperimentRun

    own_session, should_close = await _session_or_new(session)
    try:
        row = (
            await own_session.execute(select(ExperimentRun).where(ExperimentRun.id == int(run_id)))
        ).scalar_one_or_none()
    finally:
        if should_close:
            await own_session.close()
    if row is None:
        raise LookupError(f"experiment_run id={run_id} 不存在")
    return {
        "id": int(row.id),
        "experiment_id": int(row.experiment_id),
        "attempt": int(row.attempt or 1),
        "status": row.status,
        "error": row.error,
        "duration_ms": int(row.duration_ms) if row.duration_ms is not None else None,
        "artifact_path": row.artifact_path,
        "executor_task_id": row.executor_task_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def run_metrics(run_id: int, *, session: Any = None) -> list[dict[str, Any]]:
    """Run 指标（``GET /experiments/runs/{id}/metrics``）。"""
    return await M.load_run_metrics(run_id=int(run_id), session=session)


async def run_passport(run_id: int, *, session: Any = None) -> dict[str, Any] | None:
    """Run 的 Passport（``GET /experiments/runs/{id}/passport``；无则 ``None``）。"""
    return await passport_mod.get_passport_by_run(int(run_id), session=session)


async def experiment_passports(
    experiment_id: int, *, session: Any = None
) -> list[dict[str, Any]]:
    """某实验下所有 Run 的 Passport（父/子凭证树）。"""
    from sqlalchemy import select

    from db.models.pipeline import ExperimentRun
    from db.models.review import ExperimentPassport

    own_session, should_close = await _session_or_new(session)
    try:
        run_ids = [
            int(row)
            for row in (
                await own_session.execute(
                    select(ExperimentRun.id).where(ExperimentRun.experiment_id == int(experiment_id))
                )
            )
            .scalars()
            .all()
        ]
        rows: Sequence[Any] = []
        if run_ids:
            rows = (
                (
                    await own_session.execute(
                        select(ExperimentPassport)
                        .where(ExperimentPassport.experiment_run_id.in_(run_ids))
                        .order_by(ExperimentPassport.id)
                    )
                )
                .scalars()
                .all()
            )
    finally:
        if should_close:
            await own_session.close()
    return [passport_mod.serialize(row) for row in rows]


async def stage_output_exists(
    *, pipeline_run_id: int | None, stage: str, attempt: int, session: Any = None
) -> bool:
    """``stage_outputs`` 行是否已存在（环节执行期通常尚未落库 → 回填留待引擎落库后读）。"""
    if not pipeline_run_id:
        return False
    from sqlalchemy import select

    from db.models.pipeline import StageOutput

    own_session, should_close = await _session_or_new(session)
    try:
        row = (
            await own_session.execute(
                select(StageOutput.id).where(
                    StageOutput.pipeline_run_id == int(pipeline_run_id),
                    StageOutput.stage == str(stage),
                    StageOutput.attempt == int(attempt),
                )
            )
        ).scalar_one_or_none()
    finally:
        if should_close:
            await own_session.close()
    return row is not None


async def _session_or_new(session: Any) -> tuple[Any, bool]:
    if session is not None:
        return session, False
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover
        raise RuntimeError("数据库会话不可用（DATABASE_URL 未就绪）")
    return AsyncSessionLocal(), True


__all__ = [
    "execute_experiment",
    "experiment_passports",
    "get_run",
    "list_experiments",
    "list_runs",
    "list_templates",
    "run_metrics",
    "run_passport",
    "stage_output_exists",
]
