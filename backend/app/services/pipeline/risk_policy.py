# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""风险三分计算（WP10-T2，计划书 §2.4 / contracts.risk_policy_rules）。

三个分数都在 ``0–100`` / ``0–1`` 区间，且**每一项特征都输出原值、归一值、权重与来源**，
便于评委逐项复算（禁止只给黑箱总分）：

- ``risk_score``       = 0.30*cost_exposure + 0.25*blast_radius + 0.20*evidence_gap
                         + 0.15*state_change + 0.10*external_dependency（特征先归一到 0–100）
- ``confidence_score`` = 0.40*evidence_coverage + 0.25*review_agreement
                         + 0.20*input_completeness + 0.15*historical_success_rate
- ``reversibility_score`` 按**动作域配置表**（检索式调整高、停止流水线低），
  并叠加少量显式修正项（如覆盖任务书、删除已有产出）。

红线：

- **可复现**：同输入同结果，模块内不使用任何随机源、不依赖时间。
- **取不到就缺失**：测量类特征拿不到真实值 → ``normalized=None`` 并**按剩余权重归一**，
  同时在 ``missing_features`` 里如实标注（禁止编造数值）。
- **不适用就剔除**：策略表 ``*_FEATURE_APPLICABILITY`` 声明该决策点不涉及的特征
  （如 D1 检索策略没有全文证据可谈）→ ``applicable=false``，既不算 0 也不算缺失。
- **LLM 只可建议**：``context['llm_suggested_scores']`` 只被记录、被标注
  ``applied=False``，**不参与任何计算**（规则层拥有最终决定权）。
- 阈值来自配置（``RISK_AUTO_MAX`` / ``DECISION_CONFIDENCE_AUTO_MIN`` /
  ``DECISION_REVERSIBILITY_AUTO_MIN``），不接受 action_plan 或 LLM 覆盖。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger("sciloop.pipeline.risk_policy")

WP_ID = "WP10"

#: 风险策略版本（写入 decision_logs.policy_version）
POLICY_VERSION = "wp10-risk-v1.0.0"
#: 归一化口径版本（口径变更必须递增，否则历史记录不可比）
NORMALIZATION_VERSION = "wp10-norm-v1.0.0"
#: 配置表版本（blast_radius / state_change / reversibility 等策略表）
TABLE_VERSION = "wp10-tables-v1.0.0"

RISK_FEATURE_WEIGHTS: dict[str, float] = {
    "cost_exposure": 0.30,
    "blast_radius": 0.25,
    "evidence_gap": 0.20,
    "state_change": 0.15,
    "external_dependency": 0.10,
}

CONFIDENCE_FEATURE_WEIGHTS: dict[str, float] = {
    "evidence_coverage": 0.40,
    "review_agreement": 0.25,
    "input_completeness": 0.20,
    "historical_success_rate": 0.15,
}

#: 风险特征的**适用性**（策略表，版本化）：不适用于该决策点的特征既不算缺失也不算 0，
#: 而是标注 ``applicable=false`` 并**从加权中剔除**——避免不相干的特征压低/抬高分数。
RISK_FEATURE_APPLICABILITY: dict[str, tuple[str, ...]] = {
    "D1": ("cost_exposure", "blast_radius", "state_change", "external_dependency"),
    "D2": ("cost_exposure", "blast_radius", "evidence_gap", "state_change", "external_dependency"),
    "D3": ("cost_exposure", "blast_radius", "evidence_gap", "state_change", "external_dependency"),
    "D4": ("cost_exposure", "blast_radius", "state_change", "external_dependency"),
    "D5": ("cost_exposure", "blast_radius", "evidence_gap", "state_change", "external_dependency"),
    "D6": ("cost_exposure", "blast_radius", "evidence_gap", "state_change", "external_dependency"),
}

#: 置信度特征的适用性：D1（检索策略）尚无全文证据与评审结论 → 证据类特征不适用
CONFIDENCE_FEATURE_APPLICABILITY: dict[str, tuple[str, ...]] = {
    "D1": ("input_completeness", "historical_success_rate"),
    "D2": ("review_agreement", "input_completeness", "historical_success_rate"),
    "D3": ("evidence_coverage", "input_completeness", "historical_success_rate"),
    "D4": ("input_completeness", "historical_success_rate"),
    "D5": (
        "evidence_coverage",
        "review_agreement",
        "input_completeness",
        "historical_success_rate",
    ),
    "D6": ("evidence_coverage", "input_completeness", "historical_success_rate"),
}

#: 动作域字段（用于输入完整度与动作是否「已声明」的判定）
ACTION_DOMAIN_FIELDS: dict[str, tuple[str, ...]] = {
    "D1": ("queries", "fields", "paper_limit"),
    "D2": ("method_index",),
    "D3": ("template_id", "params", "sample_size"),
    "D4": ("failure_action",),
    "D5": ("iteration_decision",),
    "D6": ("outline", "section_focus"),
}

#: 决策上下文基础字段（引擎/WP13 提供；动作域未声明时只计这些字段）
BASE_CONTEXT_FIELDS: tuple[str, ...] = (
    "stage",
    "attempt",
    "iteration",
    "mode",
    "upstream_stages",
)

#: 各决策点的候选动作（写入 decision_logs.options_considered）
ACTION_OPTIONS: dict[str, tuple[str, ...]] = {
    "D1": ("retain_queries", "adjust_queries", "expand_fields", "adjust_paper_limit"),
    "D2": ("select_method",),
    "D3": ("use_template_config", "reduce_sample_size", "switch_template"),
    "D4": ("retry", "downgrade", "switch_template", "switch_model", "circuit_break"),
    "D5": ("continue", "adjust_and_continue", "stop"),
    "D6": ("keep_outline", "revise_outline"),
}

#: 未声明具体动作域时的默认选择（不假装做了某个具体调整）
DEFAULT_CHOSEN: dict[str, str] = {
    "D1": "proceed_with_default_search",
    "D2": "select_best_candidate",
    "D3": "proceed_with_default_config",
    "D4": "retry",
    "D5": "continue",
    "D6": "proceed_with_default_outline",
}

#: 已声明动作域且自动执行时的首选动作
PRIMARY_CHOSEN: dict[str, str] = {
    "D1": "adjust_queries",
    "D2": "select_method",
    "D3": "use_template_config",
    "D4": "retry",
    "D5": "continue",
    "D6": "keep_outline",
}

#: 影响范围（0–100：受影响产物/环节/成本的规模）
BLAST_RADIUS_TABLE: dict[str, dict[str, float]] = {
    "D1": {"default": 25.0},
    "D2": {"default": 50.0},
    "D3": {"default": 70.0},
    "D4": {
        "default": 45.0,
        "retry": 30.0,
        "downgrade": 45.0,
        "switch_model": 40.0,
        "switch_template": 60.0,
        "circuit_break": 95.0,
    },
    "D5": {"default": 50.0, "continue": 45.0, "adjust_and_continue": 55.0, "stop": 85.0},
    "D6": {"default": 35.0, "keep_outline": 30.0, "revise_outline": 35.0},
}

#: 状态变更（0–100：是否改动既有产物/任务书/流水线状态）
STATE_CHANGE_TABLE: dict[str, dict[str, float]] = {
    "D1": {"default": 20.0},
    "D2": {"default": 45.0},
    "D3": {"default": 65.0},
    "D4": {
        "default": 40.0,
        "retry": 25.0,
        "downgrade": 45.0,
        "switch_model": 35.0,
        "switch_template": 55.0,
        "circuit_break": 90.0,
    },
    "D5": {"default": 45.0, "continue": 35.0, "adjust_and_continue": 55.0, "stop": 90.0},
    "D6": {"default": 30.0, "keep_outline": 25.0, "revise_outline": 30.0},
}

#: 外部依赖（0–100：依赖的外部源/模型数量基线，实测值可覆盖）
EXTERNAL_DEPENDENCY_TABLE: dict[str, float] = {
    "D1": 60.0,  # arXiv / Semantic Scholar / OpenAlex / GitHub
    "D2": 30.0,  # 生成模型 + 隔离评审模型
    "D3": 40.0,  # 执行器 + LLM
    "D4": 30.0,
    "D5": 20.0,
    "D6": 25.0,
}

#: 可逆性配置表（0–1；检索式调整高，停止流水线/覆盖任务书低）
REVERSIBILITY_TABLE: dict[str, dict[str, float]] = {
    "D1": {"default": 0.95, "retain_queries": 1.00, "adjust_queries": 0.95},
    "D2": {"default": 0.60, "select_method": 0.60},
    "D3": {
        "default": 0.55,
        "use_template_config": 0.55,
        "reduce_sample_size": 0.75,
        "switch_template": 0.45,
    },
    "D4": {
        "default": 0.70,
        "retry": 0.90,
        "downgrade": 0.70,
        "switch_model": 0.85,
        "switch_template": 0.45,
        "circuit_break": 0.80,
    },
    "D5": {"default": 0.70, "continue": 0.80, "adjust_and_continue": 0.75, "stop": 0.25},
    "D6": {"default": 0.80, "keep_outline": 0.95, "revise_outline": 0.80},
}

#: 覆盖任务书（改动已锁定的方案/任务书）→ 显著降低可逆性
TASKBOOK_OVERWRITE_MULTIPLIER = 0.60

#: 盲评结论 → 评审一致度基线（0–1，策略表；非测量值）
REVIEW_VERDICT_AGREEMENT: dict[str, float] = {
    "approve": 0.85,
    "revise": 0.55,
    "reject": 0.25,
}

#: 失败等级 → D4 首选处置（L1 重试 / L2 降级 / L3 熔断）
FAILURE_LEVEL_ACTION: dict[str, str] = {
    "L1": "retry",
    "L2": "downgrade",
    "L3": "circuit_break",
}

#: 历史成功率的最少样本数（样本不足则视为缺失，不宣称统计意义）
HISTORICAL_MIN_SAMPLES = 1


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _feature(
    name: str,
    *,
    raw: Any,
    normalized: float | None,
    weight: float,
    source: str,
    kind: str,
    note: str | None = None,
    applicable: bool = True,
) -> dict[str, Any]:
    """一条特征明细：原值 / 归一值 / 权重 / 来源 / 类型（测量值 or 策略表）。"""
    return {
        "name": name,
        "raw": raw,
        "normalized": None if normalized is None else round(float(normalized), 3),
        "weight": round(float(weight), 4),
        "source": source,
        "kind": kind,  # measured | policy_table | context_override
        "applicable": bool(applicable),
        "missing": bool(applicable) and normalized is None,
        "note": note,
    }


def _weighted_average(
    features: Mapping[str, Mapping[str, Any]], weights: Mapping[str, float]
) -> dict[str, Any]:
    """只在**适用且有值**的特征上按权重归一后平均（缺失降权、不适用剔除，均不改写成 0）。"""
    scoped = {name: item for name, item in features.items() if item.get("applicable", True)}
    available = {
        name: float(feature["normalized"])
        for name, feature in scoped.items()
        if feature.get("normalized") is not None
    }
    total_weight = sum(weights[name] for name in available if name in weights)
    missing = [name for name in weights if name not in available and name in scoped]
    not_applicable = [name for name in weights if name not in scoped]
    if total_weight <= 0:
        return {
            "value": None,
            "used_weight_total": 0.0,
            "effective_weights": {},
            "missing_features": missing,
            "not_applicable_features": not_applicable,
            "normalization": NORMALIZATION_VERSION,
        }
    value = sum(weights[name] * value_ for name, value_ in available.items()) / total_weight
    return {
        "value": value,
        "used_weight_total": round(total_weight, 6),
        "effective_weights": {
            name: round(weights[name] / total_weight, 6) for name in available if name in weights
        },
        "missing_features": missing,
        "not_applicable_features": not_applicable,
        "normalization": NORMALIZATION_VERSION,
    }


def table_lookup(table: Mapping[str, Any], decision_point: str, action: str | None) -> float:
    """策略表查找：``按动作 -> default`` 兜底（未知决策点返回 0，调用方须标注来源）。"""
    per_point = table.get(decision_point) or {}
    if not isinstance(per_point, Mapping):
        return 0.0
    if action and action in per_point:
        return float(per_point[action])
    return float(per_point.get("default", 0.0))


def _mark_applicability(
    features: dict[str, dict[str, Any]],
    table: Mapping[str, tuple[str, ...]],
    decision_point: str,
) -> None:
    """按适用性表标注：不适用 → ``applicable=false`` 且不参与加权（既非缺失也非 0）。"""
    allowed = table.get(decision_point)
    for name, feature in features.items():
        if allowed is not None and name not in allowed:
            feature["applicable"] = False
            feature["missing"] = False
            feature["normalized"] = None
            note = feature.get("note") or ""
            feature["note"] = (
                f"{note}｜{decision_point} 不适用该特征（策略表 applicability），不参与加权"
            ).strip("｜")


def resolve_thresholds() -> dict[str, Any]:
    """策略阈值（只来自配置；LLM/action_plan 不可覆盖）。"""
    auto_risk, auto_confidence, auto_reversibility = 30.0, 0.75, 0.60
    confidence_break_reserved = 0.50
    try:
        from app.core.config import get_settings

        settings = get_settings()
        auto_risk = float(getattr(settings, "risk_auto_max", auto_risk))
        auto_confidence = float(getattr(settings, "decision_confidence_auto_min", auto_confidence))
        auto_reversibility = float(
            getattr(settings, "decision_reversibility_auto_min", auto_reversibility)
        )
        confidence_break_reserved = float(
            getattr(settings, "decision_confidence_break_min", confidence_break_reserved)
        )
    except Exception:  # noqa: BLE001 - 配置不可用用契约默认值
        logger.warning("读取风险阈值失败，使用契约默认值", exc_info=True)
    return {
        "risk_auto_max": auto_risk,
        "confidence_auto_min": auto_confidence,
        "reversibility_auto_min": auto_reversibility,
        "confidence_break_min_reserved": confidence_break_reserved,
        "confidence_break_min_note": (
            "契约 action_rules 未定义该分支（仅有 auto/need_human/circuit_break 三条），"
            "本版本仅保留配置项、不启用"
        ),
        "overridable_by_llm": False,
        "source": "settings:RISK_AUTO_MAX/DECISION_CONFIDENCE_AUTO_MIN/DECISION_REVERSIBILITY_AUTO_MIN",
        "policy_version": POLICY_VERSION,
    }


def choose_action(
    decision_point: str,
    *,
    policy_action: str,
    context: Mapping[str, Any] | None = None,
    action_specified: bool = False,
) -> str:
    """依据策略动作与动作域，给出 ``decision_logs.chosen``（候选来自 ACTION_OPTIONS）。"""
    ctx = dict(context or {})
    options = ACTION_OPTIONS.get(decision_point, (policy_action,))
    proposed = ctx.get("proposed_action") or ctx.get("failure_action")
    if policy_action == "circuit_break":
        return "circuit_break"
    if policy_action == "need_human":
        return "request_human"
    if isinstance(proposed, str) and proposed.strip() in options:
        return proposed.strip()
    if decision_point == "D4":
        level = str(ctx.get("failure_level") or "").strip().upper()
        if level in FAILURE_LEVEL_ACTION:
            return FAILURE_LEVEL_ACTION[level]
    if action_specified:
        return PRIMARY_CHOSEN.get(decision_point, options[0])
    return DEFAULT_CHOSEN.get(decision_point, options[0])


# --------------------------------------------------------------------------- #
# 风险特征（0–100）
# --------------------------------------------------------------------------- #
def compute_risk_features(
    decision_point: str,
    context: Mapping[str, Any] | None,
    *,
    guardrail: Mapping[str, Any] | None = None,
    chosen: str | None = None,
) -> dict[str, dict[str, Any]]:
    """五个风险特征：原值 + 归一值 + 权重 + 来源。"""
    ctx = dict(context or {})
    action = chosen or ctx.get("proposed_action") or ctx.get("failure_action") or "default"
    features: dict[str, dict[str, Any]] = {}

    # ① cost_exposure：优先用护栏里的真实累计 + 本次预估（measured）
    exposure: dict[str, Any] = {
        "raw": None,
        "normalized": None,
        "source": "unavailable",
        "kind": "measured",
        "note": "缺护栏成本数据",
    }
    cost_detail = (guardrail or {}).get("detail") if isinstance(guardrail, Mapping) else None
    cost_block = (cost_detail or {}).get("cost") if isinstance(cost_detail, Mapping) else None
    check = (cost_block or {}).get("check") if isinstance(cost_block, Mapping) else None
    if isinstance(check, Mapping) and check.get("limit_usd"):
        limit = float(check["limit_usd"])
        projected = float(check.get("used_usd") or 0.0) + float(check.get("estimated_usd") or 0.0)
        exposure = {
            "raw": {
                "used_usd": float(check.get("used_usd") or 0.0),
                "estimated_usd": float(check.get("estimated_usd") or 0.0),
                "limit_usd": limit,
            },
            "normalized": _clamp(projected / limit * 100.0, 0.0, 100.0),
            "source": f"guardrail:cost(累计+预估)/{limit}*100",
            "kind": "measured",
            "note": None,
        }
    features["cost_exposure"] = _feature(
        "cost_exposure",
        raw=exposure["raw"],
        normalized=exposure["normalized"],
        weight=RISK_FEATURE_WEIGHTS["cost_exposure"],
        source=exposure["source"],
        kind=exposure["kind"],
        note=exposure["note"],
    )

    # ② blast_radius：context 显式给值优先，否则策略表
    explicit = _number(ctx.get("blast_radius"))
    if explicit is not None:
        features["blast_radius"] = _feature(
            "blast_radius",
            raw=explicit,
            normalized=_clamp(explicit, 0.0, 100.0),
            weight=RISK_FEATURE_WEIGHTS["blast_radius"],
            source="context:blast_radius",
            kind="context_override",
            note="调用方提供的实测/评估值",
        )
    else:
        base = table_lookup(BLAST_RADIUS_TABLE, decision_point, action)
        features["blast_radius"] = _feature(
            "blast_radius",
            raw=base,
            normalized=_clamp(base, 0.0, 100.0),
            weight=RISK_FEATURE_WEIGHTS["blast_radius"],
            source=f"table:blast_radius[{decision_point}/{action}]@{TABLE_VERSION}",
            kind="policy_table",
            note="动作域配置表（版本化）",
        )

    # ③ evidence_gap：由 evidence_coverage 反推（WP13 提供），缺失则如实标注
    coverage = _number(ctx.get("evidence_coverage"))
    if coverage is None and _number(ctx.get("papers_total")):
        total = _number(ctx.get("papers_total")) or 0.0
        with_text = _number(ctx.get("papers_with_fulltext"))
        if total > 0 and with_text is not None:
            coverage = _clamp(with_text / total, 0.0, 1.0)
    if coverage is None:
        features["evidence_gap"] = _feature(
            "evidence_gap",
            raw=None,
            normalized=None,
            weight=RISK_FEATURE_WEIGHTS["evidence_gap"],
            source="unavailable:evidence_coverage",
            kind="measured",
            note="WP13 未提供 evidence_coverage，该特征缺失并按剩余权重归一（不编造）",
        )
    else:
        normalized = _clamp((1.0 - coverage) * 100.0, 0.0, 100.0)
        features["evidence_gap"] = _feature(
            "evidence_gap",
            raw={"evidence_coverage": round(coverage, 4)},
            normalized=normalized,
            weight=RISK_FEATURE_WEIGHTS["evidence_gap"],
            source="context:evidence_coverage→(1-coverage)*100",
            kind="measured",
        )

    # ④ state_change
    explicit_change = _number(ctx.get("state_change"))
    if explicit_change is not None:
        features["state_change"] = _feature(
            "state_change",
            raw=explicit_change,
            normalized=_clamp(explicit_change, 0.0, 100.0),
            weight=RISK_FEATURE_WEIGHTS["state_change"],
            source="context:state_change",
            kind="context_override",
            note="调用方提供的实测/评估值",
        )
    else:
        base = table_lookup(STATE_CHANGE_TABLE, decision_point, action)
        features["state_change"] = _feature(
            "state_change",
            raw=base,
            normalized=_clamp(base, 0.0, 100.0),
            weight=RISK_FEATURE_WEIGHTS["state_change"],
            source=f"table:state_change[{decision_point}/{action}]@{TABLE_VERSION}",
            kind="policy_table",
            note="动作域配置表（版本化）",
        )

    # ⑤ external_dependency：优先实测（外部调用数/外部源数），否则策略表
    calls = _number(ctx.get("external_calls"))
    sources = ctx.get("external_sources")
    if calls is not None:
        features["external_dependency"] = _feature(
            "external_dependency",
            raw={"external_calls": calls},
            normalized=_clamp(calls * 10.0, 0.0, 100.0),
            weight=RISK_FEATURE_WEIGHTS["external_dependency"],
            source="context:external_calls*10",
            kind="measured",
        )
    elif isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)) and sources:
        features["external_dependency"] = _feature(
            "external_dependency",
            raw={"external_sources": list(sources)},
            normalized=_clamp(len(sources) * 25.0, 0.0, 100.0),
            weight=RISK_FEATURE_WEIGHTS["external_dependency"],
            source="context:len(external_sources)*25",
            kind="measured",
        )
    else:
        base = float(EXTERNAL_DEPENDENCY_TABLE.get(decision_point, 30.0))
        features["external_dependency"] = _feature(
            "external_dependency",
            raw=base,
            normalized=base,
            weight=RISK_FEATURE_WEIGHTS["external_dependency"],
            source=f"table:external_dependency[{decision_point}]@{TABLE_VERSION}",
            kind="policy_table",
            note="决策点外部依赖基线（版本化）",
        )

    _mark_applicability(features, RISK_FEATURE_APPLICABILITY, decision_point)
    return features


# --------------------------------------------------------------------------- #
# 置信度特征（0–1）
# --------------------------------------------------------------------------- #
def input_completeness(
    decision_point: str,
    context: Mapping[str, Any] | None,
    *,
    action_specified: bool,
) -> dict[str, Any]:
    """输入完整度 = 本次判定所需上下文字段的可得比例（不编造，缺失字段逐条列出）。

    动作域字段同时接受两种承载方式：``context["action_domain"]`` 内的嵌套键
    与 ``context`` 顶层同名键（引擎经 ``action_domain`` 传递，验收脚本可用顶层键）。
    """
    ctx = dict(context or {})
    domain = ctx.get("action_domain")
    nested = dict(domain) if isinstance(domain, Mapping) else {}

    def _has(name: str) -> bool:
        return _present(ctx.get(name)) or _present(nested.get(name))

    fields = list(BASE_CONTEXT_FIELDS)
    if action_specified:
        fields.extend(ACTION_DOMAIN_FIELDS.get(decision_point, ()))
    present = [name for name in fields if _has(name)]
    missing = [name for name in fields if name not in present]
    score = (len(present) / len(fields)) if fields else None
    return {
        "score": score,
        "scored_fields": fields,
        "present": present,
        "missing": missing,
        "action_specified": action_specified,
    }


def compute_confidence_features(
    decision_point: str,
    context: Mapping[str, Any] | None,
    *,
    historical_success_rate: float | None = None,
    action_specified: bool = False,
) -> dict[str, dict[str, Any]]:
    """四个置信度特征：evidence_coverage / review_agreement / input_completeness / history。"""
    ctx = dict(context or {})
    features: dict[str, dict[str, Any]] = {}

    # ① evidence_coverage
    coverage = _number(ctx.get("evidence_coverage"))
    if coverage is None and _number(ctx.get("papers_total")):
        total = _number(ctx.get("papers_total")) or 0.0
        with_text = _number(ctx.get("papers_with_fulltext"))
        if total > 0 and with_text is not None:
            coverage = with_text / total
    if coverage is None:
        features["evidence_coverage"] = _feature(
            "evidence_coverage",
            raw=None,
            normalized=None,
            weight=CONFIDENCE_FEATURE_WEIGHTS["evidence_coverage"],
            source="unavailable:evidence_coverage",
            kind="measured",
            note="无证据覆盖数据（WP13 未提供）→ 缺失并降权",
        )
    else:
        features["evidence_coverage"] = _feature(
            "evidence_coverage",
            raw=round(float(coverage), 4),
            normalized=_clamp(float(coverage), 0.0, 1.0),
            weight=CONFIDENCE_FEATURE_WEIGHTS["evidence_coverage"],
            source="context:evidence_coverage",
            kind="measured",
        )

    # ② review_agreement
    explicit = _number(ctx.get("review_agreement"))
    verdict = str(ctx.get("review_verdict") or "").strip().lower()
    if explicit is not None:
        features["review_agreement"] = _feature(
            "review_agreement",
            raw=explicit,
            normalized=_clamp(explicit, 0.0, 1.0),
            weight=CONFIDENCE_FEATURE_WEIGHTS["review_agreement"],
            source="context:review_agreement",
            kind="context_override",
        )
    elif verdict in REVIEW_VERDICT_AGREEMENT:
        features["review_agreement"] = _feature(
            "review_agreement",
            raw={"review_verdict": verdict},
            normalized=REVIEW_VERDICT_AGREEMENT[verdict],
            weight=CONFIDENCE_FEATURE_WEIGHTS["review_agreement"],
            source=f"table:review_verdict_agreement[{verdict}]@{TABLE_VERSION}",
            kind="policy_table",
            note="盲评结论一致度基线（非测量值）",
        )
    else:
        features["review_agreement"] = _feature(
            "review_agreement",
            raw=None,
            normalized=None,
            weight=CONFIDENCE_FEATURE_WEIGHTS["review_agreement"],
            source="unavailable:review_agreement",
            kind="measured",
            note="该决策点尚无评审结论 → 缺失并降权",
        )

    # ③ input_completeness
    completeness = input_completeness(decision_point, ctx, action_specified=action_specified)
    features["input_completeness"] = _feature(
        "input_completeness",
        raw={
            "present": completeness["present"],
            "missing": completeness["missing"],
            "action_specified": action_specified,
        },
        normalized=completeness["score"],
        weight=CONFIDENCE_FEATURE_WEIGHTS["input_completeness"],
        source="context:字段可得比例",
        kind="measured",
    )

    # ④ historical_success_rate（由调用方查库后传入；样本不足视为缺失）
    rate = historical_success_rate if historical_success_rate is not None else None
    if rate is None:
        rate = _number(ctx.get("historical_success_rate"))
        source = "context:historical_success_rate"
        kind = "context_override"
        raw: Any = rate
        if rate is None:
            features["historical_success_rate"] = _feature(
                "historical_success_rate",
                raw=None,
                normalized=None,
                weight=CONFIDENCE_FEATURE_WEIGHTS["historical_success_rate"],
                source="unavailable:stage_outputs",
                kind="measured",
                note="无同环节历史样本 → 缺失并降权（不编造成功率）",
            )
            _mark_applicability(features, CONFIDENCE_FEATURE_APPLICABILITY, decision_point)
            return features
    else:
        source = "db:stage_outputs(done/attempts)"
        kind = "measured"
        raw = {"historical_success_rate": round(float(rate), 4)}
    features["historical_success_rate"] = _feature(
        "historical_success_rate",
        raw=raw,
        normalized=_clamp(float(rate), 0.0, 1.0),
        weight=CONFIDENCE_FEATURE_WEIGHTS["historical_success_rate"],
        source=source,
        kind=kind,
    )
    _mark_applicability(features, CONFIDENCE_FEATURE_APPLICABILITY, decision_point)
    return features


def historical_success_rate_from_rows(rows: Sequence[str]) -> dict[str, Any]:
    """由 ``stage_outputs.status`` 序列计算成功率（实测量；样本数一并披露）。"""
    statuses = [str(item).strip().lower() for item in rows if str(item or "").strip()]
    finished = [item for item in statuses if item in {"done", "failed", "circuit_break"}]
    if len(finished) < HISTORICAL_MIN_SAMPLES:
        return {
            "value": None,
            "samples": len(finished),
            "done": 0,
            "note": f"可用样本 {len(finished)} < {HISTORICAL_MIN_SAMPLES}，视为缺失",
        }
    done = sum(1 for item in finished if item == "done")
    return {
        "value": done / len(finished),
        "samples": len(finished),
        "done": done,
        "note": None,
    }


# --------------------------------------------------------------------------- #
# 可逆性（0–1，配置表 + 显式修正）
# --------------------------------------------------------------------------- #
def compute_reversibility(
    decision_point: str,
    context: Mapping[str, Any] | None,
    *,
    chosen: str | None = None,
) -> dict[str, Any]:
    """可逆性：动作域配置表为基线，叠加显式修正项（覆盖任务书 / 移除已有产出）。"""
    ctx = dict(context or {})
    action = chosen or ctx.get("proposed_action") or "default"
    explicit = _number(ctx.get("reversibility_score"))
    modifiers: list[dict[str, Any]] = []
    if explicit is not None:
        base_source = "context:reversibility_score"
        base = _clamp(explicit, 0.0, 1.0)
        kind = "context_override"
    else:
        base = table_lookup(REVERSIBILITY_TABLE, decision_point, action)
        base_source = f"table:reversibility[{decision_point}/{action}]@{TABLE_VERSION}"
        kind = "policy_table"

    value = base
    if ctx.get("touches_taskbook"):
        value *= TASKBOOK_OVERWRITE_MULTIPLIER
        modifiers.append(
            {
                "name": "taskbook_overwrite",
                "multiplier": TASKBOOK_OVERWRITE_MULTIPLIER,
                "source": "context:touches_taskbook",
                "note": "覆盖已锁定任务书 → 可逆性显著降低",
            }
        )
    if ctx.get("removes_artifacts"):
        value *= 0.5
        modifiers.append(
            {
                "name": "removes_artifacts",
                "multiplier": 0.5,
                "source": "context:removes_artifacts",
                "note": "删除已有产出 → 可逆性降低",
            }
        )
    criterion = ctx.get("stop_reason_criterion")
    if str(action) == "stop" and criterion == "manual":
        modifiers.append(
            {
                "name": "stop_requires_human",
                "multiplier": 1.0,
                "source": "context:stop_reason_criterion",
                "note": "以人工判据停止流水线（不可逆动作，人工确认后执行）",
            }
        )
    value = _clamp(value, 0.0, 1.0)
    return {
        "raw": {"base": round(base, 4), "action": action},
        "base": round(base, 4),
        "normalized": round(value, 4),
        "weight": 1.0,
        "source": base_source,
        "kind": kind,
        "modifiers": modifiers,
        "missing": False,
    }


# --------------------------------------------------------------------------- #
# 三分汇总
# --------------------------------------------------------------------------- #
def _llm_suggestion(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """记录 LLM 建议分（**applied 恒为 False**，规则层不受其影响）。"""
    raw = context.get("llm_suggested_scores") or context.get("llm_suggestion")
    if not isinstance(raw, Mapping):
        return None
    suggestion: dict[str, Any] = {
        "applied": False,
        "note": "LLM 仅提供建议分，不参与最终判定（规则层拥有最终决定权）",
    }
    for key in ("risk_score", "confidence_score", "reversibility_score", "model_ref", "note"):
        if key in raw:
            suggestion[key] = raw[key]
    return suggestion


def compute_scores(
    decision_point: str,
    context: Mapping[str, Any] | None = None,
    *,
    guardrail: Mapping[str, Any] | None = None,
    historical_success_rate: float | None = None,
    chosen: str | None = None,
    action_specified: bool = False,
) -> dict[str, Any]:
    """计算风险三分（纯函数，可复现）：返回分数 + 全量特征明细 + 审计信息。"""
    point = str(decision_point or "").strip().upper() or "D1"
    ctx = dict(context or {})
    action = chosen or choose_action(
        point,
        policy_action="auto_execute",
        context=ctx,
        action_specified=action_specified,
    )

    risk_features = compute_risk_features(point, ctx, guardrail=guardrail, chosen=action)
    confidence_features = compute_confidence_features(
        point,
        ctx,
        historical_success_rate=historical_success_rate,
        action_specified=action_specified,
    )
    reversibility = compute_reversibility(point, ctx, chosen=action)

    risk_agg = _weighted_average(risk_features, RISK_FEATURE_WEIGHTS)
    confidence_agg = _weighted_average(confidence_features, CONFIDENCE_FEATURE_WEIGHTS)

    risk_score = None if risk_agg["value"] is None else round(float(risk_agg["value"]), 2)
    confidence_score = (
        None if confidence_agg["value"] is None else round(float(confidence_agg["value"]), 3)
    )
    reversibility_score = reversibility["normalized"]

    features: dict[str, Any] = {
        "risk": risk_features,
        "confidence": confidence_features,
        "reversibility": reversibility,
        "aggregation": {
            "risk": risk_agg,
            "confidence": confidence_agg,
            "reversibility": {"value": reversibility_score, "effective_weights": {"base": 1.0}},
        },
        "feature_schema_version": NORMALIZATION_VERSION,
        "tables_version": TABLE_VERSION,
        "missing_features": sorted(
            {
                *risk_agg["missing_features"],
                *confidence_agg["missing_features"],
            }
        ),
        "not_applicable_features": sorted(
            {
                *risk_agg.get("not_applicable_features", []),
                *confidence_agg.get("not_applicable_features", []),
            }
        ),
        "weights": {
            "risk": RISK_FEATURE_WEIGHTS,
            "confidence": CONFIDENCE_FEATURE_WEIGHTS,
        },
        "applicability_tables": {
            "risk": {key: list(value) for key, value in RISK_FEATURE_APPLICABILITY.items()},
            "confidence": {
                key: list(value) for key, value in CONFIDENCE_FEATURE_APPLICABILITY.items()
            },
        },
        "action": action,
    }
    suggestion = _llm_suggestion(ctx)
    if suggestion is not None:
        features["llm_suggestion"] = suggestion

    return {
        "decision_point": point,
        "risk_score": risk_score,
        "confidence_score": confidence_score,
        "reversibility_score": reversibility_score,
        "features": features,
        "thresholds": resolve_thresholds(),
        "policy_version": POLICY_VERSION,
        "missing_features": list(features["missing_features"]),
        "reproducible": True,
        "incomplete": risk_score is None or confidence_score is None,
    }


# --------------------------------------------------------------------------- #
# 策略动作判定（规则层拥有最终决定权）
# --------------------------------------------------------------------------- #
def decide_action(
    *,
    risk_score: float | None,
    confidence_score: float | None,
    reversibility_score: float | None,
    guardrail_failed: bool = False,
    guardrail_first_failure: str | None = None,
    retry_exhausted: bool = False,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """三分 + 护栏 → ``auto_execute`` / ``need_human`` / ``circuit_break``。

    判定顺序（contracts.risk_policy_rules.action_rules）：

    1. 硬护栏任一失败 → ``circuit_break``（阈值不可覆盖）
    2. 重试次数耗尽 → ``circuit_break``
    3. risk<=30 且 confidence>=0.75 且 reversibility>=0.60 → ``auto_execute``
    4. 其余（护栏通过）→ ``need_human``

    分数缺失（``None``）时**一律不自动执行**：缺证据不足以支撑自动放行。
    """
    limits = dict(thresholds or resolve_thresholds())
    considered = ["auto_execute", "need_human", "circuit_break"]
    if guardrail_failed:
        return {
            "policy_action": "circuit_break",
            "reason": f"guardrail_failed:{guardrail_first_failure or 'unknown'}",
            "detail": "硬护栏失败 → 直接熔断（阈值不可覆盖）",
            "conditions": {
                "guardrail_ok": False,
                "retry_exhausted": retry_exhausted,
            },
            "options_considered": considered,
        }
    if retry_exhausted:
        return {
            "policy_action": "circuit_break",
            "reason": "retry_exhausted",
            "detail": "重试次数耗尽 → 熔断并转人工（计划书 §2.5.2 L3）",
            "conditions": {"guardrail_ok": True, "retry_exhausted": True},
            "options_considered": considered,
        }

    risk_ok = risk_score is not None and risk_score <= float(limits["risk_auto_max"])
    confidence_ok = confidence_score is not None and confidence_score >= float(
        limits["confidence_auto_min"]
    )
    reversibility_ok = reversibility_score is not None and reversibility_score >= float(
        limits["reversibility_auto_min"]
    )
    conditions = {
        "guardrail_ok": True,
        "retry_exhausted": False,
        "risk_ok": risk_ok,
        "confidence_ok": confidence_ok,
        "reversibility_ok": reversibility_ok,
        "risk_auto_max": limits["risk_auto_max"],
        "confidence_auto_min": limits["confidence_auto_min"],
        "reversibility_auto_min": limits["reversibility_auto_min"],
    }
    if risk_ok and confidence_ok and reversibility_ok:
        return {
            "policy_action": "auto_execute",
            "reason": "auto_conditions_met",
            "detail": (
                f"风险 {risk_score}≤{limits['risk_auto_max']}、"
                f"置信度 {confidence_score}≥{limits['confidence_auto_min']}、"
                f"可逆性 {reversibility_score}≥{limits['reversibility_auto_min']} → 自动执行"
            ),
            "conditions": conditions,
            "options_considered": considered,
        }

    unmet = [
        name
        for name, ok in (
            ("risk<=max", risk_ok),
            ("confidence>=min", confidence_ok),
            ("reversibility>=min", reversibility_ok),
        )
        if not ok
    ]
    return {
        "policy_action": "need_human",
        "reason": f"auto_conditions_unmet:{'+'.join(unmet)}",
        "detail": f"未满足自动执行条件（{'、'.join(unmet)}）→ 转人工确认",
        "conditions": conditions,
        "options_considered": considered,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "缺失"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def build_rationale(
    *,
    decision_point: str,
    decision: Mapping[str, Any],
    scores: Mapping[str, Any],
    guardrail: Mapping[str, Any] | None,
    extra: str | None = None,
) -> str:
    """生成可审计的判定理由（只陈述真实计算结果与缺失项）。"""
    parts = [
        f"{decision_point} 策略判定：{decision['policy_action']}",
        f"风险={_fmt(scores.get('risk_score'))}/100",
        f"置信度={_fmt(scores.get('confidence_score'))}",
        f"可逆性={_fmt(scores.get('reversibility_score'))}",
        str(decision.get("detail") or decision.get("reason") or ""),
    ]
    if guardrail:
        parts.append(
            "护栏 safety={safety_ok}/time={time_ok}/cost={cost_ok}（评估 {ev}；跳过 {sk}）".format(
                safety_ok=guardrail.get("safety_ok"),
                time_ok=guardrail.get("time_ok"),
                cost_ok=guardrail.get("cost_ok"),
                ev=",".join(guardrail.get("evaluated") or []) or "-",
                sk=",".join(guardrail.get("skipped") or []) or "-",
            )
        )
        if guardrail.get("first_failure"):
            parts.append(f"首个失败护栏={guardrail['first_failure']}（短路，后续未评估）")
    missing = list(scores.get("missing_features") or [])
    parts.append(f"缺失特征={','.join(missing) if missing else '无'}（已按剩余权重归一）")
    not_applicable = list((scores.get("features") or {}).get("not_applicable_features") or [])
    if not_applicable:
        parts.append(f"不适用特征={','.join(not_applicable)}（策略表 applicability，不计入）")
    suggestion = (scores.get("features") or {}).get("llm_suggestion")
    if suggestion:
        parts.append("LLM 建议分仅记录未采用（applied=False）")
    if extra:
        parts.append(extra)
    return "；".join(part for part in parts if part)


__all__ = [
    "ACTION_DOMAIN_FIELDS",
    "ACTION_OPTIONS",
    "BLAST_RADIUS_TABLE",
    "CONFIDENCE_FEATURE_APPLICABILITY",
    "CONFIDENCE_FEATURE_WEIGHTS",
    "NORMALIZATION_VERSION",
    "POLICY_VERSION",
    "REVERSIBILITY_TABLE",
    "RISK_FEATURE_APPLICABILITY",
    "RISK_FEATURE_WEIGHTS",
    "STATE_CHANGE_TABLE",
    "TABLE_VERSION",
    "build_rationale",
    "choose_action",
    "compute_confidence_features",
    "compute_reversibility",
    "compute_risk_features",
    "compute_scores",
    "decide_action",
    "historical_success_rate_from_rows",
    "input_completeness",
    "resolve_thresholds",
    "table_lookup",
]
