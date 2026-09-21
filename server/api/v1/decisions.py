# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""决策与风险策略接口（WP10-T5，contracts.api_contract.key_endpoints.decisions）。

- ``POST /decisions/{id}/evaluate-risk`` —— 用当前上下文**重算**三分与策略动作
  （``persist=false`` 时只计算不落库；``persist=true`` 追加一条审计记录，需 Owner）
- ``POST /decisions/{id}/approve`` —— Owner 批准/修改/拒绝高风险决策，写 ``interventions``
  并解除 ``WAIT_HUMAN``（返回 ``ready_to_resume``，续跑仍由 ``POST /pipelines/{pid}/resume`` 负责）；
  终态项目（``DONE``/``ABORTED``）或同一 decision 的重复处置返回 409
  ``decision_already_resolved`` 且**不重复落库**（P0-1 幂等护栏）
- ``GET  /decisions/{project_id}`` —— 决策列表（供 WP15 看板；含成本双线与护栏阈值）

所有错误统一为契约错误体 ``{code,message,detail}``。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.security import OWNER_HEADER, owner_token_matches, require_owner
from db.models import DecisionLog, PipelineRun, Project
from db.session import AsyncSessionLocal
from services.pipeline import guardrails as guardrail_mod
from services.pipeline.decision_engine import (
    POLICY,
    POLICY_VERSION,
    ensure_fallback_consumer,
    fallback_events,
    intervention_node_for,
)
from services.pipeline.engine import PipelineError, engine
from services.pipeline.risk_policy import ACTION_OPTIONS, resolve_thresholds
from services.pipeline.state_machine import IllegalTransition

logger = logging.getLogger("sciloop.api.decisions")

router = APIRouter(prefix="/decisions", tags=["decisions"])

OwnerDep = Annotated[None, Depends(require_owner)]

#: 人工介入动作（contracts.enums.intervention_action）
INTERVENTION_ACTIONS: tuple[str, ...] = (
    "approve",
    "modify",
    "reject",
    "rerun",
    "downgrade",
    "abort",
    "switch_mode",
)

#: 无专属介入节点时的兜底节点（D5 迭代判据在 contracts.intervention_mapping 中无节点）
FALLBACK_NODE = "N3"


class EvaluateRiskRequest(BaseModel):
    """重算请求体：``context`` 只承载**调用方掌握的事实**（缺失项会被如实标注）。"""

    context: dict[str, Any] | None = Field(
        default=None,
        description="决策上下文（action_domain/evidence_coverage/attempt 等）；缺字段即视为缺失",
    )
    persist: bool = Field(default=False, description="是否追加一条审计用 decision_logs 记录")
    decision_point: str | None = Field(default=None, description="覆盖决策点（默认取原记录）")
    stage: str | None = Field(default=None, description="覆盖环节（默认取原记录）")


class ApproveRequest(BaseModel):
    """人工介入请求体（N1–N4；节点默认按决策点/环节自动映射）。"""

    action: str = Field(
        default="approve", description="approve/modify/reject/rerun/downgrade/abort/switch_mode"
    )
    node: str | None = Field(default=None, description="介入节点 N1–N4；缺省则按决策点映射")
    note: str | None = Field(default=None, description="人工备注（写入 interventions.note）")
    payload: dict[str, Any] | None = Field(default=None, description="人工动作的附加载荷")
    resume: bool = Field(default=False, description="是否在批准后立即触发断点续跑")


def _session_factory() -> Any:
    if AsyncSessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unavailable", "message": "数据库会话不可用", "detail": None},
        )
    return AsyncSessionLocal


def _error(code: str, message: str, http_status: int, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=http_status, detail={"code": code, "message": message, "detail": detail}
    )


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, IllegalTransition):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.to_dict())
    if isinstance(exc, PipelineError):
        http_status = {
            "project_not_found": status.HTTP_404_NOT_FOUND,
            "run_not_found": status.HTTP_404_NOT_FOUND,
            "invalid_node": status.HTTP_400_BAD_REQUEST,
            "invalid_action": status.HTTP_400_BAD_REQUEST,
            "project_aborted": status.HTTP_409_CONFLICT,
            "project_done": status.HTTP_409_CONFLICT,
            "decision_already_resolved": status.HTTP_409_CONFLICT,
            "db_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return HTTPException(status_code=http_status, detail=exc.to_dict())
    logger.exception("决策接口未预期异常")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "internal_error", "message": str(exc), "detail": None},
    )


def _decision_to_dict(row: DecisionLog) -> dict[str, Any]:
    payload = row.guardrail_checks if isinstance(row.guardrail_checks, dict) else {}
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
        "risk_score": float(row.risk_score),
        "confidence_score": float(row.confidence_score),
        "reversibility_score": float(row.reversibility_score),
        "policy_action": str(row.policy_action),
        "policy_version": str(row.policy_version),
        "guardrail_checks": payload,
        "cost_usd": float(row.cost_usd or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "node": intervention_node_for(str(row.decision_point), row.stage),
        "record_kind": payload.get("record_kind") or "policy_decision",
    }


async def _get_decision(decision_id: int) -> DecisionLog:
    async with _session_factory()() as session:
        row = (
            await session.execute(select(DecisionLog).where(DecisionLog.id == decision_id))
        ).scalar_one_or_none()
    if row is None:
        raise _error(
            "decision_not_found",
            f"decision_log {decision_id} 不存在",
            status.HTTP_404_NOT_FOUND,
            {"decision_id": decision_id},
        )
    return row


async def _run_context(pipeline_run_id: int | None) -> dict[str, Any]:
    """从 ``pipeline_runs`` 补齐 iteration/mode（真实值；取不到就留空）。"""
    if not pipeline_run_id:
        return {}
    async with _session_factory()() as session:
        run = (
            await session.execute(select(PipelineRun).where(PipelineRun.id == pipeline_run_id))
        ).scalar_one_or_none()
    if run is None:
        return {}
    return {
        "iteration": int(run.iteration or 1),
        "mode": str(run.mode),
        "pipeline_run_id": int(run.id),
    }


# --------------------------------------------------------------------------- #
# POST /decisions/{id}/evaluate-risk
# --------------------------------------------------------------------------- #
@router.post("/{decision_id}/evaluate-risk", summary="重算风险三分与策略动作")
async def evaluate_risk(
    decision_id: int,
    payload: EvaluateRiskRequest | None = None,
    x_owner_token: Annotated[str | None, Header(alias=OWNER_HEADER)] = None,
) -> dict[str, Any]:
    """重算并返回：``persist=true`` 需要 Owner（会写库），默认只计算不落库。"""
    body = payload or EvaluateRiskRequest()
    if body.persist and not owner_token_matches(x_owner_token):
        raise _error(
            "owner_token_required",
            "persist=true 会写入 decision_logs，需携带有效的 X-Owner-Token",
            status.HTTP_403_FORBIDDEN,
        )

    row = await _get_decision(decision_id)
    decision_point = (body.decision_point or str(row.decision_point)).strip().upper()
    context: dict[str, Any] = {
        "stage": body.stage or row.stage,
        "decision_point": decision_point,
    }
    context.update(await _run_context(int(row.pipeline_run_id) if row.pipeline_run_id else None))
    context.update(body.context or {})

    try:
        result = await POLICY.evaluate_decision(
            int(row.project_id), decision_point, context, persist=bool(body.persist)
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    original = {
        "risk_score": float(row.risk_score),
        "confidence_score": float(row.confidence_score),
        "reversibility_score": float(row.reversibility_score),
        "policy_action": str(row.policy_action),
        "chosen": str(row.chosen),
        "policy_version": str(row.policy_version),
    }
    recomputed = {
        "risk_score": result.get("risk_score"),
        "confidence_score": result.get("confidence_score"),
        "reversibility_score": result.get("reversibility_score"),
        "policy_action": result.get("policy_action"),
        "chosen": result.get("chosen"),
        "policy_version": result.get("policy_version"),
    }
    changed = any(original[key] != recomputed[key] for key in ("policy_action", "chosen")) or any(
        abs(float(original[key]) - float(recomputed[key] or 0)) > 1e-9
        for key in ("risk_score", "confidence_score", "reversibility_score")
    )
    return {
        "decision_id": decision_id,
        "project_id": int(row.project_id),
        "decision_point": decision_point,
        "stage": result.get("stage") or row.stage,
        "persisted": bool(body.persist),
        "new_decision_log_id": result.get("decision_log_id") if body.persist else None,
        "original": original,
        "recomputed": recomputed,
        "changed": changed,
        "result": result,
        "policy_version": POLICY_VERSION,
        "note": "重算使用 rules-only 策略：缺失特征按剩余权重归一，LLM 建议分只记录不参与判定",
    }


# --------------------------------------------------------------------------- #
# POST /decisions/{id}/approve
# --------------------------------------------------------------------------- #
@router.post("/{decision_id}/approve", summary="人工批准/处置高风险决策（Owner）")
async def approve_decision(
    decision_id: int,
    payload: ApproveRequest | None = None,
    _: OwnerDep = None,
) -> dict[str, Any]:
    """写 ``interventions``（node ∈ N1–N4）并解除 ``WAIT_HUMAN``，使流水线可恢复。"""
    body = payload or ApproveRequest()
    action = str(body.action or "").strip().lower()
    if action not in INTERVENTION_ACTIONS:
        raise _error(
            "invalid_action",
            f"未知介入动作 {action!r}（合法 {'/'.join(INTERVENTION_ACTIONS)}）",
            status.HTTP_400_BAD_REQUEST,
            {"action": action},
        )
    row = await _get_decision(decision_id)
    node = str(body.node or intervention_node_for(str(row.decision_point), row.stage) or "").strip()
    mapping_note: str | None = None
    if not node:
        node = FALLBACK_NODE
        mapping_note = (
            f"{row.decision_point} 在 contracts.intervention_mapping 中无专属节点，"
            f"按「结果异常确认」落到 {FALLBACK_NODE}"
        )
    if node not in {"N1", "N2", "N3", "N4"}:
        raise _error(
            "invalid_node",
            f"未知介入节点 {node!r}（合法 N1–N4）",
            status.HTTP_400_BAD_REQUEST,
            {"node": node},
        )

    payload_out: dict[str, Any] = {
        **(body.payload or {}),
        "decision_log_id": decision_id,
        "decision_point": str(row.decision_point),
        "policy_action": str(row.policy_action),
        "policy_version": str(row.policy_version),
        "risk_score": float(row.risk_score),
        "confidence_score": float(row.confidence_score),
        "reversibility_score": float(row.reversibility_score),
        "chosen": str(row.chosen),
        "guardrail_checks": row.guardrail_checks,
    }
    if mapping_note:
        payload_out["node_mapping_note"] = mapping_note
    note = (
        body.note
        or f"人工 {node}={action}：处置决策 {row.decision_point}（决策记录 {decision_id}）"
    )

    try:
        result = await engine.intervene(
            int(row.project_id), node=node, action=action, payload=payload_out, note=note
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    resumable = str(result.get("project_status")) == "RUNNING"
    response: dict[str, Any] = {
        "decision_id": decision_id,
        "project_id": int(row.project_id),
        "node": node,
        "action": action,
        "decision_point": str(row.decision_point),
        "intervention_id": result.get("intervention_id"),
        "project_status": result.get("project_status"),
        "pipeline_run_id": result.get("pipeline_run_id"),
        "ready_to_resume": resumable,
        "effect": result.get("effect"),
        "note": result.get("note"),
        "next_action": (
            f"POST /api/v1/pipelines/{int(row.project_id)}/resume（断点续跑，跳过 done 环节）"
            if resumable
            else f"当前状态 {result.get('project_status')} 无需续跑，详见 "
            f"/api/v1/pipelines/{int(row.project_id)}/status"
        ),
        "policy_version": POLICY_VERSION,
    }
    if body.resume and resumable:
        try:
            response["resume"] = await engine.resume(int(row.project_id))
        except Exception as exc:  # noqa: BLE001 - 批准已生效，续跑失败单独报告
            response["resume"] = {"ok": False, "error": str(exc)}
    return response


# --------------------------------------------------------------------------- #
# GET /decisions/{project_id}
# --------------------------------------------------------------------------- #
@router.get("/{project_id}", summary="决策列表（D1–D6，供看板）")
async def list_decisions(
    project_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    decision_point: Annotated[str | None, Query(pattern="^D[1-6]$")] = None,
    policy_action: Annotated[
        str | None, Query(pattern="^(auto_execute|need_human|circuit_break)$")
    ] = None,
    include_fallback_events: bool = True,
) -> dict[str, Any]:
    async with _session_factory()() as session:
        exists = (
            await session.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one_or_none()
        if exists is None:
            raise _error(
                "project_not_found", f"project {project_id} 不存在", status.HTTP_404_NOT_FOUND
            )
        conditions = [DecisionLog.project_id == project_id]
        if decision_point:
            conditions.append(DecisionLog.decision_point == decision_point)
        if policy_action:
            conditions.append(DecisionLog.policy_action == policy_action)
        total = int(
            (
                await session.execute(select(func.count(DecisionLog.id)).where(*conditions))
            ).scalar_one()
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
        audit_rows = list(
            (
                await session.execute(
                    select(DecisionLog)
                    .where(
                        DecisionLog.project_id == project_id,
                        DecisionLog.decision_point == "D4",
                    )
                    .order_by(DecisionLog.id.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    fallback_rows = [
        row
        for row in audit_rows
        if (row.guardrail_checks or {}).get("record_kind") == "llm_fallback_audit"
    ][:5]
    return {
        "items": [_decision_to_dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "policy_version": POLICY_VERSION,
        "thresholds": resolve_thresholds(),
        "guardrail_limits": guardrail_mod.guardrail_limits(),
        "options_by_decision_point": {key: list(value) for key, value in ACTION_OPTIONS.items()},
        # 降级事件以库为准（跨进程可见）；进程内环形缓冲仅作实时补充
        "fallback_events": (
            [_decision_to_dict(row) for row in fallback_rows] if include_fallback_events else []
        ),
        "fallback_events_in_process": fallback_events(limit=5) if include_fallback_events else [],
        "note": "三分可逐项复算：items[].guardrail_checks 含特征权重、缺失项与护栏短路证据",
    }


# --------------------------------------------------------------------------- #
# 订阅降级事件（模块导入即生效；main.py 挂载本模块时自动完成）
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - 导入期副作用，失败不影响接口可用性
    ensure_fallback_consumer()
except Exception:  # noqa: BLE001
    logger.warning("llm_fallback 订阅失败（降级留痕可能缺失）", exc_info=True)


__all__ = ["router"]
