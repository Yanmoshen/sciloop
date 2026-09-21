# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""流水线 API（WP09-T7；附录 B.4）。

======== ==============================================  ==========  ==========================
方法      路径                                            鉴权        说明
======== ==============================================  ==========  ==========================
POST     ``/pipelines``                                  Owner       创建流水线 ``{project_id}``
POST     ``/pipelines/{pid}/run?mode=auto\\|manual``      Owner       启动 / 断点续跑
POST     ``/pipelines/{pid}/pause``                      Owner       暂停（环节边界生效）
POST     ``/pipelines/{pid}/resume``                     Owner       继续（跳过 ``done`` 环节）
POST     ``/pipelines/{pid}/stop``                       Owner       中止（``stop_reason=manual``）
POST     ``/pipelines/{pid}/switch-mode``                Owner       运行中切换模式
GET      ``/pipelines/{pid}/status``                     公开        状态 + 当前环节 + 迭代 + 成本双线
GET      ``/pipelines/{pid}/stages``                     公开        六环节产出
GET      ``/pipelines/{pid}/decision-logs``              公开        决策日志（D1–D6 + 护栏检查）
GET      ``/pipelines/{pid}/interventions``              公开        介入记录（N1–N4）
POST     ``/pipelines/{pid}/intervene``                  Owner       人工介入 ``{node,action,payload}``
======== ==============================================  ==========  ==========================

``{pid}`` 为 ``project_id``（与附录 B.4 一致）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from core.security import require_owner
from db.models import DecisionLog, Intervention, PipelineRun
from db.session import AsyncSessionLocal
from services.pipeline.engine import PipelineError, engine
from services.pipeline.resume import active_run, latest_run
from services.pipeline.stages import registry_snapshot
from services.pipeline.state_machine import IllegalTransition

logger = logging.getLogger("sciloop.api.pipelines")

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

OwnerDep = Annotated[None, Depends(require_owner)]


def _session_factory() -> Any:
    if AsyncSessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unavailable", "message": "数据库会话不可用", "detail": None},
        )
    return AsyncSessionLocal


def _http_error(exc: Exception) -> HTTPException:
    """把引擎异常映射为契约错误体 ``{code,message,detail}``。"""
    if isinstance(exc, IllegalTransition):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict())
    if isinstance(exc, PipelineError):
        code = exc.code
        http_status = {
            "project_not_found": status.HTTP_404_NOT_FOUND,
            "run_not_found": status.HTTP_404_NOT_FOUND,
            "taskbook_not_locked": status.HTTP_409_CONFLICT,
            "run_not_running": status.HTTP_409_CONFLICT,
            "invalid_mode": status.HTTP_400_BAD_REQUEST,
            "invalid_node": status.HTTP_400_BAD_REQUEST,
            "invalid_action": status.HTTP_400_BAD_REQUEST,
            "project_aborted": status.HTTP_409_CONFLICT,
            "project_done": status.HTTP_409_CONFLICT,
            "decision_already_resolved": status.HTTP_409_CONFLICT,
            "db_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
        }.get(code, status.HTTP_400_BAD_REQUEST)
        return HTTPException(status_code=http_status, detail=exc.to_dict())
    if isinstance(exc, LookupError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "not_found", "message": str(exc), "detail": None},
        )
    logger.exception("流水线接口未预期异常")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "internal_error", "message": str(exc), "detail": None},
    )


async def _ensure_project(project_id: int) -> None:
    from db.models import Project

    async with _session_factory()() as session:
        exists = (
            await session.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "project_not_found", "message": f"project {project_id} 不存在", "detail": None},
        )


# --------------------------------------------------------------------------- #
# 写操作（Owner）
# --------------------------------------------------------------------------- #
@router.post("", summary="创建流水线（Owner）")
async def create_pipeline(
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
) -> dict[str, Any]:
    """创建一条流水线运行记录（不启动）。"""
    project_id = payload.get("project_id")
    if not isinstance(project_id, int):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "validation_error", "message": "project_id 必须为整数", "detail": payload},
        )
    await _ensure_project(project_id)
    async with _session_factory()() as session:
        from db.models import Project

        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one()
        run = PipelineRun(
            project_id=project_id,
            iteration=int(project.current_iteration or 0) + 1,
            mode=str(payload.get("mode") or project.mode),
            status="paused",
        )
        session.add(run)
        await session.flush()
        result = {
            "pipeline_run_id": int(run.id),
            "project_id": project_id,
            "iteration": int(run.iteration),
            "mode": str(run.mode),
            "status": str(run.status),
            "started": False,
            "note": "已创建 run（status=paused，尚未启动）；调用 POST /pipelines/{pid}/run 启动",
            "stage_registry": registry_snapshot(),
        }
        await session.commit()
    return result


@router.post("/{project_id}/run", summary="启动 / 断点续跑（Owner）")
async def run_pipeline(
    project_id: int,
    _owner: OwnerDep,
    mode: Annotated[str | None, Query(pattern="^(auto|manual)$")] = None,
    resume: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    try:
        result = await engine.start(project_id, mode=mode, resume=resume)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    result["note"] = (
        "已断点续跑：从第一个 status != done 的环节继续，done 环节复用 output_json"
        if resume
        else "已启动：六环节按 survey → plan → plan_review → experiment → writing → review 顺序执行"
    )
    result["registry"] = registry_snapshot()
    return result


@router.post("/{project_id}/pause", summary="暂停（Owner）")
async def pause_pipeline(project_id: int, _owner: OwnerDep) -> dict[str, Any]:
    try:
        return await engine.pause(project_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/{project_id}/resume", summary="继续（Owner，断点续跑）")
async def resume_pipeline(project_id: int, _owner: OwnerDep) -> dict[str, Any]:
    try:
        return await engine.resume(project_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/{project_id}/stop", summary="中止（Owner）")
async def stop_pipeline(
    project_id: int,
    _owner: OwnerDep,
    reason: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    try:
        result = await engine.stop(project_id, reason=reason or "用户中止流水线")
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    result["note"] = "项目已转 ABORTED（终态），run 状态 failed，stop_reason=manual"
    return result


@router.post("/{project_id}/switch-mode", summary="运行中切换模式（Owner）")
async def switch_mode(
    project_id: int,
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
) -> dict[str, Any]:
    try:
        return await engine.switch_mode(project_id, str(payload.get("mode") or ""))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.post("/{project_id}/intervene", summary="人工介入（Owner）")
async def intervene(
    project_id: int,
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
    auto_resume: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    node = str(payload.get("node") or "")
    action = str(payload.get("action") or "")
    result: dict[str, Any]
    try:
        result = await engine.intervene(
            project_id,
            node=node,
            action=action,
            payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
            note=payload.get("note"),
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    if auto_resume and action in {"approve", "modify", "rerun", "downgrade"} and result.get("ready_to_resume"):
        try:
            result["resumed"] = await engine.resume(project_id)
        except Exception as exc:  # noqa: BLE001 - 介入已落库，续跑失败如实返回
            result["resumed"] = None
            result["resume_error"] = {"code": getattr(exc, "code", "resume_failed"), "message": str(exc)}
    return result


# --------------------------------------------------------------------------- #
# 读操作（public_demo 允许）
# --------------------------------------------------------------------------- #
@router.get("/{project_id}/status", summary="流水线状态")
async def pipeline_status(project_id: int) -> dict[str, Any]:
    await _ensure_project(project_id)
    try:
        return await engine.snapshot(project_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc


@router.get("/{project_id}/stages", summary="六环节产出")
async def pipeline_stages(project_id: int) -> dict[str, Any]:
    await _ensure_project(project_id)
    try:
        snapshot = await engine.snapshot(project_id, include_stages=True)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    return {
        "project_id": project_id,
        "project_status": snapshot["project_status"],
        "pipeline_run_id": (snapshot.get("run") or {}).get("id"),
        "stop_reason": snapshot.get("stop_reason"),
        "stages": snapshot.get("stages", []),
        "resume_plan": snapshot.get("resume_plan", []),
        "next_stage": snapshot.get("next_stage"),
        "stage_registry": snapshot.get("stage_registry"),
        "note": "每环节仅展示最新 attempt；历史 attempt 可用 GET /runs/{run_id} 查看",
    }


@router.get("/{project_id}/decision-logs", summary="决策日志（D1–D6）")
async def decision_logs(
    project_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    decision_point: Annotated[str | None, Query(pattern="^D[1-6]$")] = None,
) -> dict[str, Any]:
    await _ensure_project(project_id)
    async with _session_factory()() as session:
        conditions = [DecisionLog.project_id == project_id]
        if decision_point:
            conditions.append(DecisionLog.decision_point == decision_point)
        total = int(
            (await session.execute(select(func.count(DecisionLog.id)).where(*conditions))).scalar_one()
            or 0
        )
        rows = list(
            (
                await session.execute(
                    select(DecisionLog)
                    .where(*conditions)
                    .order_by(DecisionLog.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "items": [_decision_to_dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "note": "risk/confidence/reversibility 在风险策略未挂载时为 0 占位（rationale 含 stub:policy_not_mounted）",
    }


@router.get("/{project_id}/interventions", summary="介入记录（N1–N4）")
async def interventions(
    project_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    await _ensure_project(project_id)
    async with _session_factory()() as session:
        conditions = [Intervention.project_id == project_id]
        total = int(
            (await session.execute(select(func.count(Intervention.id)).where(*conditions))).scalar_one()
            or 0
        )
        rows = list(
            (
                await session.execute(
                    select(Intervention)
                    .where(*conditions)
                    .order_by(Intervention.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
    return {
        "items": [
            {
                "id": int(row.id),
                "project_id": int(row.project_id),
                "pipeline_run_id": int(row.pipeline_run_id) if row.pipeline_run_id else None,
                "node": str(row.node),
                "action": str(row.action),
                "payload": row.payload,
                "note": row.note,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "mapping": {
            "N1": "任务书审批（DRAFT → TASKBOOK_LOCKED，流水线前前置门禁）",
            "N2": "方案批准（plan_review 后，决策点 D2）",
            "N3": "结果异常确认（experiment 异常或 L2/L3 失败，决策点 D4）",
            "N4": "写作大纲确认（writing 后，决策点 D6）",
        },
    }


def _decision_to_dict(row: DecisionLog) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "project_id": int(row.project_id),
        "pipeline_run_id": int(row.pipeline_run_id) if row.pipeline_run_id else None,
        "decision_point": str(row.decision_point),
        "stage": row.stage,
        "context_digest": row.context_digest,
        "options_considered": row.options_considered,
        "chosen": str(row.chosen),
        "rationale": row.rationale,
        "risk_score": float(row.risk_score) if row.risk_score is not None else None,
        "confidence_score": float(row.confidence_score) if row.confidence_score is not None else None,
        "reversibility_score": (
            float(row.reversibility_score) if row.reversibility_score is not None else None
        ),
        "policy_action": str(row.policy_action),
        "policy_version": str(row.policy_version),
        "guardrail_checks": row.guardrail_checks,
        "cost_usd": float(row.cost_usd or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# --------------------------------------------------------------------------- #
# 运行记录（GET /pipelines/{pid}/runs → runs.py 也提供 /runs?project_id=）
# --------------------------------------------------------------------------- #
@router.get("/{project_id}/runs", summary="该项目的历史运行记录")
async def project_runs(
    project_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    await _ensure_project(project_id)
    async with _session_factory()() as session:
        active = await active_run(session, project_id)
        latest = await latest_run(session, project_id)
        total = int(
            (
                await session.execute(
                    select(func.count(PipelineRun.id)).where(PipelineRun.project_id == project_id)
                )
            ).scalar_one()
            or 0
        )
        rows = list(
            (
                await session.execute(
                    select(PipelineRun)
                    .where(PipelineRun.project_id == project_id)
                    .order_by(PipelineRun.iteration.desc(), PipelineRun.id.desc())
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
        "active_run_id": int(active.id) if active is not None else None,
        "latest_run_id": int(latest.id) if latest is not None else None,
        "orphan_run": bool(
            latest is not None
            and str(latest.status) == "running"
            and not engine.is_running(project_id)
        ),
        "note": "orphan_run=true 表示有 run 停在 running 但进程内已无对应任务（进程重启）；调用 resume 可断点续跑",
    }


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


__all__ = ["router"]
