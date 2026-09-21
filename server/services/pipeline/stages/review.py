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
"""``review`` 环节：四维自评审 + 停止条件输入 + D5 迭代判据（WP14-T4/T5，附录 D.6）。

环节契约
--------

======== ==================================================================
输入      ``paper_drafts`` + ``draft_claims``（草稿与三态 Claim）、
          ``experiment_metrics``（真实指标）、``experiment_passports``（可复现凭证）、
          上游 plan_review 盲评分项（novelty 来源）、证据池规模
输出      ``{novelty, rigor, completeness, reproducibility, total,
          scores{}, comments[], rubric{}, stop_conditions{}, d5{}}``
打分      四维各 0–25；``total`` 由**服务端**按四维之和重算（模型自报 total 一律忽略）
落库      ``review_scores``（四维 + total + comments 明细，含 rubric 复算依据）
停止条件  交给 WP09 ``stop_conditions``（质量达标 / 边际停滞 / 轮次用尽），
          ``stop_reason`` 由引擎写入 ``pipeline_runs.stop_reason`` 并上屏
决策      D5 迭代判据（continue / adjust_and_continue / stop）；规则层拥有最终决定权
======== ==================================================================

本环节**不自己决定停止**：只在产出中给出 WP09 的判定预览与 D5 决策留痕，最终
``stop_reason`` 以引擎收尾为准（避免同轮次出现两个互相矛盾的停止结论）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from services.pipeline.stages.base import (
    StageContext,
    StageResult,
    StageValidationError,
    call_llm,
    result_cost_of,
    unknown_cost_notes,
)
from services.review import reviewer, scorer
from services.writing import drafter

logger = logging.getLogger("sciloop.pipeline.stage.review")

WP_ID = "WP14"


# --------------------------------------------------------------------------- #
# 取数（全部为真实落库数据；表缺失/查询失败即返空并如实降级）
# --------------------------------------------------------------------------- #
async def _safe_rows(session: Any, sql: str, params: Mapping[str, Any] | None = None, *, tag: str) -> list[Any]:
    from sqlalchemy import text as sql_text

    try:
        result = await session.execute(sql_text(sql), dict(params or {}))
    except Exception as exc:  # noqa: BLE001 - 并行工作包未就绪时不得阻断评审
        logger.warning("评审取数失败 tag=%s：%s: %s", tag, type(exc).__name__, exc)
        await _rollback(session)
        return []
    try:
        return list(result.mappings().all())
    except AttributeError:  # pragma: no cover - 兼容非 SQLAlchemy 结果
        return list(result)


async def _rollback(session: Any) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        value = session.rollback()
        if hasattr(value, "__await__"):
            await value


async def _load_draft(ctx: StageContext) -> dict[str, Any]:
    """取本 run（退化到 project+iteration）的草稿。"""
    rows = await _safe_rows(
        ctx.session,
        """
        SELECT id, project_id, pipeline_run_id, iteration, content_md, claim_coverage, created_at
        FROM paper_drafts
        WHERE pipeline_run_id = :rid
        ORDER BY id DESC
        LIMIT 1
        """,
        {"rid": int(ctx.pipeline_run_id)},
        tag="paper_drafts",
    )
    if not rows:
        rows = await _safe_rows(
            ctx.session,
            """
            SELECT id, project_id, pipeline_run_id, iteration, content_md, claim_coverage, created_at
            FROM paper_drafts
            WHERE project_id = :pid AND iteration = :iteration
            ORDER BY id DESC
            LIMIT 1
            """,
            {"pid": int(ctx.project_id), "iteration": int(ctx.iteration or 1)},
            tag="paper_drafts_by_iteration",
        )
    if not rows:
        raise StageValidationError(
            "review 缺少上游草稿（paper_drafts 无本 run 记录）：writing 环节未完成或未落库",
            level="L2",
            stage="review",
            detail={"pipeline_run_id": int(ctx.pipeline_run_id)},
        )
    row = rows[0]
    return {
        "draft_id": int(row["id"]),
        "content_md": str(row["content_md"] or ""),
        "claim_coverage": float(row["claim_coverage"]) if row["claim_coverage"] is not None else None,
        "iteration": int(row["iteration"] or 1),
    }


async def _claim_counts(ctx: StageContext, draft_id: int) -> dict[str, int]:
    rows = await _safe_rows(
        ctx.session,
        """
        SELECT is_factual, support_status, count(*) AS total
        FROM draft_claims
        WHERE draft_id = :draft_id
        GROUP BY is_factual, support_status
        """,
        {"draft_id": int(draft_id)},
        tag="draft_claims_counts",
    )
    counts = {
        "total": 0,
        "factual": 0,
        "supported": 0,
        "contradicted": 0,
        "insufficient": 0,
        "non_factual": 0,
        "contradicted_factual": 0,
        "insufficient_factual": 0,
    }
    for row in rows:
        total = int(row["total"] or 0)
        is_factual = bool(row["is_factual"])
        status = str(row["support_status"] or "insufficient")
        counts["total"] += total
        if not is_factual:
            counts["non_factual"] += total
            continue
        counts["factual"] += total
        if status in {"supported", "contradicted", "insufficient"}:
            counts[status] += total
        if status == "contradicted":
            counts["contradicted_factual"] += total
        elif status == "insufficient":
            counts["insufficient_factual"] += total
    return counts


async def _bound_evidence_count(ctx: StageContext, draft_id: int) -> int:
    rows = await _safe_rows(
        ctx.session,
        """
        SELECT count(*) AS total
        FROM evidences e
        JOIN draft_claims c ON c.id = e.owner_id
        WHERE e.owner_type = 'draft_claim' AND c.draft_id = :draft_id
        """,
        {"draft_id": int(draft_id)},
        tag="draft_claim_evidences",
    )
    return int(rows[0]["total"] or 0) if rows else 0


async def _load_metrics(ctx: StageContext) -> list[dict[str, Any]]:
    rows = await _safe_rows(
        ctx.session,
        """
        SELECT m.id, m.experiment_run_id, m.metric_name, m.metric_value, m.metric_unit,
               e.template_id
        FROM experiment_metrics m
        JOIN experiment_runs r ON r.id = m.experiment_run_id
        JOIN experiments e ON e.id = r.experiment_id
        LEFT JOIN stage_outputs so ON so.id = e.stage_output_id
        LEFT JOIN pipeline_runs pr ON pr.id = so.pipeline_run_id
        WHERE pr.project_id = :pid
        ORDER BY m.id
        LIMIT 40
        """,
        {"pid": int(ctx.project_id)},
        tag="experiment_metrics",
    )
    return [
        {
            "metric_id": int(row["id"]),
            "experiment_run_id": int(row["experiment_run_id"])
            if row["experiment_run_id"] is not None
            else None,
            "metric_name": str(row["metric_name"]),
            "metric_value": float(row["metric_value"]) if row["metric_value"] is not None else None,
            "metric_unit": row["metric_unit"],
            "template_id": row["template_id"],
        }
        for row in rows
    ]


async def _load_passport(ctx: StageContext) -> dict[str, Any] | None:
    rows = await _safe_rows(
        ctx.session,
        """
        SELECT p.id, p.status, p.is_replay, p.template_id, p.model_id,
               p.dataset_name, p.dataset_version, p.dataset_sha256, p.prompt_sha256,
               p.code_commit_sha, p.dependency_lock_sha256, p.metrics
        FROM experiment_passports p
        JOIN experiment_runs r ON r.id = p.experiment_run_id
        JOIN experiments e ON e.id = r.experiment_id
        LEFT JOIN stage_outputs so ON so.id = e.stage_output_id
        LEFT JOIN pipeline_runs pr ON pr.id = so.pipeline_run_id
        WHERE pr.project_id = :pid
        ORDER BY p.id DESC
        LIMIT 1
        """,
        {"pid": int(ctx.project_id)},
        tag="experiment_passports",
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "passport_id": int(row["id"]),
        "status": str(row["status"] or "incomplete"),
        "is_replay": bool(row["is_replay"]),
        "template_id": row["template_id"],
        "model_id": row["model_id"],
        "dataset_name": row["dataset_name"],
        "dataset_version": row["dataset_version"],
        "dataset_sha256": row["dataset_sha256"],
        "prompt_sha256": row["prompt_sha256"],
        "code_commit_sha": row["code_commit_sha"],
        "dependency_lock_sha256": row["dependency_lock_sha256"],
        "metrics": row["metrics"],
    }


async def _gap_count(ctx: StageContext) -> int | None:
    rows = await _safe_rows(
        ctx.session,
        """
        SELECT count(*) AS total
        FROM gaps g
        JOIN aggregations a ON a.id = g.aggregation_id
        WHERE a.project_id = :pid
        """,
        {"pid": int(ctx.project_id)},
        tag="gaps",
    )
    return int(rows[0]["total"] or 0) if rows else None


def _plan_review_summary(ctx: StageContext) -> dict[str, Any] | None:
    """上游盲评摘要（新颖性维度的真实来源；取不到就返回 None，由 scorer 归一）。"""
    payload = ctx.upstream("plan_review")
    if not payload:
        return None
    scores = [item for item in (payload.get("scores") or []) if isinstance(item, Mapping)]
    if not scores:
        return None
    selected_index = payload.get("selected_method_index")
    chosen = next((item for item in scores if item.get("method_index") == selected_index), None)
    if chosen is None:
        chosen = scores[0]
    sample = chosen
    return {
        "verdict": payload.get("verdict"),
        "selected_method_index": selected_index,
        "novelty": sample.get("novelty"),
        "total": sample.get("total"),
        "scores": [
            {
                "method_index": item.get("method_index"),
                "candidate_alias": item.get("candidate_alias") or item.get("alias"),
                "novelty": item.get("novelty"),
                "total": item.get("total"),
            }
            for item in scores
        ],
        "concerns": list(payload.get("concerns") or [])[:6],
        "note": "来自 plan_review 隔离盲评（WP12）的真实分项，非本环节打分",
    }


# --------------------------------------------------------------------------- #
# D5 迭代判据
# --------------------------------------------------------------------------- #
async def _request_d5(
    ctx: StageContext,
    *,
    total: float,
    previous_total: float | None,
    stop_decision: Mapping[str, Any],
) -> dict[str, Any]:
    """把本轮真实总分与停止条件结论交给 WP10 的 D5 迭代判据（留痕到 ``decision_logs``）。"""
    try:
        from services.pipeline.decision_engine import evaluate_decision
    except Exception as exc:  # noqa: BLE001 - WP10 未挂载属正常并行期状态
        return {"available": False, "reason": f"policy_not_mounted:{type(exc).__name__}"}

    recommended = "stop" if stop_decision.get("should_stop") else "continue"
    context = {
        "stage": "review",
        "decision_point": "D5",
        "attempt": ctx.attempt,
        "iteration": int(ctx.iteration or 1),
        "mode": ctx.mode,
        "pipeline_run_id": ctx.pipeline_run_id,
        "taskbook_id": getattr(ctx.taskbook, "id", None),
        "action_domain": {"iteration_decision": recommended},
        "options_considered": ["continue", "adjust_and_continue", "stop"],
        "review_total": float(total),
        "previous_total": previous_total if previous_total is not None else None,
        "stop_reason": stop_decision.get("stop_reason"),
        "marginal_gain": (
            round(float(total) - float(previous_total), 2) if previous_total is not None else None
        ),
    }
    try:
        outcome = await evaluate_decision(ctx.project_id, "D5", context, persist=True)
    except Exception as exc:  # noqa: BLE001 - 策略层异常不得让评审分丢失
        logger.warning("D5 策略调用失败（不影响评审分落库）：%s", exc, exc_info=True)
        return {"available": False, "reason": f"policy_call_failed:{type(exc).__name__}: {exc}"}

    if not isinstance(outcome, Mapping):
        return {"available": False, "reason": "policy_returned_non_mapping"}
    return {
        "available": True,
        "decision_point": "D5",
        "policy_action": outcome.get("policy_action"),
        "chosen": outcome.get("chosen"),
        "risk_score": outcome.get("risk_score"),
        "confidence_score": outcome.get("confidence_score"),
        "reversibility_score": outcome.get("reversibility_score"),
        "rationale": outcome.get("rationale"),
        "policy_version": outcome.get("policy_version"),
        "decision_log_id": outcome.get("decision_log_id"),
        "recommended": recommended,
        "note": (
            "chosen 为策略层动作标签（规则层按风险/置信度/可逆性给出）；实际迭代判据见 "
            "recommended，与 WP09 stop_conditions 的同轮判定一致；run 级 stop_reason 由引擎收尾写入"
        ),
    }


# --------------------------------------------------------------------------- #
# 环节实现
# --------------------------------------------------------------------------- #
async def run(ctx: StageContext) -> StageResult:
    """执行评审环节：四维打分 → 落库 → 停止条件预览 → D5 迭代判据。"""
    if ctx.session is None:
        raise RuntimeError("review 环节缺少数据库会话（engine 必须注入 ctx.session）")

    notes: list[str] = []
    degradations: list[str] = []
    costs: list[Any] = []

    await ctx.progress(5, "加载草稿、Claim 三态与实验证据")
    draft = await _load_draft(ctx)
    counts = await _claim_counts(ctx, draft["draft_id"])
    bound = await _bound_evidence_count(ctx, draft["draft_id"])
    metrics = await _load_metrics(ctx)
    passport = await _load_passport(ctx)
    gaps = await _gap_count(ctx)
    plan_review = _plan_review_summary(ctx)
    previous_total = await reviewer.previous_review_total(
        ctx.session, project_id=int(ctx.project_id), iteration=int(ctx.iteration or 1)
    )

    await ctx.progress(25, "组装证据池与评审输入")
    pool = await drafter.build_evidence_pool(ctx.session, ctx.project_id)
    distinct_span_papers = len({entry.paper_id for entry in pool.by_type("paper_span") if entry.paper_id})
    unsupported_count = int(counts.get("insufficient") or 0) + int(counts.get("contradicted") or 0)

    inputs = reviewer.ReviewInputs(
        draft_id=draft["draft_id"],
        iteration=int(ctx.iteration or 1),
        title=(draft["content_md"].splitlines() or [None])[0],
        claim_coverage=draft["claim_coverage"],
        claim_counts=counts,
        bound_evidence_count=bound,
        section_count=len([line for line in draft["content_md"].splitlines() if line.startswith("## ")]),
        evidence_pool_size=len(pool),
        distinct_span_papers=distinct_span_papers,
        gap_count=gaps,
        metric_count=len(metrics),
        metrics=metrics,
        passport=passport,
        plan_review=plan_review,
        unsupported_count=unsupported_count,
        previous_total=previous_total,
    )
    if not passport:
        degradations.append("无 Passport（WP11 未产出）：可复现性维度按缺失权重归一，已如实披露")
    if not plan_review:
        degradations.append("上游无 plan_review 分项：新颖性维度按剩余权重归一，未编造数值")

    # 1) 四维评分（rubric 服务端权威；LLM 只提供评语）
    await ctx.progress(45, "计算四维评分")
    async def _llm_call(messages: list[dict[str, Any]]) -> Any:
        outcome = await call_llm(
            ctx,
            messages,
            json_schema=reviewer.REVIEW_SCHEMA,
            purpose="review.comments",
            temperature=0.2,
        )
        costs.append(outcome)
        return outcome

    settings = (ctx.extras or {}).get("settings")
    llm_call: Callable[[list[dict[str, Any]]], Awaitable[Any]] | None = _llm_call
    if _llm_ready(settings) is False:
        llm_call = None
        degradations.append("LLM 不可用（未配置凭据）：评语使用规则兜底（comments_source=rule_based）")

    try:
        result = await reviewer.review(
            inputs,
            draft_excerpt=reviewer.draft_excerpt_of(draft["content_md"]),
            llm_call=llm_call,
            llm_model_ref=None,
        )
    except scorer.ScoringUnavailable as exc:
        raise StageValidationError(
            f"四维无法计算（{exc}）：拒绝用占位分冒充（草稿/证据池全空）",
            level="L2",
            stage="review",
            detail=inputs.to_dict(),
        ) from exc

    await ctx.progress(65, "落库 review_scores")
    review_score_id = await reviewer.persist_review_score(
        ctx.session, pipeline_run_id=int(ctx.pipeline_run_id), result=result
    )
    await _commit(ctx.session)

    # 2) 停止条件输入（WP09 的服务端判定，仅作预览；最终写入由引擎收尾完成）
    await ctx.progress(80, "计算停止条件并请求 D5 决策")
    from services.pipeline.stop_conditions import evaluate_stop_conditions, resolve_thresholds

    thresholds = resolve_thresholds(project=ctx.project, taskbook=ctx.taskbook)
    stop_decision = evaluate_stop_conditions(
        iteration=int(ctx.iteration or 1),
        score=float(result["total"]),
        previous_score=previous_total,
        thresholds=thresholds,
    )
    d5 = await _request_d5(
        ctx,
        total=float(result["total"]),
        previous_total=previous_total,
        stop_decision=stop_decision.to_dict(),
    )
    if d5.get("available"):
        notes.append(
            f"D5 决策：chosen={d5.get('chosen')} policy_action={d5.get('policy_action')} "
            f"decision_log_id={d5.get('decision_log_id')}"
        )
    else:
        degradations.append(f"D5 策略层不可用（{d5.get('reason')}）：未编造迭代结论")

    total = float(result["total"])
    payload = {
        "draft_id": draft["draft_id"],
        "review_score_id": int(review_score_id),
        "iteration": int(ctx.iteration or 1),
        "novelty": float(result["novelty"]),
        "rigor": float(result["rigor"]),
        "completeness": float(result["completeness"]),
        "reproducibility": float(result["reproducibility"]),
        "total": total,
        "scores": dict(result["scores"]),
        "total_rule": "total = novelty + rigor + completeness + reproducibility（服务端重算）",
        "comments": list(result["comments"]),
        "comments_source": result["comments_source"],
        "rubric_version": result["rubric_version"],
        "rubric": result["rubric"],
        "score_coverage": result["score_coverage"],
        "llm": result["llm"],
        "claim_counts": counts,
        "claim_coverage": draft["claim_coverage"],
        "previous_total": previous_total,
        "marginal_gain": (
            round(total - float(previous_total), 2) if previous_total is not None else None
        ),
        "stop_conditions": stop_decision.to_dict(),
        "stop_reason": stop_decision.reason,
        "stop_reason_note": (
            "本字段为 WP09 停止条件的预览；pipeline_runs.stop_reason 由引擎收尾写入（同一口径）"
        ),
        "d5_decision": d5,
        "disclaimer": result.get("disclaimer") or "本内容由 AI 辅助生成，需研究者自行核验",
        "notes": notes,
    }

    cost_usd = result_cost_of(*costs)
    notes.extend(unknown_cost_notes(*costs))
    logger.info(
        "review done draft_id=%s total=%s dims=%s stop_reason=%s d5=%s",
        draft["draft_id"],
        total,
        result["scores"],
        stop_decision.reason,
        d5.get("chosen"),
    )
    await ctx.progress(100, "评审环节完成（四维已落库，停止条件已交 WP09/WP10）")

    return StageResult(
        output=payload,
        text=json.dumps(
            {
                "total": total,
                "scores": result["scores"],
                "stop_conditions": stop_decision.to_dict(),
            },
            ensure_ascii=False,
        ),
        cost_usd=cost_usd,
        metrics={
            "total": total,
            "novelty": float(result["novelty"]),
            "rigor": float(result["rigor"]),
            "completeness": float(result["completeness"]),
            "reproducibility": float(result["reproducibility"]),
            "review_score_id": int(review_score_id),
            "stop_reason": stop_decision.reason,
            "claim_coverage": draft["claim_coverage"],
            "iteration": int(ctx.iteration or 1),
        },
        quality_ok=True,
        quality_issues=[],
        degradations=degradations,
        notes=notes,
        decision={
            "decision_point": "D5",
            "chosen": d5.get("chosen") or ("stop" if stop_decision.should_stop else "continue"),
            "rationale": d5.get("rationale") or stop_decision.detail,
        },
    )


def _llm_ready(settings: Any) -> bool | None:
    """LLM 是否可用；``None`` 表示配置不可读（此时按可用处理，由调用异常兜底）。"""
    if settings is None:
        return None
    if bool(getattr(settings, "llm_replay", False)):
        return True
    if str(getattr(settings, "llm_default_api_key", "") or "").strip():
        return True
    return bool(str(getattr(settings, "llm_fallback_api_key", "") or "").strip())


async def _commit(session: Any) -> None:
    import inspect

    value = session.commit()
    if inspect.isawaitable(value):
        await value


class ReviewStage:
    """``review`` 环节处理器（四维自评审，决策点 D5）。"""

    name = "review"
    decision_point = "D5"

    async def run(self, ctx: StageContext) -> StageResult:
        return await run(ctx)


STAGE = ReviewStage()


def register() -> ReviewStage:
    """把本环节注册进 WP09 的注册表（幂等；供 API 模块导入时调用）。"""
    from services.pipeline.stages import register_stage

    return register_stage("review", STAGE)  # type: ignore[return-value]


__all__ = [
    "STAGE",
    "ReviewStage",
    "register",
    "run",
]
