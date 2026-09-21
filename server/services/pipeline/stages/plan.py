# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``plan`` 环节：算法/实验方案生成（WP09-T4，附录 D.2）。

输入：任务书 + ``survey_output`` + 可行性报告
输出：``{methods:[{name, description, template_id, params, expected_metrics, rationale}],
recommended_index, reasoning}``
校验：``template_id`` 必须属于 T1–T5；``methods.length >= 2``
决策点：**无**（本环节只生成候选，最终选型在 ``plan_review`` / D2）

实现口径：

- 可行性报告若不存在（WP07/WP08 未落地或未跑），如实写入 ``feasibility_missing``，
  不编造四维分
- ``params.sample_size > 50`` 时按护栏上限收敛到 50 并显式记录降级（不静默）
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from db.models.pipeline import TEMPLATE_IDS
from services.pipeline.stages.base import (
    StageContext,
    StageResult,
    StageValidationError,
    call_llm,
    result_cost_of,
    unknown_cost_notes,
)

logger = logging.getLogger("sciloop.pipeline.stage.plan")

#: P0 模板（contracts.enums.template_p0），用于 prompt 中的优先建议（**不是**限制）
TEMPLATE_P0 = ("T1_prompt_variant", "T3_model_compare")

SAMPLE_SIZE_LIMIT = 50

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "methods": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 2},
                    "description": {"type": "string", "minLength": 2},
                    "template_id": {"type": "string", "enum": list(TEMPLATE_IDS)},
                    "params": {"type": "object"},
                    "expected_metrics": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["name", "description", "template_id"],
            },
        },
        "recommended_index": {"type": "integer", "minimum": 0},
        "reasoning": {"type": "string", "minLength": 1},
    },
    "required": ["methods", "recommended_index", "reasoning"],
}

_SYSTEM_PROMPT = (
    "你是科研辅助系统的「算法生成（plan）」模块。你的输出将作为结构化数据被程序消费。\n"
    "必须严格遵守：\n"
    "1) 只输出合法 JSON，不要任何解释性文字；\n"
    "2) methods 至少 2 套（否则无法进入隔离盲评），每套必须给出 template_id；\n"
    "3) template_id 只能是 " + ", ".join(TEMPLATE_IDS) + " 之一；\n"
    "4) params 中若包含 sample_size，必须 <= 50；\n"
    "5) 结论性字段必须可解释（rationale），不确定时写 \"unknown\"，禁止编造指标数值；\n"
    "6) 禁止生成任何指向投稿的表述。"
)


async def run(ctx: StageContext) -> StageResult:
    taskbook = ctx.taskbook_payload
    if not taskbook.get("research_question"):
        raise StageValidationError(
            "plan 环节要求任务书 research_question 非空（附录 D.2 输入契约）",
            level="L2",
            stage="plan",
        )

    survey_output = ctx.upstream("survey")
    if not survey_output:
        raise StageValidationError(
            "plan 环节缺少上游 survey 产出（断点续跑/顺序执行异常，L2）",
            level="L2",
            stage="plan",
            detail={"available_upstream": sorted(ctx.inputs)},
        )

    await ctx.progress(15, "读取任务书、survey 产出与可行性报告")
    feasibility, feasibility_note = await _load_feasibility(ctx)

    messages = _build_messages(taskbook, survey_output, feasibility, ctx)
    await ctx.progress(40, "调用 LLM 生成候选方案（≥2 套）")
    result = await call_llm(
        ctx,
        messages,
        json_schema=PLAN_SCHEMA,
        purpose="plan_generation",
        temperature=0.4,
    )
    payload = result.parsed if isinstance(result.parsed, dict) else None
    if payload is None:
        raise StageValidationError(
            "plan LLM 未返回可解析的 JSON（L1：格式解析失败，可自动重试）",
            level="L1",
            stage="plan",
            detail={"model_ref": result.model_ref, "content_head": (result.content or "")[:400]},
        )

    raw_methods = [m for m in payload.get("methods") or [] if isinstance(m, dict)]
    methods, normalize_notes, degradations = _normalize_methods(raw_methods, ctx)

    invalid_templates = [m["template_id_raw"] for m in methods if not m["template_id"]]
    if invalid_templates:
        raise StageValidationError(
            f"plan 产出的 template_id 不在 T1–T5 白名单内：{invalid_templates}（L1：格式类，可重试）",
            level="L1",
            stage="plan",
            detail={"allowed": list(TEMPLATE_IDS)},
        )

    if len(methods) < 2:
        raise StageValidationError(
            f"plan 产出候选方案仅 {len(methods)} 套，少于 2 套无法进入隔离盲评"
            "（附录 D.2 校验；L2：降低目标并补充候选）",
            level="L2",
            stage="plan",
            detail={"methods": [m["name"] for m in methods]},
        )

    for method in methods:
        method.pop("template_id_raw", None)

    recommended_index = _clean_index(payload.get("recommended_index"), len(methods))
    await ctx.progress(90, f"候选方案 {len(methods)} 套校验通过，写库")

    notes = list(unknown_cost_notes(result)) + normalize_notes
    if feasibility_note:
        notes.append(feasibility_note)
    if ctx.degrade:
        notes.append(f"本轮为 L2 降级重试，降级提示：{ctx.degrade}")

    output: dict[str, Any] = {
        "methods": methods,
        "recommended_index": recommended_index,
        "reasoning": str(payload.get("reasoning") or "").strip(),
        # ---- 审计附加字段 ----
        "recommended_index_note": (
            "仅为生成模型的推荐，最终选型由 plan_review 的隔离盲评与 D2 风险策略决定"
            "（本环节不产出最终选型）"
        ),
        "template_whitelist": list(TEMPLATE_IDS),
        "template_p0": list(TEMPLATE_P0),
        "feasibility": feasibility,
        "feasibility_missing": feasibility is None,
        "upstream": {"survey_queries": survey_output.get("queries") or []},
        "degradations": degradations,
        "decision": None,
        "model_ref": result.model_ref,
        "llm_prompt_hash": result.prompt_hash,
        "is_replay": bool(result.is_replay),
        "generated_at": _utc_now(),
    }
    return StageResult(
        output=output,
        cost_usd=result_cost_of(result),
        metrics={"methods": len(methods)},
        quality_ok=True,
        degradations=degradations,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
async def _load_feasibility(ctx: StageContext) -> tuple[dict[str, Any] | None, str | None]:
    """读取项目 idea 对应的可行性报告（附录 D.2 输入之一）。"""
    from db.models import Feasibility

    idea_id = getattr(ctx.project, "idea_id", None)
    if not idea_id:
        return None, "项目未关联 idea，可行性报告缺失（如实记录，不编造四维分）"
    row = (
        await ctx.session.execute(
            select(Feasibility)
            .where(Feasibility.idea_id == int(idea_id))
            .order_by(Feasibility.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None, f"可行性报告缺失（feasibilities 无 idea_id={idea_id} 记录，如实记录）"
    return (
        {
            "feasibility_id": int(row.id),
            "total_score": float(row.total_score) if row.total_score is not None else None,
            "data_availability": row.data_availability,
            "compute_cost": row.compute_cost,
            "method_maturity": row.method_maturity,
            "novelty_gap": row.novelty_gap,
            "risk_list": row.risk_list,
            "mve_plan": row.mve_plan,
        },
        None,
    )


def _normalize_methods(
    raw_methods: list[dict[str, Any]], ctx: StageContext
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    notes: list[str] = []
    degradations: list[str] = []
    methods: list[dict[str, Any]] = []
    degrade_sample = ctx.degrade.get("sample_size") if isinstance(ctx.degrade, dict) else None

    for raw in raw_methods:
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        template_raw = str(raw.get("template_id") or "").strip()
        template_id = template_raw if template_raw in TEMPLATE_IDS else ""
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        params = dict(params)
        if degrade_sample is not None:
            try:
                params["sample_size"] = int(degrade_sample)
                degradations.append(f"{name}: sample_size 按 L2 降级设为 {degrade_sample}")
            except (TypeError, ValueError):
                pass
        if "sample_size" in params:
            try:
                requested = int(params["sample_size"])
            except (TypeError, ValueError):
                requested = None
                notes.append(f"{name}: sample_size 非整数（{params.get('sample_size')!r}），已移除该参数")
                params.pop("sample_size", None)
            if requested is not None and requested > SAMPLE_SIZE_LIMIT:
                params["sample_size"] = SAMPLE_SIZE_LIMIT
                degradations.append(
                    f"{name}: sample_size {requested} 超过护栏上限 {SAMPLE_SIZE_LIMIT}，已收敛（透明记录）"
                )
            elif requested is not None and requested < 1:
                params["sample_size"] = 1
                degradations.append(f"{name}: sample_size {requested} < 1，已收敛为 1")
        methods.append(
            {
                "name": name or f"candidate_{len(methods) + 1}",
                "description": description or "unknown",
                "template_id": template_id,
                "template_id_raw": template_raw,
                "params": params,
                "expected_metrics": [
                    str(item) for item in (raw.get("expected_metrics") or []) if str(item).strip()
                ],
                "rationale": str(raw.get("rationale") or "").strip() or "unknown",
            }
        )
    return methods, notes, degradations


def _clean_index(value: Any, count: int) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(count - 1, index))


def _build_messages(
    taskbook: dict[str, Any],
    survey_output: dict[str, Any],
    feasibility: dict[str, Any] | None,
    ctx: StageContext,
) -> list[dict[str, Any]]:
    selected = survey_output.get("selected_papers") or []
    user = {
        "taskbook": taskbook,
        "survey_output": {
            "queries": survey_output.get("queries"),
            "selected_papers": [
                {
                    "paper_id": item.get("paper_id"),
                    "title": item.get("title"),
                    "venue": item.get("venue"),
                }
                for item in selected
                if isinstance(item, dict)
            ],
            "coverage_note": survey_output.get("coverage_note"),
            "gaps": survey_output.get("gaps"),
        },
        "feasibility": feasibility,
        "feasibility_missing": feasibility is None,
        "allowed_template_ids": list(TEMPLATE_IDS),
        "instruction": (
            "请基于任务书与文献调研结果生成 2-5 套可执行候选方案；每套给出 name / description / "
            "template_id（必须取自 allowed_template_ids）/ params（如含 sample_size 需 <=50）/ "
            "expected_metrics（列出要观测的指标名，不要编造数值）/ rationale。"
            "最后给出 recommended_index（0 基）与 reasoning。不要做最终选型（选型在 plan_review）。"
        ),
        "iteration": ctx.iteration,
        "attempt": ctx.attempt,
    }
    if ctx.degrade:
        user["degrade_hint"] = f"上一轮不达标，本轮按 L2 降级策略执行：{ctx.degrade}"
    if ctx.hints:
        user["human_hint"] = ctx.hints
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _json_dumps(user)},
    ]


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class PlanStage:
    """``plan`` 环节处理器。"""

    name = "plan"
    decision_point = None

    async def run(self, ctx: StageContext) -> StageResult:
        return await run(ctx)


STAGE = PlanStage()

__all__ = ["PLAN_SCHEMA", "STAGE", "PlanStage", "run"]
