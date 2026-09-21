# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""失败分级与《失败分析报告》（WP09-T6，计划书 §2.5.2）。

======  ============================================  =========================================
等级     判定                                           自动处置
======  ============================================  =========================================
**L1**   API 超时 / 网络抖动 / 格式解析失败            自动重试 ``max_retry``（默认 2）次
**L2**   指标不达标 / 检索为空 / ``plan_review=revise`` 自动降级：缩样本 / 降目标 / 换检索式 /
                                                        回退 plan（全局最多 1 次）
**L3**   连续 max_retry 次失败 / 触发护栏 / 执行器异常   熔断 + 《失败分析报告》+ 项目转 WAIT_HUMAN
======  ============================================  =========================================

**本模块不做风险/护栏判定**（归 WP10）：L3 的护栏来源是 WP10 的 ``policy_action='circuit_break'``
或本模块的「重试/降级次数耗尽」判定；两者都如实写入 Decision Log（D4）。

《失败分析报告》包含：失败环节、失败原因链、已尝试的处置、成本消耗、建议的人工动作。
报告是**真实数据聚合**（stage_outputs / decision_logs / cost 服务），不含任何推测数值。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DecisionLog, PipelineRun, Project, StageOutput
from services.pipeline.stages.base import (
    STAGE_ORDER,
    StageContext,
    StageError,
    StageValidationError,
)

logger = logging.getLogger("sciloop.pipeline.failure_handler")

WP_ID = "WP09"

FAILURE_LEVELS: tuple[str, ...] = ("L1", "L2", "L3")

#: 每个环节允许的 L2 降级次数上限（超过即按 L3 熔断）
L2_MAX_PER_STAGE = 2

#: plan 回退的全局硬上限（附录 D.3：``revise`` 全局最多 1 次）
PLAN_REVERT_MAX = 1

REPORT_VERSION = "v1"

#: D4 失败处置的可选动作域（contracts.decision_points.D4）
D4_OPTIONS: tuple[str, ...] = ("retry", "downgrade", "switch_template", "circuit_break")


@dataclass
class FailureAssessment:
    """一次失败的评估结果（engine 据此决定下一步）。"""

    level: str
    action: str
    stage: str
    attempt: int
    error_code: str
    reason: str
    retryable: bool
    degrade: dict[str, Any] = field(default_factory=dict)
    revert_to: str | None = None
    suggestions: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_circuit_break(self) -> bool:
        return self.level == "L3" or self.action == "circuit_break"

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "stage": self.stage,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "reason": self.reason,
            "retryable": self.retryable,
            "degrade": self.degrade,
            "revert_to": self.revert_to,
            "suggestions": list(self.suggestions),
            "detail": self.detail,
        }

    def d4_context_digest(self) -> str:
        return (
            f"第{self.attempt}次尝试，环节={self.stage}，失败码={self.error_code}，"
            f"分级={self.level}，原因：{self.reason}"
        )


# --------------------------------------------------------------------------- #
# 分级
# --------------------------------------------------------------------------- #
def classify_exception(exc: BaseException) -> tuple[str, str]:
    """异常 → ``(level, error_code)``。

    - 超时 / 限流 / 服务端 5xx / JSON 解析失败 / 网络异常 → **L1**（可重试）
    - 环节校验失败（质量类）→ 由异常自带 ``level``
    - 模型路由缺失 / 盲评隔离失败 / 回放未命中 / 护栏异常 / 未实现环节 → **L3**（不可自愈）
    """
    if isinstance(exc, StageValidationError):
        return (str(getattr(exc, "level", "L1") or "L1"), getattr(exc, "code", "stage_validation_failed"))
    if isinstance(exc, StageError):
        return (str(getattr(exc, "level", "L1") or "L1"), getattr(exc, "code", "stage_error"))

    try:
        from llm.errors import (
            IsolationViolation,
            LLMAuthError,
            LLMBadRequestError,
            LLMError,
            LLMJSONValidationError,
            LLMRateLimitError,
            LLMServerError,
            LLMTimeoutError,
            ModelRoutingError,
            ReplayMissError,
            SecretBackendUnavailable,
        )
    except Exception:  # pragma: no cover - WP02 缺失
        LLMError = ()  # type: ignore[assignment]
        IsolationViolation = ModelRoutingError = ReplayMissError = SecretBackendUnavailable = ()  # type: ignore[assignment]
        LLMTimeoutError = LLMRateLimitError = LLMServerError = LLMJSONValidationError = LLMError  # type: ignore[assignment]
        LLMAuthError = LLMBadRequestError = ()  # type: ignore[assignment]

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return ("L1", type(exc).__name__)
    if isinstance(exc, LLMTimeoutError):
        return ("L1", "llm_timeout")
    if isinstance(exc, LLMRateLimitError):
        return ("L1", "llm_rate_limit")
    if isinstance(exc, LLMServerError):
        return ("L1", "llm_server_error")
    if isinstance(exc, LLMJSONValidationError):
        return ("L1", "llm_json_invalid")
    if isinstance(exc, LLMAuthError):
        return ("L1", "llm_auth")  # 鉴权问题重试无意义，但先按 L1 计一次；次数耗尽即 L3
    if isinstance(exc, LLMBadRequestError):
        return ("L1", "llm_bad_request")
    if isinstance(exc, (ModelRoutingError, IsolationViolation, ReplayMissError, SecretBackendUnavailable)):
        return ("L3", type(exc).__name__)
    if isinstance(exc, LLMError):
        return ("L1", getattr(exc, "kind", "llm_error"))
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ("L1", f"data_error:{type(exc).__name__}")
    return ("L3", f"{type(exc).__name__}")


def assess_failure(
    exc: BaseException | None,
    *,
    ctx: StageContext,
    level: str | None = None,
    reason: str | None = None,
    quality_issues: list[str] | None = None,
    result_metrics: dict[str, Any] | None = None,
) -> FailureAssessment:
    """评估失败并给出处置动作。

    ``exc=None`` 表示**非异常类失败**（质量不达标：如 ``methods < 2``、检索为空、
    ``plan_review=revise``、实验指标不达标），此时 ``level`` 必填（通常 L2）。
    """
    max_retry = int(ctx.extras.get("max_retry", 2) or 0)
    l2_used = int(ctx.extras.get("l2_used", 0) or 0)
    revert_used = int(ctx.extras.get("revert_used", 0) or 0)
    attempt = int(ctx.attempt)

    if exc is not None:
        detected_level, error_code = classify_exception(exc)
        detail: dict[str, Any] = {}
        if isinstance(exc, StageError) and exc.detail is not None:
            detail = {"detail": exc.detail} if not isinstance(exc.detail, dict) else dict(exc.detail)
        message = str(exc)
    else:
        detected_level = str(level or "L2")
        error_code = "quality_gate_failed"
        detail = {"quality_issues": list(quality_issues or [])}
        if result_metrics:
            detail["metrics"] = result_metrics
        message = reason or "; ".join(quality_issues or []) or "环节产出未达质量门槛"

    effective_level = detected_level if level is None else level

    # ---- 决策点 D4 的处置选择 ----
    retries_used = max(0, attempt - 1)
    revert_to: str | None = None
    if effective_level == "L1" and retries_used < max_retry:
        action = "retry"
        degrade: dict[str, Any] = {}
        suggestions = [f"自动重试（已用 {retries_used}/{max_retry} 次）"]
    elif effective_level == "L2" and l2_used < L2_MAX_PER_STAGE:
        action, degrade, revert_to, suggestions = _plan_degrade(ctx, reason=message)
        if revert_to == "plan" and revert_used >= PLAN_REVERT_MAX:
            action, revert_to = "switch_template", None
            suggestions.append("plan 回退已达全局上限 1 次，改用 switch_template")
    else:
        if effective_level == "L1":
            message = f"连续 {attempt} 次失败（max_retry={max_retry} 已耗尽）：{message}"
        elif effective_level == "L2":
            message = f"L2 降级次数已用尽（{l2_used}/{L2_MAX_PER_STAGE}）：{message}"
        action = "circuit_break"
        degrade = {}
        suggestions = ["按 L3 熔断，生成《失败分析报告》并转 WAIT_HUMAN"]

    assessment = FailureAssessment(
        level="L3" if action == "circuit_break" else effective_level,
        action=action,
        stage=ctx.stage,
        attempt=attempt,
        error_code=error_code,
        reason=message,
        retryable=action in {"retry", "downgrade", "switch_template"},
        degrade=degrade,
        revert_to=revert_to if action == "downgrade" else None,
        suggestions=suggestions + suggest_human_actions(ctx.stage, effective_level, error_code),
        detail=detail,
    )
    if action == "circuit_break":
        assessment.degrade = {}
    logger.info(
        json.dumps(
            {
                "event": "failure_assessed",
                "wp_id": WP_ID,
                "project_id": ctx.project_id,
                "run_id": ctx.pipeline_run_id,
                "stage": ctx.stage,
                "attempt": attempt,
                **{k: v for k, v in assessment.to_dict().items() if k != "detail"},
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return assessment


def _plan_degrade(
    ctx: StageContext, *, reason: str
) -> tuple[str, dict[str, Any], str | None, list[str]]:
    """L2 降级策略（计划书 §2.5.2：缩样本 / 降目标 / 换检索式 / 回退 plan）。"""
    stage = ctx.stage
    suggestions: list[str] = []
    if stage == "survey":
        queries = _alternate_queries(ctx)
        degrade = {
            "strategy": "alternate_queries",
            "queries": queries,
            "paper_limit": max(5, int(ctx.degrade.get("paper_limit", 20) or 20) // 2),
        }
        suggestions.append(f"更换检索式（{len(queries)} 条）并缩小 paper_limit")
        return "downgrade", degrade, None, suggestions
    if stage in {"plan", "plan_review"}:
        degrade = {"strategy": "revert_plan", "reason": reason}
        suggestions.append("回退 plan 重新生成候选方案（全局最多 1 次）")
        return "downgrade", degrade, "plan", suggestions
    if stage == "experiment":
        sample = ctx.degrade.get("sample_size")
        try:
            sample_size = max(1, int(sample) // 2)
        except (TypeError, ValueError):
            sample_size = 8
        degrade = {"strategy": "shrink_sample", "sample_size": min(50, sample_size)}
        suggestions.append(f"缩小样本量至 {degrade['sample_size']} 并保留原模板")
        return "downgrade", degrade, None, suggestions
    if stage == "review":
        return "downgrade", {"strategy": "lower_target", "note": "降低本轮目标，按当前水平继续"}, None, [
            "降低目标（不修改任务书阈值，仅记录降级）"
        ]
    if stage == "writing":
        return "downgrade", {"strategy": "shrink_scope", "note": "收缩写作范围，优先输出有证据支撑的章节"}, None, [
            "收缩写作范围，保证 Claim 证据完整"
        ]
    return "downgrade", {"strategy": "generic", "reason": reason}, None, ["按通用降级策略重试一次"]


def _alternate_queries(ctx: StageContext) -> list[str]:
    """由任务书确定性派生替代检索式（可复现、非编造）。"""
    taskbook = ctx.taskbook_payload
    question = str(taskbook.get("research_question") or "").strip()
    datasets = [str(item) for item in (taskbook.get("target_datasets") or [])]
    metrics = [str(item) for item in (taskbook.get("metrics") or [])]
    head = " ".join(question.split()[:8]) or "research question"
    queries: list[str] = []
    if datasets:
        queries.append(f"{head} {datasets[0]}".strip())
    if metrics:
        queries.append(f"{head} {metrics[0]} evaluation".strip())
    queries.append(f"{head} baseline comparison".strip())
    seen: list[str] = []
    for query in queries:
        text = query.strip()
        if text and text not in seen:
            seen.append(text)
    return seen[:3]


def suggest_human_actions(stage: str, level: str, error_code: str) -> list[str]:
    """建议的人工动作（按环节 + 错误码给出，可执行、可核对）。"""
    actions: list[str] = []
    if error_code in {"ModelRoutingError", "SecretBackendUnavailable"} or error_code.startswith("ModelRouting"):
        actions.append("在「模型与成本设置」中为该环节配置可用供应商与模型（stage_model_routing）")
    if error_code == "IsolationViolation":
        actions.append("为 plan_review 配置与 plan 不同的模型（盲评隔离：generator_model_ref != reviewer_model_ref）")
    if error_code == "ReplayMissError":
        actions.append("补齐 demo_fixtures 回放 fixture，或关闭 LLM_REPLAY 走实时调用")
    if error_code in {"llm_auth", "llm_bad_request"}:
        actions.append("核对模型供应商 base_url / api_key / 模型名（401/400 类错误重试无效）")
    if stage == "survey" and level in {"L2", "L3"}:
        actions.append("手工调整任务书检索范围（领域 / 数据集 / 指标），或先扩充 papers 表语料")
    if stage == "experiment" and level in {"L2", "L3"}:
        actions.append("检查实验模板参数与执行器限制（sample_size<=50、单 Run 300s）")
    if level == "L3":
        actions.append("在流水线看板确认 stop_reason 与决策日志后，选择 approve（继续）/ rerun（重跑该环节）/ abort（终止）")
    if not actions:
        actions.append("查看 decision-logs 与被标记 failed 的环节产出，确认后选择 approve / rerun / abort")
    return actions


# --------------------------------------------------------------------------- #
# 《失败分析报告》
# --------------------------------------------------------------------------- #
async def build_failure_report(
    session: AsyncSession, project_id: int, *, run_id: int | None = None
) -> dict[str, Any]:
    """聚合真实数据生成《失败分析报告》（失败链 / 处置 / 成本 / 建议动作）。"""
    project = (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise LookupError(f"project {project_id} 不存在")

    run: PipelineRun | None = None
    if run_id is not None:
        run = (
            await session.execute(select(PipelineRun).where(PipelineRun.id == int(run_id)))
        ).scalar_one_or_none()
    if run is None:
        run = (
            await session.execute(
                select(PipelineRun)
                .where(PipelineRun.project_id == project_id)
                .order_by(PipelineRun.iteration.desc(), PipelineRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    rows: list[StageOutput] = []
    decision_rows: list[DecisionLog] = []
    if run is not None:
        rows = list(
            (
                await session.execute(
                    select(StageOutput)
                    .where(StageOutput.pipeline_run_id == int(run.id))
                    .order_by(StageOutput.updated_at, StageOutput.stage, StageOutput.attempt)
                )
            )
            .scalars()
            .all()
        )
        decision_rows = list(
            (
                await session.execute(
                    select(DecisionLog)
                    .where(DecisionLog.pipeline_run_id == int(run.id))
                    .order_by(DecisionLog.id)
                )
            )
            .scalars()
            .all()
        )

    failure_chain: list[dict[str, Any]] = []
    handled: list[dict[str, Any]] = []
    for row in rows:
        payload = row.output_json if isinstance(row.output_json, dict) else {}
        failure = payload.get("_failure") if isinstance(payload, dict) else None
        if str(row.status) == "failed" or isinstance(failure, dict):
            failure_chain.append(
                {
                    "stage": str(row.stage),
                    "attempt": int(row.attempt or 1),
                    "status": str(row.status),
                    "level": (failure or {}).get("level"),
                    "action": (failure or {}).get("action"),
                    "error_code": (failure or {}).get("error_code"),
                    "reason": (failure or {}).get("reason") or row.error,
                    "revert_to": (failure or {}).get("revert_to"),
                    "at": row.updated_at.isoformat() if row.updated_at else None,
                    "cost_usd": float(row.cost_usd or 0),
                }
            )
            if isinstance(failure, dict) and failure.get("action"):
                handled.append(
                    {
                        "stage": str(row.stage),
                        "attempt": int(row.attempt or 1),
                        "level": failure.get("level"),
                        "action": failure.get("action"),
                        "degrade": failure.get("degrade"),
                        "suggestions": failure.get("suggestions"),
                    }
                )

    cost = await _cost_snapshot(project_id)
    run_cost = round(sum(float(row.cost_usd or 0) for row in rows), 6)

    last_failure = failure_chain[-1] if failure_chain else None
    level = (last_failure or {}).get("level") or "L3"
    stage = (last_failure or {}).get("stage") or _last_stage_of(rows)
    error_code = (last_failure or {}).get("error_code") or "circuit_break"
    suggestions = list(
        (last_failure or {}).get("suggestions") or suggest_human_actions(stage or "unknown", level, error_code)
    )

    report = {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "project_name": project.name,
        "project_status": str(project.status),
        "is_demo": bool(project.is_demo),
        "pipeline_run_id": int(run.id) if run is not None else None,
        "iteration": int(run.iteration) if run is not None else None,
        "run_status": str(run.status) if run is not None else None,
        "stop_reason": run.stop_reason if run is not None else None,
        "level": level,
        "failed_stage": stage,
        "failure_chain": failure_chain,
        "handled": handled,
        "cost": {
            "run_cost_usd": run_cost,
            "project_used_usd": cost.get("used_usd"),
            "limit_usd": cost.get("limit_usd"),
            "quota_usd": cost.get("quota_usd"),
            "limit_exceeded": cost.get("limit_exceeded"),
            "quota_exceeded": cost.get("quota_exceeded"),
            "cost_complete": cost.get("cost_complete"),
        },
        "decision_log_ids": [int(d.id) for d in decision_rows],
        "decision_logs": [
            {
                "id": int(d.id),
                "decision_point": str(d.decision_point),
                "stage": d.stage,
                "chosen": str(d.chosen),
                "policy_action": str(d.policy_action),
                "policy_version": str(d.policy_version),
                "rationale": d.rationale,
                "guardrail_checks": d.guardrail_checks,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decision_rows
        ],
        "suggested_human_actions": suggestions,
        "report_url": f"/api/v1/reports/{project_id}/failure",
        "data_source": "stage_outputs+decision_logs+llm_call_logs（真实数据聚合，无推测数值）",
    }
    return report


def _last_stage_of(rows: list[StageOutput]) -> str | None:
    order = {name: index for index, name in enumerate(STAGE_ORDER)}
    candidates = [row for row in rows if str(row.status) in {"running", "failed", "waiting_human"}]
    if not candidates:
        return None
    candidates.sort(key=lambda row: order.get(str(row.stage), 99))
    return str(candidates[0].stage)


async def _cost_snapshot(project_id: int) -> dict[str, Any]:
    """真实成本快照（cost 服务）；不可用时如实置 None。"""
    try:
        from services.cost import accumulate

        summary = await accumulate(project_id)
        return summary.to_dict()
    except Exception as exc:  # noqa: BLE001 - 成本服务不可用时报告仍须可查
        logger.warning("成本快照不可用 project_id=%s err=%s", project_id, exc)
        return {}


async def persist_failure_report(
    session: AsyncSession, *, run_id: int, stage: str, attempt: int, report: dict[str, Any]
) -> None:
    """把报告快照写进失败环节的 ``stage_outputs.output_json._failure_report``（可查询）。"""
    from services.pipeline.resume import upsert_stage_output

    row = (
        await session.execute(
            select(StageOutput).where(
                StageOutput.pipeline_run_id == int(run_id),
                StageOutput.stage == stage,
                StageOutput.attempt == int(attempt),
            )
        )
    ).scalar_one_or_none()
    payload = dict(row.output_json) if row is not None and isinstance(row.output_json, dict) else {}
    payload["_failure_report"] = report
    await upsert_stage_output(
        session,
        pipeline_run_id=int(run_id),
        stage=stage,
        attempt=int(attempt),
        status="failed",
        output_json=payload,
    )


async def latest_persisted_report(session: AsyncSession, project_id: int) -> dict[str, Any] | None:
    """读取最近一次熔断时落库的报告快照。"""
    rows = list(
        (
            await session.execute(
                select(StageOutput)
                .join(PipelineRun, PipelineRun.id == StageOutput.pipeline_run_id)
                .where(PipelineRun.project_id == project_id)
                .order_by(StageOutput.updated_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        payload = row.output_json if isinstance(row.output_json, dict) else None
        if payload and isinstance(payload.get("_failure_report"), dict):
            snapshot = dict(payload["_failure_report"])
            snapshot["snapshot_from"] = {"stage": str(row.stage), "attempt": int(row.attempt or 1)}
            return snapshot
    return None


__all__ = [
    "D4_OPTIONS",
    "FAILURE_LEVELS",
    "L2_MAX_PER_STAGE",
    "PLAN_REVERT_MAX",
    "REPORT_VERSION",
    "FailureAssessment",
    "assess_failure",
    "build_failure_report",
    "classify_exception",
    "latest_persisted_report",
    "persist_failure_report",
    "suggest_human_actions",
]
