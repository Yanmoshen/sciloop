# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""策略引擎（WP10-T3/T4/T6，计划书 §2.4）。

对外唯一入口（WP09 引擎懒加载本模块）：

``evaluate_decision(project_id, decision_point, context) -> dict``

返回体满足 WP09 的接口约定（``policy_action`` / 三分 / ``rationale`` / ``policy_version`` /
``guardrail_checks`` / ``options_considered`` / ``chosen`` / ``decision_log_id``），引擎拿到
``decision_log_id`` 后**不再重复插入**（避免一条决策两条记录）。

判定顺序（contracts.risk_policy_rules.action_rules）：

1. 三类硬护栏短路判定（safety → time → cost，见 :mod:`.guardrails`）；任一失败 → ``circuit_break``
2. 重试次数耗尽 → ``circuit_break``
3. ``risk<=30 且 confidence>=0.75 且 reversibility>=0.60`` → ``auto_execute``
4. 其余（护栏通过）→ ``need_human``

同时本模块消费 WP02 的 ``llm_fallback`` 事件（:func:`ensure_fallback_consumer`）：
主模型失败切备用模型是**一次真实的 D4 决策**，必须补写 ``decision_logs``。
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import Mapping
from typing import Any

from app.services.pipeline import guardrails as guardrail_mod
from app.services.pipeline import risk_policy as risk_mod

logger = logging.getLogger("sciloop.pipeline.decision")

WP_ID = "WP10"

#: 策略实现版本（写入 decision_logs.policy_version，<=32 字符）
POLICY_VERSION = risk_mod.POLICY_VERSION

#: 决策点合法集合（contracts.enums.decision_point）
DECISION_POINTS: tuple[str, ...] = ("D1", "D2", "D3", "D4", "D5", "D6")

#: 环节 → 决策点（与 WP09 stages/base.py 的 STAGE_DECISION_POINT 一致；plan 无决策点）
STAGE_DECISION_POINT: dict[str, str | None] = {
    "survey": "D1",
    "plan": None,
    "plan_review": "D2",
    "experiment": "D3",
    "writing": "D6",
    "review": "D5",
}

#: 环节 → 人工介入节点（contracts.intervention_mapping：N2/D2、N3/D4、N4/D6）
STAGE_INTERVENTION_NODE: dict[str, str | None] = {
    "survey": None,
    "plan": None,
    "plan_review": "N2",
    "experiment": "N3",
    "writing": "N4",
    "review": None,
}

#: 决策点 → 介入节点（approve 时用于落 interventions.node）
DECISION_POINT_INTERVENTION_NODE: dict[str, str | None] = {
    "D1": None,
    "D2": "N2",
    "D3": None,  # D3 无专属节点；由环节映射（experiment → N3）决定
    "D4": "N3",
    "D5": None,
    "D6": "N4",
}

#: ``action_plan`` 中会被护栏校验的动作字段
ACTION_PLAN_KEYS: tuple[str, ...] = (
    "template_id",
    "params",
    "sample_size",
    "estimated_cost_usd",
    "run_timeout_seconds",
    "stage_timeout_seconds",
    "egress_hosts",
    "proposed_action",
    "failure_action",
)

#: 最近一次降级事件（供 API/看板展示；DB 留痕失败时的兜底可见性）
_FALLBACK_EVENTS: deque[dict[str, Any]] = deque(maxlen=200)
_FALLBACK_CONSUMER_INSTALLED = False


def _settings_value(name: str, default: Any) -> Any:
    try:
        from app.core.config import get_settings

        return getattr(get_settings(), name, default)
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- #
# 上下文 → action_plan
# --------------------------------------------------------------------------- #
def normalize_decision_point(decision_point: str | None, context: Mapping[str, Any] | None) -> str:
    """归一决策点：显式值优先，否则按环节推断；未知值不猜测（回落 D1 并标注）。"""
    point = str(decision_point or "").strip().upper()
    if point in DECISION_POINTS:
        return point
    stage = str((context or {}).get("stage") or "").strip().lower()
    inferred = STAGE_DECISION_POINT.get(stage)
    if inferred:
        return inferred
    logger.warning("未知决策点 %r（stage=%r），按 D1 记录并标注", decision_point, stage)
    return "D1"


def build_action_plan(
    decision_point: str,
    context: Mapping[str, Any] | None,
    *,
    stage: str | None = None,
) -> dict[str, Any]:
    """把决策上下文里的动作域字段抽成待校验的 ``action_plan``（不虚构字段）。"""
    ctx = dict(context or {})
    domain = ctx.get("action_domain")
    plan: dict[str, Any] = {"decision_point": decision_point}
    if stage:
        plan["stage"] = stage
    elif ctx.get("stage"):
        plan["stage"] = ctx["stage"]
    if isinstance(domain, Mapping):
        for key, value in domain.items():
            plan[str(key)] = value
    for key in ACTION_PLAN_KEYS:
        if key in ctx and ctx[key] is not None:
            plan.setdefault(key, ctx[key])
    return plan


def action_specified(decision_point: str, action_plan: Mapping[str, Any]) -> bool:
    """动作域是否已声明（未声明时不做「假装有动作」的校验，也不编造输入完整度）。"""
    fields = risk_mod.ACTION_DOMAIN_FIELDS.get(decision_point, ())
    plan = dict(action_plan or {})
    return any(plan.get(name) not in (None, "", [], {}) for name in fields)


def resolve_retry_exhausted(context: Mapping[str, Any] | None, *, attempt: int | None) -> bool:
    """重试次数是否耗尽（``PIPELINE_MAX_RETRY`` 默认 2 → 最多 3 次尝试）。"""
    ctx = dict(context or {})
    for key in ("retry_exhausted", "attempts_exhausted", "retry_exhausted_by_engine"):
        if ctx.get(key) is True:
            return True
    max_retry = int(_settings_value("pipeline_max_retry", 2))
    current = attempt if attempt is not None else ctx.get("attempt")
    if isinstance(current, int) and not isinstance(current, bool):
        return current > max_retry + 1
    return False


# --------------------------------------------------------------------------- #
# 历史成功率（真实数据；查不到就缺失）
# --------------------------------------------------------------------------- #
async def historical_success_rate(project_id: int | None, stage: str | None) -> dict[str, Any]:
    """由 ``stage_outputs`` 计算**本项目**同环节历史成功率。

    只统计本项目：跨项目混用会把别的工作包验收时的实验数据当成自己的历史，
    口径不一致（宁缺毋滥）；样本不足 → ``value=None`` 并如实标注来源与样本数。
    """
    if not stage:
        return {"value": None, "samples": 0, "source": "unavailable:no_stage", "note": None}
    if project_id is None:
        return {"value": None, "samples": 0, "source": "unavailable:no_project", "note": None}
    try:
        from sqlalchemy import select

        from app.db.models import PipelineRun, StageOutput
        from app.db.session import AsyncSessionLocal
    except Exception as exc:  # noqa: BLE001 - 记账层不可用时如实缺失
        return {
            "value": None,
            "samples": 0,
            "source": "unavailable:db_import",
            "note": f"{type(exc).__name__}: {exc}",
        }
    if AsyncSessionLocal is None:
        return {"value": None, "samples": 0, "source": "unavailable:db_unavailable", "note": None}

    async def _fetch(scope_project: int | None) -> list[str]:
        async with AsyncSessionLocal() as session:
            stmt = select(StageOutput.status).where(StageOutput.stage == stage)
            if scope_project is not None:
                stmt = stmt.join(PipelineRun, StageOutput.pipeline_run_id == PipelineRun.id).where(
                    PipelineRun.project_id == scope_project
                )
            rows = (await session.execute(stmt)).scalars().all()
            return [str(item) for item in rows]

    try:
        rows = await _fetch(project_id)
        scope = "project"
    except Exception as exc:  # noqa: BLE001
        logger.warning("历史成功率查询失败 stage=%s", stage, exc_info=True)
        return {
            "value": None,
            "samples": 0,
            "source": "unavailable:query_failed",
            "note": f"{type(exc).__name__}: {exc}",
        }
    result = risk_mod.historical_success_rate_from_rows(rows)
    result["source"] = f"db:stage_outputs[{scope}]"
    return result


# --------------------------------------------------------------------------- #
# 入库（decision_logs）
# --------------------------------------------------------------------------- #
def _clamp_score(value: Any, *, low: float, high: float, digits: int) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(low, min(high, number)), digits)


def _digest(
    context: Mapping[str, Any], *, decision_point: str, action_plan: Mapping[str, Any]
) -> str:
    ctx = dict(context or {})
    upstream = ctx.get("upstream_stages") or []
    if isinstance(upstream, (list, tuple, set)):
        upstream_text = ",".join(sorted(str(item) for item in upstream)) or "无"
    else:
        upstream_text = str(upstream)
    action_keys = sorted(
        key for key in risk_mod.ACTION_DOMAIN_FIELDS.get(decision_point, ()) if key in action_plan
    )
    return (
        f"{decision_point} 位于环节={ctx.get('stage') or action_plan.get('stage') or '未知'}，"
        f"第{ctx.get('attempt') or 1}次尝试，iteration={ctx.get('iteration') or 1}，"
        f"mode={ctx.get('mode') or '未知'}，上游已完成={upstream_text}；"
        f"动作域字段={','.join(action_keys) if action_keys else '未声明'}；"
        f"taskbook_id={ctx.get('taskbook_id')}，pipeline_run_id={ctx.get('pipeline_run_id')}"
    )


async def write_decision_log(
    *,
    project_id: int | None,
    pipeline_run_id: int | None,
    decision_point: str,
    stage: str | None,
    context_digest: str,
    options_considered: list[str],
    chosen: str,
    rationale: str,
    risk_score: Any,
    confidence_score: Any,
    reversibility_score: Any,
    policy_action: str,
    policy_version: str,
    guardrail_checks: Mapping[str, Any],
    cost_usd: Any = 0.0,
) -> dict[str, Any]:
    """写一条 ``decision_logs``；失败时**返回错误而不是静默丢弃**。"""
    if project_id is None:
        return {"decision_log_id": None, "error": "project_id_required"}
    try:
        from app.db.models import DecisionLog
        from app.db.session import AsyncSessionLocal
    except Exception as exc:  # noqa: BLE001
        return {"decision_log_id": None, "error": f"db_import_failed:{type(exc).__name__}"}
    if AsyncSessionLocal is None:
        return {"decision_log_id": None, "error": "db_unavailable"}

    record = DecisionLog(
        project_id=int(project_id),
        pipeline_run_id=int(pipeline_run_id) if pipeline_run_id else None,
        decision_point=decision_point,
        stage=(stage or None),
        context_digest=str(context_digest)[:4000],
        options_considered=list(options_considered or [chosen]),
        chosen=str(chosen)[:64],
        rationale=str(rationale)[:4000],
        risk_score=_clamp_score(risk_score, low=0.0, high=100.0, digits=2),
        confidence_score=_clamp_score(confidence_score, low=0.0, high=1.0, digits=3),
        reversibility_score=_clamp_score(reversibility_score, low=0.0, high=1.0, digits=3),
        policy_action=policy_action,
        policy_version=str(policy_version)[:32],
        guardrail_checks=dict(guardrail_checks or {}),
        cost_usd=_clamp_score(cost_usd, low=0.0, high=999999.0, digits=4),
    )
    try:
        async with AsyncSessionLocal() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return {"decision_log_id": int(record.id), "error": None}
    except Exception as exc:  # noqa: BLE001 - 写库失败不得让流水线失控，但必须可见
        logger.warning(
            "decision_logs 写入失败 project_id=%s decision_point=%s：%s",
            project_id,
            decision_point,
            exc,
            exc_info=True,
        )
        return {"decision_log_id": None, "error": f"insert_failed:{type(exc).__name__}:{exc}"}


# --------------------------------------------------------------------------- #
# 策略引擎
# --------------------------------------------------------------------------- #
class RiskPolicyEngine:
    """规则层策略引擎（LLM 无法覆盖其结论）。"""

    name = "wp10_risk_policy"
    version = POLICY_VERSION
    mounted = True

    async def evaluate_decision(
        self,
        project_id: int | None,
        decision_point: str | None,
        context: Mapping[str, Any] | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        ctx = dict(context or {})
        point = normalize_decision_point(decision_point, ctx)
        stage = str(ctx.get("stage") or "").strip() or None

        plan = build_action_plan(point, ctx, stage=stage)
        specified = action_specified(point, plan)
        retry_exhausted = resolve_retry_exhausted(ctx, attempt=ctx.get("attempt"))

        # ① 硬护栏（短路）
        estimated = plan.get("estimated_cost_usd")
        guardrail = await guardrail_mod.check_guardrails(
            plan,
            project_id=project_id,
            estimated_cost_usd=estimated if isinstance(estimated, (int, float)) else None,
        )
        guardrail_failed = guardrail["ok"] is False

        # ② 三分（护栏里的真实成本作为 cost_exposure 的测量输入）
        history = await historical_success_rate(project_id, stage)
        pre_chosen = (
            "circuit_break"
            if guardrail_failed
            else risk_mod.choose_action(
                point,
                policy_action="auto_execute",
                context=ctx,
                action_specified=specified,
            )
        )
        scores = risk_mod.compute_scores(
            point,
            ctx,
            guardrail=guardrail,
            historical_success_rate=history.get("value"),
            chosen=pre_chosen,
            action_specified=specified,
        )

        # ③ 策略动作（规则层最终决定）
        decision = risk_mod.decide_action(
            risk_score=scores["risk_score"],
            confidence_score=scores["confidence_score"],
            reversibility_score=scores["reversibility_score"],
            guardrail_failed=guardrail_failed,
            guardrail_first_failure=guardrail.get("first_failure"),
            retry_exhausted=retry_exhausted,
            thresholds=scores["thresholds"],
        )
        chosen = risk_mod.choose_action(
            point,
            policy_action=decision["policy_action"],
            context=ctx,
            action_specified=specified,
        )
        rationale = risk_mod.build_rationale(
            decision_point=point,
            decision=decision,
            scores=scores,
            guardrail=guardrail,
            extra=(
                f"历史成功率={history.get('value')}（样本 {history.get('samples')}，"
                f"{history.get('source')}）"
            ),
        )
        digest = _digest(ctx, decision_point=point, action_plan=plan)
        scores_unavailable = scores["risk_score"] is None or scores["confidence_score"] is None
        if scores_unavailable:
            rationale = f"{rationale}｜风险/置信度存在缺失，入库占位 0（禁止用 0 冒充真实评分）"

        cost_usd = (
            plan.get("estimated_cost_usd")
            if isinstance(plan.get("estimated_cost_usd"), (int, float))
            else 0.0
        )
        checks = {
            "safety_ok": guardrail.get("safety_ok"),
            "time_ok": guardrail.get("time_ok"),
            "cost_ok": guardrail.get("cost_ok"),
            "policy_mounted": True,
            "guardrail_version": guardrail.get("version"),
            "guardrail_evaluated": list(guardrail.get("evaluated") or []),
            "guardrail_skipped": list(guardrail.get("skipped") or []),
            "guardrail_first_failure": guardrail.get("first_failure"),
            "short_circuited": bool(guardrail.get("short_circuited")),
            "guardrail_counters": dict(guardrail.get("counters") or {}),
            "cost_check": (guardrail.get("detail") or {}).get("cost", {}).get("check"),
            "action_specified": specified,
            "retry_exhausted": retry_exhausted,
            "scores_unavailable": scores_unavailable,
            "missing_features": list(scores.get("missing_features") or []),
            "historical_success_rate": {
                "value": history.get("value"),
                "samples": history.get("samples"),
                "source": history.get("source"),
            },
            "thresholds": scores["thresholds"],
            "llm_suggestion": (scores.get("features") or {}).get("llm_suggestion"),
        }
        if guardrail_failed:
            checks.update(guardrail_mod.failure_report_hint(guardrail))

        decision_log_id: int | None = None
        log_error: str | None = None
        if persist:
            written = await write_decision_log(
                project_id=project_id,
                pipeline_run_id=ctx.get("pipeline_run_id"),
                decision_point=point,
                stage=stage,
                context_digest=digest,
                options_considered=list(decision["options_considered"]),
                chosen=chosen,
                rationale=rationale,
                risk_score=scores["risk_score"],
                confidence_score=scores["confidence_score"],
                reversibility_score=scores["reversibility_score"],
                policy_action=decision["policy_action"],
                policy_version=POLICY_VERSION,
                guardrail_checks=checks,
                cost_usd=cost_usd,
            )
            decision_log_id = written.get("decision_log_id")
            log_error = written.get("error")

        return {
            "policy_action": decision["policy_action"],
            "risk_score": scores["risk_score"],
            "confidence_score": scores["confidence_score"],
            "reversibility_score": scores["reversibility_score"],
            "features": scores["features"],
            "reason": decision["reason"],
            "rationale": rationale,
            "policy_version": POLICY_VERSION,
            "chosen": chosen,
            "options_considered": list(decision["options_considered"]),
            "guardrail_checks": checks,
            "guardrails": {
                "safety_ok": guardrail.get("safety_ok"),
                "time_ok": guardrail.get("time_ok"),
                "cost_ok": guardrail.get("cost_ok"),
                "ok": guardrail.get("ok"),
                "first_failure": guardrail.get("first_failure"),
                "evaluated": list(guardrail.get("evaluated") or []),
                "skipped": list(guardrail.get("skipped") or []),
                "short_circuited": bool(guardrail.get("short_circuited")),
                "detail": guardrail.get("detail"),
                "limits": guardrail.get("limits"),
            },
            "conditions": decision.get("conditions"),
            "decision_point": point,
            "stage": stage,
            "decision_log_id": decision_log_id,
            "decision_log_error": log_error,
            "context_digest": digest,
            "missing_features": list(scores.get("missing_features") or []),
            "degraded": False,
            "recomputed": False,
        }


POLICY = RiskPolicyEngine()


def get_policy() -> RiskPolicyEngine:
    """WP09 引擎懒加载入口。"""
    return POLICY


async def evaluate_decision(
    project_id: int | None,
    decision_point: str | None,
    context: Mapping[str, Any] | None = None,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """模块级入口（WP09 的 :func:`_resolve_policy` 可直接取用）。"""
    return await POLICY.evaluate_decision(project_id, decision_point, context, persist=persist)


# --------------------------------------------------------------------------- #
# T6：消费 LLM 降级事件 → 补写 D4 决策留痕
# --------------------------------------------------------------------------- #
def _fallback_choice(payload: Mapping[str, Any]) -> str:
    """降级处置的动作名：配置了备用模型 → ``switch_model``；否则 ``retry``。"""
    to_ref = str(payload.get("to_model_ref") or "").strip()
    from_ref = str(payload.get("from_model_ref") or "").strip()
    if to_ref and to_ref != from_ref:
        return "switch_model"
    return "retry"


async def record_fallback_decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把一次 ``llm_fallback`` 事件补写成 D4 决策记录（真实上下文，不编造三分）。"""
    data = dict(payload or {})
    project_id = data.get("project_id")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        project_id = None
    stage = str(data.get("stage") or "").strip() or None
    chosen = _fallback_choice(data)
    run_id, mode, iteration, upstream = await _fallback_context(project_id, stage)

    context = {
        "stage": stage,
        "attempt": 1,
        "iteration": iteration or 1,
        "mode": mode or "auto",
        "upstream_stages": upstream,
        "pipeline_run_id": run_id,
        "failure_action": chosen,
        "failure_level": "L1",
        "proposed_action": chosen,
    }
    result = await POLICY.evaluate_decision(project_id, "D4", context, persist=False)

    digest = (
        f"模型降级事件（llm_fallback）：{data.get('from_model_ref')} → {data.get('to_model_ref')}；"
        f"原因={data.get('reason')}，error_kind={data.get('error_kind')}，"
        f"环节={stage}，project_id={project_id}，pipeline_run_id={run_id}；"
        "该处置已由 LLM 路由自动执行，本记录为事后留痕（D4）"
    )
    rationale = (
        f"{result['rationale']}｜处置动作={chosen}（路由层已自动完成模型切换，"
        "此条为降级路径的决策留痕，非实时决策前审批）"
    )
    checks = dict(result["guardrail_checks"])
    checks.update(
        {
            "record_kind": "llm_fallback_audit",
            "fallback_from": data.get("from_model_ref"),
            "fallback_to": data.get("to_model_ref"),
            "fallback_reason": data.get("reason"),
            "error_kind": data.get("error_kind"),
        }
    )
    written = await write_decision_log(
        project_id=project_id,
        pipeline_run_id=run_id,
        decision_point="D4",
        stage=stage,
        context_digest=digest,
        options_considered=[
            "retry",
            "switch_model",
            "downgrade",
            "switch_template",
            "circuit_break",
        ],
        chosen=chosen,
        rationale=rationale,
        risk_score=result["risk_score"],
        confidence_score=result["confidence_score"],
        reversibility_score=result["reversibility_score"],
        policy_action=result["policy_action"],
        policy_version=POLICY_VERSION,
        guardrail_checks=checks,
        cost_usd=0.0,
    )
    record = {
        "event": "llm_fallback",
        "project_id": project_id,
        "pipeline_run_id": run_id,
        "stage": stage,
        "chosen": chosen,
        "policy_action": result["policy_action"],
        "risk_score": result["risk_score"],
        "confidence_score": result["confidence_score"],
        "reversibility_score": result["reversibility_score"],
        "policy_version": POLICY_VERSION,
        "decision_log_id": written.get("decision_log_id"),
        "decision_log_error": written.get("error"),
        "from_model_ref": data.get("from_model_ref"),
        "to_model_ref": data.get("to_model_ref"),
        "reason": data.get("reason"),
        "error_kind": data.get("error_kind"),
        "created_at": _now_iso(),
    }
    _FALLBACK_EVENTS.append(record)
    logger.info(
        "已消费 llm_fallback 事件并补写 D4 决策留痕 project_id=%s decision_log_id=%s chosen=%s",
        project_id,
        record["decision_log_id"],
        chosen,
    )
    return record


async def _fallback_context(
    project_id: int | None, stage: str | None
) -> tuple[int | None, str | None, int | None, list[str]]:
    """补全降级决策所需上下文（真实库内数据；取不到则留空）。"""
    if project_id is None:
        return None, None, None, []
    try:
        from sqlalchemy import select

        from app.db.models import PipelineRun, Project, StageOutput
        from app.db.session import AsyncSessionLocal
    except Exception:  # noqa: BLE001
        return None, None, None, []
    if AsyncSessionLocal is None:
        return None, None, None, []
    try:
        async with AsyncSessionLocal() as session:
            project = (
                await session.execute(select(Project).where(Project.id == project_id))
            ).scalar_one_or_none()
            run = (
                await session.execute(
                    select(PipelineRun)
                    .where(PipelineRun.project_id == project_id)
                    .order_by(PipelineRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            upstream: list[str] = []
            if run is not None:
                rows = (
                    await session.execute(
                        select(StageOutput.stage)
                        .where(
                            StageOutput.pipeline_run_id == int(run.id),
                            StageOutput.status == "done",
                        )
                        .order_by(StageOutput.id)
                    )
                ).scalars()
                upstream = sorted({str(item) for item in rows})
            return (
                int(run.id) if run is not None else None,
                str(project.mode) if project is not None else None,
                int(run.iteration) if run is not None else None,
                upstream,
            )
    except Exception:  # noqa: BLE001 - 上下文补全失败不影响留痕
        logger.warning("降级决策上下文补全失败 project_id=%s", project_id, exc_info=True)
        return None, None, None, []


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def _on_llm_fallback(event_type: str, payload: dict[str, Any]) -> None:
    if event_type != "llm_fallback":
        return
    try:
        await record_fallback_decision(payload or {})
    except Exception:  # noqa: BLE001 - 留痕失败不得影响 LLM 主流程
        logger.warning("llm_fallback 决策留痕失败 payload=%s", payload, exc_info=True)


def ensure_fallback_consumer(bus: Any = None) -> bool:
    """幂等订阅 ``default_bus`` 的 ``llm_fallback`` 事件（T6）。"""
    global _FALLBACK_CONSUMER_INSTALLED
    if os.environ.get("WP10_DISABLE_FALLBACK_CONSUMER") == "1":
        return False
    try:
        from app.llm.events import default_bus
    except Exception:  # noqa: BLE001 - LLM 层不可用时不阻塞导入
        return False
    target = bus if bus is not None else default_bus
    if _FALLBACK_CONSUMER_INSTALLED and bus is None:
        return True
    target.subscribe(_on_llm_fallback)
    _FALLBACK_CONSUMER_INSTALLED = True
    logger.info("WP10 已订阅 llm_fallback 事件（降级留痕 D4）")
    return True


def fallback_events(limit: int = 20) -> list[dict[str, Any]]:
    """最近消费到的降级事件（含落库结果，供看板与验收核对）。"""
    return list(_FALLBACK_EVENTS)[-limit:]


def intervention_node_for(decision_point: str | None, stage: str | None) -> str | None:
    """介入节点解析：环节映射优先（N2/N3/N4），其次决策点映射（``contracts``）。"""
    point = str(decision_point or "").strip().upper()
    stage_key = str(stage or "").strip().lower()
    if stage_key in STAGE_INTERVENTION_NODE and STAGE_INTERVENTION_NODE[stage_key]:
        return STAGE_INTERVENTION_NODE[stage_key]
    return DECISION_POINT_INTERVENTION_NODE.get(point)


__all__ = [
    "DECISION_POINTS",
    "POLICY",
    "POLICY_VERSION",
    "RiskPolicyEngine",
    "action_specified",
    "build_action_plan",
    "ensure_fallback_consumer",
    "evaluate_decision",
    "fallback_events",
    "get_policy",
    "historical_success_rate",
    "intervention_node_for",
    "normalize_decision_point",
    "record_fallback_decision",
    "resolve_retry_exhausted",
    "write_decision_log",
]
