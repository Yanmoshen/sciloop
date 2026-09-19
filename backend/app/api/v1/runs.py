# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""流水线运行记录与《失败分析报告》（WP09-T7）。

- ``GET /runs?project_id=&page=&page_size=`` —— 运行记录分页
- ``GET /runs/{run_id}`` —— 单次运行详情（含六环节各 attempt 的真实产出与成本）
- ``GET /reports/{project_id}/failure`` —— 计划书 §2.5.2《失败分析报告》

报告口径：优先返回熔断时落库的**快照**（``source='snapshot'``），无快照时按当前库内数据
实况计算（``source='live'``）。两者都只聚合真实数据（stage_outputs / decision_logs /
llm_call_logs），不含推测数值。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.db.models import PipelineRun, Project
from app.db.session import AsyncSessionLocal
from app.services.pipeline.failure_handler import build_failure_report, latest_persisted_report
from app.services.pipeline.resume import load_stage_rows

logger = logging.getLogger("sciloop.api.runs")

router = APIRouter(tags=["runs"])


def _session_factory() -> Any:
    if AsyncSessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unavailable", "message": "数据库会话不可用", "detail": None},
        )
    return AsyncSessionLocal


def _run_to_dict(run: PipelineRun) -> dict[str, Any]:
    return {
        "id": int(run.id),
        "project_id": int(run.project_id),
        "iteration": int(run.iteration or 1),
        "mode": str(run.mode),
        "status": str(run.status),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "total_cost_usd": float(run.total_cost_usd or 0),
        "stop_reason": run.stop_reason,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs", summary="流水线运行记录")
async def list_runs(
    project_id: Annotated[int | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    run_status: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    async with _session_factory()() as session:
        conditions = []
        if project_id is not None:
            conditions.append(PipelineRun.project_id == project_id)
        if run_status:
            conditions.append(PipelineRun.status == run_status)
        total = int(
            (await session.execute(select(func.count(PipelineRun.id)).where(*conditions))).scalar_one()
            or 0
        )
        rows = list(
            (
                await session.execute(
                    select(PipelineRun)
                    .where(*conditions)
                    .order_by(PipelineRun.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "items": [_run_to_dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/runs/{run_id}", summary="运行详情（含各 attempt 环节产出）")
async def run_detail(run_id: int) -> dict[str, Any]:
    async with _session_factory()() as session:
        run = (
            await session.execute(select(PipelineRun).where(PipelineRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "run_not_found", "message": f"run {run_id} 不存在", "detail": None},
            )
        rows = await load_stage_rows(session, run_id)
    return {
        "run": _run_to_dict(run),
        "stage_attempts": [
            {
                "id": int(row.id),
                "stage": str(row.stage),
                "attempt": int(row.attempt or 1),
                "status": str(row.status),
                "cost_usd": float(row.cost_usd or 0),
                "duration_ms": int(row.duration_ms) if row.duration_ms is not None else None,
                "error": row.error,
                "output_json": row.output_json,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
        "note": "同一 (pipeline_run_id, stage, attempt) 唯一；断点续跑以 status=done 为复用判据",
    }


@router.get("/reports/{project_id}/failure", summary="《失败分析报告》")
async def failure_report(
    project_id: int,
    source: Annotated[str, Query(pattern="^(auto|snapshot|live)$")] = "auto",
) -> dict[str, Any]:
    async with _session_factory()() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "project_not_found",
                    "message": f"project {project_id} 不存在",
                    "detail": None,
                },
            )
        snapshot = None if source == "live" else await latest_persisted_report(session, project_id)
        live = await build_failure_report(session, project_id)

    if source == "snapshot" and snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "report_not_found",
                "message": "该项目尚无熔断时落库的失败报告快照（可用 ?source=live 查看当前实况）",
                "detail": None,
            },
        )
    if snapshot is not None and source in {"auto", "snapshot"}:
        report = dict(snapshot)
        report["source"] = "snapshot"
        report["live_summary"] = {
            "project_status": live.get("project_status"),
            "run_status": live.get("run_status"),
            "failed_stage": live.get("failed_stage"),
            "level": live.get("level"),
            "failure_chain_length": len(live.get("failure_chain") or []),
        }
        return report
    live["source"] = "live"
    return live


__all__ = ["router"]
