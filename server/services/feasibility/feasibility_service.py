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
"""可行性报告编排与持久化（WP08-T5，附录 A.4 ``feasibilities``）。

一次 ``create_feasibility``：

1. 定位 idea 所属聚合 → 读回真实论文卡片（无卡片不产分）；
2. 汇总真实信号（:mod:`scorer.build_signal_bundle`）→ 四维分（规则层）+ 总分；
3. 生成 MVE（:mod:`mve_planner`）→ 风险清单（:mod:`risk_analyzer`，吃四维分与 MVE 假设）；
4. 落库 ``feasibilities`` 行；
5. **逐维绑定证据**（走 WP13 ``bind_evidence_detailed``，``owner_type='feasibility'``），
   把解析后的证据写回各维 ``evidence[]``；
6. 若某维绑定后证据为空，写入 ``evidence_note`` 并计入 ``dimensions_without_evidence``
   （**如实暴露**，不静默通过 WP08-A5「每维都有依据与证据」）。

LLM 只做「建议」，不做「打分」：``use_llm=True`` 时每维额外落
``llm_suggestion{model_ref, suggested_score, comment, cost_usd, is_replay}``，
**不参与 total_score**（规则层拥有最终决定权）。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Feasibility, Idea
from services.aggregation.cards import load_cards
from services.cost import accumulate
from services.evidence import bind_evidence_detailed
from services.feasibility import mve_planner, risk_analyzer, scorer

logger = logging.getLogger("sciloop.wp08.feasibility_service")

FEASIBILITY_OWNER_TYPE = "feasibility"

#: 四维列名 → 表列
_DIM_COLUMNS = {
    "data_availability": "data_availability",
    "compute_cost": "compute_cost",
    "method_maturity": "method_maturity",
    "novelty_gap": "novelty_gap",
}

#: 内存专用字段（落库前剔除）
_INTERNAL_KEYS = ("_cards",)

LLM_STAGE = "plan_review"
LLM_PURPOSE = "feasibility_rationale"
_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["suggested_score", "comment"],
    "properties": {
        "suggested_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "comment": {"type": "string"},
    },
}


class FeasibilityError(Exception):
    """可行性入参错误（API 层转 4xx）。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


async def _aggregation_paper_ids(session: AsyncSession, aggregation_id: int) -> list[int]:
    rows = (
        await session.execute(
            text("SELECT paper_ids FROM aggregations WHERE id = :id"),
            {"id": int(aggregation_id)},
        )
    ).mappings().all()
    if not rows:
        raise FeasibilityError(
            "aggregation_not_found",
            f"聚合 {aggregation_id} 不存在",
            {"aggregation_id": int(aggregation_id)},
        )
    return [int(pid) for pid in (rows[0]["paper_ids"] or [])]


def _strip_internal(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _INTERNAL_KEYS}


async def _llm_review(
    dimensions: Sequence[Mapping[str, Any]],
    *,
    model_ref: str | None,
    project_id: int | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """可选 LLM 复核：只产出建议分与说明，**不影响 rule score**。"""
    from llm.adapter import chat

    suggestions: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for dim in dimensions:
        try:
            result = await chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是科研可行性评审助手。只输出 JSON，禁止编造材料中没有的数据；"
                            "你的建议分仅供参考，最终分由规则层给出。"
                        ),
                    },
                    {"role": "user", "content": scorer.llm_suggestion_prompt({}, dim)},
                ],
                model_ref,
                0.2,
                600,
                _LLM_SCHEMA,
                project_id=project_id,
                stage=LLM_STAGE,
                purpose=LLM_PURPOSE,
            )
            parsed = result.parsed if isinstance(result.parsed, Mapping) else None
            suggestions[str(dim["key"])] = {
                "suggested_score": (parsed or {}).get("suggested_score"),
                "comment": (parsed or {}).get("comment"),
                "model_ref": result.model_ref,
                "provider": result.provider,
                "cost_usd": result.cost_usd,
                "is_replay": result.is_replay,
                "used_in_total": False,
                "note": "LLM 建议分不参与 total_score（规则层拥有最终决定权）",
            }
        except Exception as exc:  # noqa: BLE001 - LLM 不可用时不影响规则分
            errors.append(
                {"dimension": dim["key"], "type": type(exc).__name__, "message": str(exc)}
            )
            logger.warning("feasibility LLM 复核失败 dim=%s: %s", dim["key"], exc)
    return suggestions, errors


async def create_feasibility(
    session: AsyncSession,
    *,
    idea_id: int,
    project_id: int | None = None,
    aggregation_id: int | None = None,
    paper_ids: Sequence[int] | None = None,
    sample_size: int = 20,
    rounds: Mapping[str, Any] | None = None,
    use_llm: bool = False,
    model_ref: str | None = None,
) -> dict[str, Any]:
    """生成并落库可行性报告（含逐维证据绑定）。"""
    idea_row = (
        await session.execute(select(Idea).where(Idea.id == int(idea_id)))
    ).scalars().first()
    if idea_row is None:
        raise FeasibilityError(
            "idea_not_found", f"idea {idea_id} 不存在", {"idea_id": int(idea_id)}
        )
    idea = {
        "id": int(idea_row.id),
        "title": idea_row.title,
        "content": idea_row.content,
        "mechanism": idea_row.mechanism,
        "origin": idea_row.origin,
        "aggregation_id": idea_row.aggregation_id,
    }

    effective_aggregation = aggregation_id if aggregation_id is not None else idea_row.aggregation_id
    ids: list[int] = []
    if paper_ids:
        ids = [int(pid) for pid in paper_ids]
    elif effective_aggregation is not None:
        ids = await _aggregation_paper_ids(session, int(effective_aggregation))
    if not ids:
        raise FeasibilityError(
            "no_papers",
            "既未传入 paper_ids，idea 也未关联聚合：无法取真实论文与证据，拒绝打分",
            {"idea_id": int(idea_id), "aggregation_id": effective_aggregation},
        )

    effective_project = project_id if project_id is not None else idea_row.project_id
    cards = await load_cards(session, ids)
    if not cards:
        raise FeasibilityError(
            "no_cards",
            f"论文 {ids} 均无解析卡片：无法基于真实材料打分（禁止编造信号）",
            {"paper_ids": ids},
        )

    used_cost: float | None = None
    if effective_project is not None:
        try:
            summary = await accumulate(int(effective_project))
            used_cost = float(summary.used_usd)
        except Exception as exc:  # noqa: BLE001 - 记账不可用时不阻断规则分
            logger.warning("读取累计成本失败，compute_cost 将披露该信号缺失：%s", exc)
    else:
        logger.info("未指定 project_id：跳过累计成本信号（不跨项目求和，避免把别项目成本算进来）")

    bundle = scorer.build_signal_bundle(
        cards,
        idea=idea,
        max_llm_cost_usd=float(
            (rounds or {}).get("max_llm_cost_usd", scorer.DEFAULT_MAX_LLM_COST_USD)
        ),
        demo_cost_quota_usd=float(
            (rounds or {}).get("demo_cost_quota_usd", scorer.DEFAULT_DEMO_QUOTA_USD)
        ),
        estimated_cost_usd=(rounds or {}).get("estimated_cost_usd"),
        used_cost_usd=used_cost,
        sample_size=sample_size,
    )
    scoring = scorer.build_scoring_payload(bundle)
    dimensions = scoring["dimensions"]
    mve = mve_planner.build_mve_plan(
        idea=idea,
        bundle=bundle,
        dimensions=dimensions,
        project_id=effective_project,
        sample_size=sample_size,
        rounds=rounds,
    )
    risk = risk_analyzer.analyze(dimensions, bundle=bundle, mve_plan=mve)

    llm_suggestions: dict[str, dict[str, Any]] = {}
    llm_errors: list[dict[str, Any]] = []
    if use_llm:
        llm_suggestions, llm_errors = await _llm_review(
            dimensions, model_ref=model_ref, project_id=effective_project
        )

    dim_payloads: dict[str, dict[str, Any]] = {}
    for dim in dimensions:
        payload = _strip_internal(dim)
        payload["evidence"] = []
        payload["evidence_count"] = 0
        if dim["key"] in llm_suggestions:
            payload["llm_suggestion"] = llm_suggestions[dim["key"]]
        dim_payloads[dim["key"]] = payload

    row = Feasibility(
        idea_id=int(idea_id),
        data_availability=dim_payloads["data_availability"],
        compute_cost=dim_payloads["compute_cost"],
        method_maturity=dim_payloads["method_maturity"],
        novelty_gap=dim_payloads["novelty_gap"],
        total_score=Decimal(str(scoring["total_score"])),
        risk_list=risk["risk_list"],
        mve_plan=mve,
    )
    session.add(row)
    await session.flush()
    feasibility_id = int(row.id)

    binding_audit: dict[str, Any] = {}
    dims_without_evidence: list[str] = []
    for dim in scoring["dimensions"]:
        key = str(dim["key"])
        candidates = list(dim.get("evidence_candidates") or [])
        detail = await bind_evidence_detailed(
            FEASIBILITY_OWNER_TYPE, feasibility_id, candidates, session=session
        )
        bound = list(detail.get("bound") or [])
        payload = dim_payloads[key]
        payload["evidence"] = bound
        payload["evidence_count"] = len(bound)
        payload["evidence_ids"] = list(detail.get("bound_ids") or [])
        if not bound:
            dims_without_evidence.append(key)
            payload["evidence_note"] = (
                "该维未绑定到证据：候选被 WP13 拒绝（原因见 audit），"
                "请人工补充证据后重跑；本维分数仍可由 signals/formula 复算"
            )
            logger.warning(
                "feasibility#%s 维度 %s 无证据：rejected=%s",
                feasibility_id,
                key,
                [item.get("code") for item in (detail.get("rejected") or [])],
            )
        binding_audit[key] = {
            "submitted": len(candidates),
            "bound_ids": list(detail.get("bound_ids") or []),
            "rejected": list(detail.get("rejected") or []),
            "warnings": list(detail.get("warnings") or []),
        }

    row.data_availability = dim_payloads["data_availability"]
    row.compute_cost = dim_payloads["compute_cost"]
    row.method_maturity = dim_payloads["method_maturity"]
    row.novelty_gap = dim_payloads["novelty_gap"]
    await session.commit()

    logger.info(
        "create_feasibility id=%s idea=%s papers=%s total=%s risks=%s dims_without_evidence=%s",
        feasibility_id,
        idea_id,
        len(ids),
        scoring["total_score"],
        len(risk["risk_list"]),
        dims_without_evidence,
    )
    return {
        "id": feasibility_id,
        "feasibility_id": feasibility_id,
        "idea_id": int(idea_id),
        "project_id": effective_project,
        "aggregation_id": effective_aggregation,
        "paper_ids": ids,
        "dimensions": [dim_payloads[key] for key in (d["key"] for d in scoring["dimensions"])],
        "total_score": scoring["total_score"],
        "scoring": scoring["scoring"],
        "risk_list": risk["risk_list"],
        "risk_summary": {
            "risk_count": risk["risk_count"],
            "level_counts": risk["level_counts"],
            "highest_level": risk["highest_level"],
            "evaluated_rules": risk["evaluated_rules"],
            "thresholds": risk["thresholds"],
            "policy_note": risk["policy_note"],
        },
        "mve_plan": mve,
        "signal_bundle": _strip_internal(bundle),
        "evidence_binding": binding_audit,
        "dimensions_without_evidence": dims_without_evidence,
        "all_dimensions_have_evidence": not dims_without_evidence,
        "llm_suggestions": llm_suggestions,
        "llm_errors": llm_errors,
        "llm_used_in_total": False,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


def _dim_payload(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


async def get_feasibility(session: AsyncSession, feasibility_id: int) -> dict[str, Any] | None:
    """读回可行性报告（四维分 + 风险 + MVE）。"""
    row = (
        await session.execute(select(Feasibility).where(Feasibility.id == int(feasibility_id)))
    ).scalars().first()
    if row is None:
        return None
    dims = [
        _dim_payload(row.data_availability),
        _dim_payload(row.compute_cost),
        _dim_payload(row.method_maturity),
        _dim_payload(row.novelty_gap),
    ]
    without = [str(dim.get("key")) for dim in dims if not dim.get("evidence")]
    return {
        "id": int(row.id),
        "feasibility_id": int(row.id),
        "idea_id": int(row.idea_id),
        "dimensions": dims,
        "dimension_scores": {str(dim.get("key")): dim.get("score") for dim in dims},
        "total_score": float(row.total_score),
        "risk_list": list(row.risk_list or []),
        "risk_count": len(row.risk_list or []),
        "mve_plan": row.mve_plan or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "dimensions_without_evidence": without,
        "all_dimensions_have_evidence": not without,
        "scoring_note": "每维 {score, rationale, evidence[], signals, formula}；总分 = Σ(维度分×权重)",
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def latest_feasibility_for_idea(
    session: AsyncSession, idea_id: int
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(Feasibility)
            .where(Feasibility.idea_id == int(idea_id))
            .order_by(Feasibility.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    return await get_feasibility(session, int(row.id))


__all__ = [
    "FEASIBILITY_OWNER_TYPE",
    "FeasibilityError",
    "create_feasibility",
    "get_feasibility",
    "latest_feasibility_for_idea",
]
