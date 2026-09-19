# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``plan_review`` 环节：隔离模型盲评（WP12-T2/T3，附录 D.3 —— v1.2 核心创新）。

环节契约
--------

======== ==================================================================
输入      经服务端匿名化 + 固定种子乱序的候选方案 + 任务书约束
          （不得包含生成模型、供应商、原始下标、生成时间或推荐项）
输出      ``{scores:[{method_index, novelty, feasibility, rigor,
          cost_reasonableness, risk_control, total, comments[]}],
          verdict, concerns[], selected_method_id, improvement_suggestions[]}``
打分      五维各 0-20；``total`` 由**服务端**按五维之和重算（不信任模型给的 total）
校验      ``verdict ∈ {approve, revise, reject}``；``approve`` 必须有 selected；
          ``revise`` 必须给 ≥1 条 improvement_suggestions
流向      ``approve`` → experiment；``revise`` → 回退 plan（全局最多 1 次，
          超限按 L2 降级）；``reject`` → 触发 D4 失败处置
决策点    D2 方案选型（评审分与策略动作**分开存储**）
======== ==================================================================

隔离失败（``generator_model_ref == reviewer_model_ref``）时本环节**直接失败**，
抛 :class:`~app.services.review.blind.IsolationStageError`（同时是 ``StageError``
与 WP02 的 ``IsolationViolation``），**不静默回退同一模型**。
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.services.pipeline.stages.base import (
    StageContext,
    StageResult,
    StageValidationError,
    call_llm,
    result_cost_of,
    unknown_cost_notes,
)
from app.services.review import blind
from app.services.review.calibration import DIMENSIONS, SCORE_MAX, TOTAL_MAX

logger = logging.getLogger("sciloop.pipeline.stage.plan_review")

VERDICTS: tuple[str, ...] = ("approve", "revise", "reject")

#: ``revise`` 回退 plan 的全局次数上限（附录 D.3；引擎侧另有同值硬约束）
REVISE_GLOBAL_MAX = 1

_DIMENSION_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "maximum": SCORE_MAX,
    "description": f"0-{SCORE_MAX} 的整数",
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_alias": {"type": "string", "minLength": 1},
                    "novelty": _DIMENSION_SCHEMA,
                    "feasibility": _DIMENSION_SCHEMA,
                    "rigor": _DIMENSION_SCHEMA,
                    "cost_reasonableness": _DIMENSION_SCHEMA,
                    "risk_control": _DIMENSION_SCHEMA,
                    "comments": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "candidate_alias",
                    "novelty",
                    "feasibility",
                    "rigor",
                    "cost_reasonableness",
                    "risk_control",
                ],
            },
        },
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "selected_alias": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "improvement_suggestions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scores", "verdict", "concerns"],
}

_SYSTEM_PROMPT = (
    "你是科研辅助系统的「隔离盲评（plan_review）」评委。你**不知道**任何候选方案由谁生成，"
    "候选只用别名（A/B/C…）指代；也不要猜测、询问或推断它们的来源、生成顺序或是否被预先指定。\n"
    "必须严格遵守：\n"
    "1) 只输出合法 JSON，不要任何解释性文字；\n"
    "2) 对**每一个**候选别名都给五维打分，每维 0-20 的整数："
    "novelty（新颖性）/ feasibility（可执行性）/ rigor（严谨性）/ "
    "cost_reasonableness（成本合理）/ risk_control（风险可控）；\n"
    "3) 不要自行给出 total，服务端会按五维之和重算；\n"
    "4) verdict 只能是 approve / revise / reject；\n"
    "5) verdict=approve 时必须给出 selected_alias（只能取候选别名之一）；\n"
    "6) verdict=revise 时必须给出至少 1 条 improvement_suggestions；\n"
    "7) 不确定的信息写 unknown，禁止编造数据或引用不存在的事实。"
)


# --------------------------------------------------------------------------- #
# 环节实现
# --------------------------------------------------------------------------- #
async def run(ctx: StageContext) -> StageResult:
    """执行隔离盲评（含匿名化、打分复核、verdict 流转与校准记录落库）。"""
    notes: list[str] = []
    degradations: list[str] = []

    await ctx.progress(5, "解析生成 / 评审路由并校验盲评隔离")
    generator, reviewer = await blind.resolve_isolated_pair(ctx.project_id)
    generator_ref = str(generator.model_ref)
    reviewer_ref = str(reviewer.model_ref)

    plan_output = ctx.upstream("plan")
    methods = [item for item in (plan_output.get("methods") or []) if isinstance(item, Mapping)]
    if not methods:
        raise StageValidationError(
            "plan_review 缺少上游 plan 的候选方案（methods 为空；断点续跑或 plan 产出异常）",
            level="L2",
            stage="plan_review",
            detail={"available_upstream": sorted(ctx.inputs)},
        )

    # 1) 服务端生成 shuffle_seed（可由 hints 指定以便复现，模型无法影响）
    seed = _resolve_seed(ctx)

    # 2) 匿名化 + 固定种子乱序（内部自带泄漏自检，发现泄漏即抛 AnonymizationLeakError）
    forbidden = blind.forbidden_tokens_for(generator, reviewer)
    pack = blind.anonymize_and_shuffle(methods, seed=seed, forbidden_tokens=forbidden)
    payload = blind.build_review_payload(
        pack, taskbook_constraints=_taskbook_constraints(ctx)
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]

    # 3) 送审前对**真实 prompt** 再做一次字符串审计（验收 A2 的同一口径）
    prompt_audit = blind.assert_anonymized(messages, forbidden_tokens=forbidden)
    if not prompt_audit["clean"]:
        raise blind.AnonymizationLeakError(
            f"评审 prompt 审计未通过，命中可识别信息 {prompt_audit['hits']}（拒绝送审）",
            hits=prompt_audit["hits"],
            detail={"anonymization_version": blind.ANONYMIZATION_VERSION, "seed": seed},
        )

    await ctx.progress(35, f"匿名候选 {len(pack['aliased'])} 套，调用评审模型（隔离成立）")
    result = await call_llm(
        ctx,
        messages,
        json_schema=REVIEW_SCHEMA,
        purpose="plan_review",
        temperature=0.2,
        model_ref=reviewer_ref,
    )

    # 4) 兜底再校验：实际调用的模型绝不能等于生成模型（防降级链撞车）
    used_ref = str(result.model_ref or "")
    if blind.norm_ref(used_ref) == blind.norm_ref(generator_ref):
        raise blind.IsolationStageError(
            f"IsolationViolation：评审实际调用的模型与生成模型相同（{used_ref}），"
            "兜底隔离校验失败，环节终止（不做任何回退）",
            generator_ref=generator_ref,
            reviewer_ref=used_ref or reviewer_ref,
            detail={"project_id": ctx.project_id, "cause": "post_call_check"},
        )

    parsed = result.parsed if isinstance(result.parsed, dict) else None
    if parsed is None:
        raise StageValidationError(
            "plan_review 评审模型未返回可解析 JSON（L1：格式解析失败，可自动重试）",
            level="L1",
            stage="plan_review",
            detail={"model_ref": used_ref, "content_head": (result.content or "")[:400]},
        )

    scored = _validate_scores(parsed.get("scores"), pack["mapping"])
    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        raise StageValidationError(
            f"plan_review verdict 非法：{parsed.get('verdict')!r}，合法值 {list(VERDICTS)}（L1）",
            level="L1",
            stage="plan_review",
            detail={"allowed": list(VERDICTS)},
        )

    selected_alias = str(parsed.get("selected_alias") or "").strip().upper() or None
    selected_index = blind.resolve_alias(pack["mapping"], selected_alias) if selected_alias else None
    concerns = _str_list(parsed.get("concerns"))
    suggestions = _str_list(parsed.get("improvement_suggestions"))

    if verdict == "approve" and (not selected_alias or selected_index is None):
        raise StageValidationError(
            "plan_review verdict=approve 但 selected_alias 缺失或不在候选别名内"
            f"（收到 {parsed.get('selected_alias')!r}；附录 D.3：approve 时 selected_method_id 必填，L1）",
            level="L1",
            stage="plan_review",
            detail={"aliases": sorted(pack["mapping"])},
        )
    if verdict == "revise" and not suggestions:
        raise StageValidationError(
            "plan_review verdict=revise 但未给出 improvement_suggestions（附录 D.3 要求 ≥1 条，L1）",
            level="L1",
            stage="plan_review",
            detail={"verdict": verdict},
        )

    # 5) 五维总分由服务端重算
    scores = _resolve_totals(scored, parsed.get("scores"), notes)
    scores_by_index = sorted(
        (item for item in scores if item.get("method_index") is not None),
        key=lambda item: int(item["method_index"]),
    )

    # 6) 校准记录落库（status=pending；人工标签由 POST /human-labels 补足）
    model_scores = _model_scores_for_db(scores_by_index, pack, selected_index)
    calibration_id, calibration_error = await _persist_calibration(
        ctx,
        generator_ref=generator_ref,
        reviewer_ref=used_ref or reviewer_ref,
        pack=pack,
        model_scores=model_scores,
    )
    if calibration_error:
        notes.append(f"review_calibrations 落库失败（未影响评审结论）：{calibration_error}")

    # 7) verdict 流转
    revert_used = int(ctx.extras.get("revert_used", 0) or 0)
    quality_ok = True
    revert_to: str | None = None
    quality_issues: list[str] = []
    if verdict == "revise":
        quality_ok = False
        quality_issues.append(
            f"隔离盲评判定 revise：{len(suggestions)} 条改进建议，回退 plan 重新生成候选"
        )
        if revert_used >= REVISE_GLOBAL_MAX:
            degradations.append(
                f"revise 回退 plan 已用尽全局上限 {REVISE_GLOBAL_MAX} 次（revert_used={revert_used}），"
                "本轮不再回退，交由 L2 降级策略处置（switch_template）"
            )
        else:
            revert_to = "plan"
    elif verdict == "reject":
        quality_ok = False
        quality_issues.append(
            "隔离盲评判定 reject：全部候选方案被驳回 → 触发 D4 失败处置"
            "（retry / downgrade / switch_template / circuit_break 由风险策略裁决）"
        )

    d2 = await _request_d2(ctx, scores_by_index, selected_index, verdict)

    await ctx.progress(85, f"评审完成：verdict={verdict}，候选 {len(scores_by_index)} 套已落库")

    output: dict[str, Any] = {
        # ---- 附录 D.3 规定字段 ----
        "scores": scores_by_index,
        "verdict": verdict,
        "concerns": concerns,
        "selected_method_id": f"method_{selected_index}" if selected_index is not None else None,
        "improvement_suggestions": suggestions,
        # ---- 便于下游（experiment/WP11）直接取用的同一份事实 ----
        "selected_method_index": selected_index,
        "selected_alias": selected_alias,
        "selected_method": (
            dict(methods[selected_index])
            if selected_index is not None and 0 <= selected_index < len(methods)
            else None
        ),
        # ---- 审计附加字段 ----
        "isolation": {
            "ok": True,
            "rule": "generator_model_ref != reviewer_model_ref",
            "generator_stage": blind.GENERATOR_STAGE,
            "reviewer_stage": blind.REVIEWER_STAGE,
            "generator_model_ref": generator_ref,
            "reviewer_model_ref": used_ref or reviewer_ref,
            "reviewer_configured_ref": reviewer_ref,
            "generator_source": generator.source,
            "reviewer_source": reviewer.source,
            "fallback_used": bool(used_ref and used_ref != reviewer_ref),
        },
        "anonymization": {
            "version": blind.ANONYMIZATION_VERSION,
            "shuffle_seed": pack["seed"],
            "seed_source": "hints" if ctx.hints.get("shuffle_seed") is not None else "server_derived",
            "candidate_order": pack["candidate_order"],
            "mapping": pack["mapping"],
            "masked_tokens": pack["masked_tokens"],
            "leak_check": pack["leak_check"],
            "prompt_audit": prompt_audit,
            "note": (
                "映射与 seed 只在服务端保存；评审 prompt 已通过 0 命中的可识别信息检索"
                "（provider/model/生成时间/原始下标/推荐项）"
            ),
        },
        "revise_quota": {
            "global_max": REVISE_GLOBAL_MAX,
            "used_before_this_attempt": revert_used,
            "revert_to_plan_this_attempt": revert_to == "plan",
        },
        "d2_decision": d2,
        "scoring_spec": {
            "dimensions": list(DIMENSIONS),
            "per_dimension_max": SCORE_MAX,
            "total_max": TOTAL_MAX,
        },
        "upstream": {"methods_count": len(methods)},
        "calibration_id": calibration_id,
        "calibration_status": "pending" if calibration_id else None,
        "calibration_note": (
            "评审分已写入 review_calibrations.model_scores；人工标签通过 "
            "POST /pipelines/{project_id}/human-labels 录入后计算一致率（样本数同时展示）"
        ),
        "model_ref": used_ref or reviewer_ref,
        "llm_prompt_hash": result.prompt_hash,
        "is_replay": bool(result.is_replay),
        "generated_at": _utc_now(),
    }

    notes.extend(unknown_cost_notes(result))
    if ctx.degrade:
        notes.append(f"本轮为 L2 降级重试，降级提示：{ctx.degrade}")
    if revert_to:
        notes.append("本环节请求回退 plan（revise 全局最多 1 次）")

    metrics = {
        "candidates": len(scores_by_index),
        "shuffle_seed": pack["seed"],
        "verdict": verdict,
    }
    if selected_index is not None:
        metrics["selected_method_index"] = selected_index
    best = _best_score(scores_by_index)
    if best is not None:
        metrics["max_total"] = best
    if not d2.get("available"):
        metrics["d2_policy"] = "unavailable"

    return StageResult(
        output=output,
        cost_usd=result_cost_of(result),
        verdict=verdict,
        selected_method_index=selected_index,
        metrics=metrics,
        quality_ok=quality_ok,
        quality_issues=quality_issues,
        revert_to=revert_to,
        degradations=degradations,
        notes=notes,
        decision={
            "decision_point": "D4" if verdict == "reject" else "D2",
            "chosen": (
                "downgrade" if verdict == "reject" else f"select(method_index={selected_index})"
            ),
            "rationale": _decision_rationale(verdict, scores_by_index, selected_index, concerns),
        },
    )


# --------------------------------------------------------------------------- #
# 校验与整理
# --------------------------------------------------------------------------- #
def _validate_scores(raw_scores: Any, mapping: Mapping[str, Any]) -> list[dict[str, Any]]:
    """校验评审打分：别名合法、五维 0-20 整数；任何越界都按 L1 失败（不静默截断）。"""
    if not isinstance(raw_scores, list) or not raw_scores:
        raise StageValidationError(
            "plan_review 未返回任何候选打分（scores 为空，L1）",
            level="L1",
            stage="plan_review",
            detail={"aliases": sorted(mapping)},
        )

    valid_aliases = {str(key).strip().upper() for key in mapping}
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    unknown_alias: list[str] = []

    for item in raw_scores:
        if not isinstance(item, Mapping):
            invalid.append({"raw": repr(item)[:120], "reason": "非对象"})
            continue
        alias = str(item.get("candidate_alias") or item.get("alias") or "").strip().upper()
        if alias not in valid_aliases:
            unknown_alias.append(alias or "<空>")
            continue
        if alias in seen:
            invalid.append({"alias": alias, "reason": "重复别名"})
            continue
        seen.add(alias)
        # 别名 → 原始下标（**服务端**完成还原；评审模型始终只看到别名）
        row: dict[str, Any] = {
            "candidate_alias": alias,
            "method_index": blind.resolve_alias(mapping, alias),
        }
        bad = False
        for dimension in DIMENSIONS:
            value = item.get(dimension)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                invalid.append({"alias": alias, "field": dimension, "value": value, "reason": "非数值"})
                bad = True
                continue
            number = int(value)
            if not 0 <= number <= SCORE_MAX:
                invalid.append(
                    {"alias": alias, "field": dimension, "value": number, "reason": f"超出 0-{SCORE_MAX}"}
                )
                bad = True
                continue
            row[dimension] = number
        if bad:
            continue
        row["comments"] = _str_list(item.get("comments"))
        row["model_total"] = item.get("total")
        cleaned.append(row)

    if unknown_alias:
        raise StageValidationError(
            f"plan_review 打分引用了不存在的候选别名：{unknown_alias}（合法别名 {sorted(mapping)}，L1）",
            level="L1",
            stage="plan_review",
            detail={"aliases": sorted(mapping)},
        )
    if invalid:
        raise StageValidationError(
            f"plan_review 五维打分不合法（{len(invalid)} 处）：每维必须是 0-{SCORE_MAX} 的整数（L1）",
            level="L1",
            stage="plan_review",
            detail={"invalid": invalid[:20]},
        )
    if not cleaned:
        raise StageValidationError(
            "plan_review 打分全部无效（L1）", level="L1", stage="plan_review", detail={"aliases": sorted(mapping)}
        )
    unresolved = [item["candidate_alias"] for item in cleaned if item.get("method_index") is None]
    if unresolved:
        raise StageValidationError(
            f"别名无法还原为原始下标：{unresolved}（别名→下标映射异常，L1）",
            level="L1",
            stage="plan_review",
            detail={"mapping": dict(mapping)},
        )
    return cleaned


def _resolve_totals(
    scored: Sequence[Mapping[str, Any]],
    raw_scores: Any,
    notes: list[str],
) -> list[dict[str, Any]]:
    """服务端重算 ``total = 五维之和``；模型自报的 total 仅作对照（不一致就留痕）。"""
    raw_by_alias: dict[str, Any] = {}
    if isinstance(raw_scores, list):
        for item in raw_scores:
            if isinstance(item, Mapping):
                alias = str(item.get("candidate_alias") or item.get("alias") or "").strip().upper()
                if alias:
                    raw_by_alias[alias] = item.get("total")

    result: list[dict[str, Any]] = []
    for item in scored:
        alias = str(item["candidate_alias"])
        total = sum(int(item[dimension]) for dimension in DIMENSIONS)
        row = dict(item)
        reported = raw_by_alias.get(alias)
        if (
            isinstance(reported, (int, float))
            and not isinstance(reported, bool)
            and int(reported) != total
        ):
            notes.append(
                f"候选 {alias}：模型自报 total={int(reported)} 与五维之和 {total} 不一致，"
                f"按契约以五维之和为准"
            )
        row["total"] = total
        row.pop("model_total", None)
        result.append(row)
    return result


def _model_scores_for_db(
    scores: Sequence[Mapping[str, Any]],
    pack: Mapping[str, Any],
    selected_index: int | None,
) -> list[dict[str, Any]]:
    """落库结构：别名 + 原始下标 + 五维 + total + ``selected``（供人工标签对齐）。"""
    from app.services.review.calibration import build_model_scores

    return build_model_scores(
        scores, candidate_order=pack["candidate_order"], selected_method_index=selected_index
    )


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _best_score(scores: Sequence[Mapping[str, Any]]) -> int | None:
    totals = [int(item["total"]) for item in scores if item.get("total") is not None]
    return max(totals) if totals else None


def _taskbook_constraints(ctx: StageContext) -> dict[str, Any]:
    """送审的任务书约束（只含研究问题与硬性约束，不含任务书 id / 项目 id）。"""
    taskbook = ctx.taskbook_payload
    constraints: dict[str, Any] = {
        "research_question": taskbook.get("research_question"),
        "target_datasets": taskbook.get("target_datasets"),
        "baselines": taskbook.get("baselines"),
        "metrics": taskbook.get("metrics"),
        "compute_budget": taskbook.get("compute_budget"),
        "deliverables": taskbook.get("deliverables"),
        "sample_size_limit": 50,
    }
    return {key: value for key, value in constraints.items() if value not in (None, [], {})}


def _resolve_seed(ctx: StageContext) -> int:
    """``shuffle_seed``：优先人工指定（演示/验收可复现），否则由服务端按运行标识派生。"""
    for source in (
        ctx.hints.get("shuffle_seed"),
        ctx.extras.get("shuffle_seed"),
    ):
        if source not in (None, ""):
            try:
                return int(source)
            except (TypeError, ValueError):
                logger.warning("忽略非整数 shuffle_seed：%r", source)
    return blind.deterministic_seed(
        ctx.pipeline_run_id, ctx.attempt, int(ctx.iteration or 1)
    )


def _decision_rationale(
    verdict: str,
    scores: Sequence[Mapping[str, Any]],
    selected_index: int | None,
    concerns: Sequence[str],
) -> str:
    if verdict == "approve" and selected_index is not None:
        best = next(
            (item for item in scores if item.get("method_index") == selected_index), None
        )
        total = best.get("total") if best else None
        return (
            f"隔离盲评通过：候选 method_index={selected_index} 五维总分 {total} 最高/最优，"
            f"关注点 {len(concerns)} 条。最终选型由 D2 风险策略裁决（评审分与策略动作分开存储）"
        )
    if verdict == "revise":
        return "隔离盲评判定 revise：候选质量未达门槛，回退 plan 重新生成（全局最多 1 次）"
    if verdict == "reject":
        return "隔离盲评判定 reject：全部候选被驳回，交 D4 失败处置裁决"
    return f"隔离盲评结论：{verdict}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# 落库与 D2 交接
# --------------------------------------------------------------------------- #
def _scores_changed(stored: Any, fresh: Sequence[Mapping[str, Any]]) -> bool:
    """判断本次评审结果是否与库中已有记录不同（决定旧人工标签是否失效）。"""
    current = [item for item in (stored or []) if isinstance(item, Mapping)]

    def canon(rows: Any) -> list[str]:
        return sorted(
            json.dumps(
                {
                    "alias": item.get("alias") or item.get("candidate_alias"),
                    "method_index": item.get("method_index"),
                    "total": item.get("total"),
                    "selected": bool(item.get("selected")),
                    "dims": [item.get(dimension) for dimension in DIMENSIONS],
                },
                sort_keys=True,
                default=str,
            )
            for item in rows
        )

    return canon(current) != canon(fresh)


async def _persist_calibration(
    ctx: StageContext,
    *,
    generator_ref: str,
    reviewer_ref: str,
    pack: Mapping[str, Any],
    model_scores: Sequence[Mapping[str, Any]],
) -> tuple[int | None, str | None]:
    """写 ``review_calibrations``（``status='pending'``，sample_size=0 直到有人工标签）。"""
    try:
        from sqlalchemy import select

        from app.db.models import ReviewCalibration
    except Exception as exc:  # noqa: BLE001 - 模块缺失不应让评审结论丢失
        return None, f"db_import_failed:{type(exc).__name__}"

    session = ctx.session
    if session is None:
        return None, "session_unavailable"

    try:
        existing = (
            await session.execute(
                select(ReviewCalibration)
                .where(ReviewCalibration.pipeline_run_id == int(ctx.pipeline_run_id))
                .where(ReviewCalibration.shuffle_seed == int(pack["seed"]))
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            changed = _scores_changed(existing.model_scores, model_scores)
            existing.model_scores = [dict(item) for item in model_scores]
            existing.candidate_order = [dict(item) for item in pack["candidate_order"]]
            existing.generator_model_ref = generator_ref
            existing.reviewer_model_ref = reviewer_ref
            if changed:
                # 评审结果变了 → 旧人工标签对新候选已失效，必须作废而不是沿用
                existing.human_labels = []
                existing.sample_size = 0
                existing.agreement_metric = None
                existing.agreement_value = None
                existing.confidence_interval = None
                existing.status = "pending"
            await session.commit()
            return int(existing.id), None

        row = ReviewCalibration(
            pipeline_run_id=int(ctx.pipeline_run_id),
            generator_model_ref=generator_ref,
            reviewer_model_ref=reviewer_ref,
            anonymization_version=blind.ANONYMIZATION_VERSION,
            shuffle_seed=int(pack["seed"]),
            candidate_order=[dict(item) for item in pack["candidate_order"]],
            model_scores=[dict(item) for item in model_scores],
            human_labels=[],
            sample_size=0,
            agreement_metric=None,
            agreement_value=None,
            confidence_interval=None,
            status="pending",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return int(row.id), None
    except Exception as exc:  # noqa: BLE001 - 落库失败必须可见但不得中断评审
        logger.warning("review_calibrations 写入失败 run=%s：%s", ctx.pipeline_run_id, exc, exc_info=True)
        with contextlib.suppress(Exception):
            await session.rollback()
        return None, f"insert_failed:{type(exc).__name__}:{exc}"


async def _request_d2(
    ctx: StageContext,
    scores: Sequence[Mapping[str, Any]],
    selected_index: int | None,
    verdict: str,
) -> dict[str, Any]:
    """把「评审五维分 + 候选列表」交给 WP10 的 D2 选型裁决（**评审分与策略动作分开存储**）。

    - ``persist=False``：D2 的 decision_log 由引擎在环节前的策略门禁统一落库，
      这里只取策略层对「带上评审分」的上下文给出的动作结论，避免同一决策点写两条日志；
    - WP10 未挂载/导入失败时**降级为 available=false**，绝不臆造策略动作。
    """
    context = {
        "stage": "plan_review",
        "decision_point": "D2",
        "attempt": ctx.attempt,
        "iteration": int(ctx.iteration or 1),
        "review_verdict": verdict,
        "review_scores_digest": [
            {
                "method_index": item.get("method_index"),
                "alias": item.get("candidate_alias"),
                "total": item.get("total"),
                "dimensions": {dimension: item.get(dimension) for dimension in DIMENSIONS},
            }
            for item in scores
        ],
        "options_considered": [
            f"select(method_index={item.get('method_index')})" for item in scores
        ],
        "action_plan": (
            {"method_index": selected_index} if selected_index is not None else {}
        ),
        "candidates_count": len(scores),
    }
    try:
        from app.services.pipeline.decision_engine import evaluate_decision
    except Exception as exc:  # noqa: BLE001 - WP10 未挂载属正常并行期状态
        return {"available": False, "reason": f"policy_not_mounted:{type(exc).__name__}"}

    try:
        outcome = await evaluate_decision(ctx.project_id, "D2", context, persist=False)
    except Exception as exc:  # noqa: BLE001 - 策略层异常不得让评审环节失败
        logger.warning("D2 策略调用失败（不影响评审结论）：%s", exc, exc_info=True)
        return {"available": False, "reason": f"policy_call_failed:{type(exc).__name__}"}

    if not isinstance(outcome, Mapping):
        return {"available": False, "reason": "policy_returned_non_mapping"}
    return {
        "available": True,
        "policy_action": outcome.get("policy_action") or (outcome.get("decision") or {}).get("action"),
        "chosen": outcome.get("chosen"),
        "risk_score": outcome.get("risk_score"),
        "confidence_score": outcome.get("confidence_score"),
        "reversibility_score": outcome.get("reversibility_score"),
        "rationale": outcome.get("rationale"),
        "policy_version": outcome.get("policy_version"),
        "degraded": outcome.get("degraded"),
        "persisted": False,
        "note": (
            "本结论为策略层对「带评审分上下文」的动作建议；D2 的 decision_logs 记录"
            "由流水线引擎在环节前门禁统一落库（评审分与策略动作分开存储）"
        ),
    }


class PlanReviewStage:
    """``plan_review`` 环节处理器（隔离盲评，决策点 D2）。"""

    name = "plan_review"
    decision_point = "D2"

    async def run(self, ctx: StageContext) -> StageResult:
        return await run(ctx)


STAGE = PlanReviewStage()


def register() -> PlanReviewStage:
    """把本环节注册进 WP09 的注册表（供 API 模块创建时调用，缺省幂等）。"""
    from app.services.pipeline.stages import register_stage

    return register_stage("plan_review", STAGE)  # type: ignore[return-value]


__all__ = [
    "PlanReviewStage",
    "REVIEW_SCHEMA",
    "REVISE_GLOBAL_MAX",
    "STAGE",
    "VERDICTS",
    "register",
    "run",
]
