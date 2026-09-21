# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点输出契约：三节点的 Pydantic 模型 + 手写 JSON Schema。

两条口径
--------
1. **Schema 手写、不用 ``model_json_schema()``** —— ``additionalProperties: false``
   与显式 ``required`` 能给模型更明确的约束；与既有 ``PLAN_SCHEMA`` / ``CARD_SCHEMA``
   风格一致。Pydantic 模型只用于「拿到 payload 之后的类型化取值与默认值补齐」。
2. **契约只管形状，不管学术质量** —— 是否真有创新、结论是否正确，这里一律不判。
   形状与可核实事实由 ``rules.py`` 的 R1–R14 判定。

字段命名一律 snake_case，与既有 ``SciLoopModel`` 一致（基类不做 camelCase 转换）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from schemas.base import SciLoopModel

__all__ = [
    "EXPERIMENT_PREP_SCHEMA",
    "IDEA_FEASIBILITY_SCHEMA",
    "LITERATURE_REVIEW_SCHEMA",
    "NODE_OUTPUT_MODELS",
    "NODE_OUTPUT_SCHEMAS",
    "BaselinePlan",
    "ClosestWork",
    "DatasetPlan",
    "EvidenceDraft",
    "ExperimentPrepOutput",
    "FalsificationDraft",
    "FeasibilityDraft",
    "GapDraft",
    "Hypothesis",
    "IdeaAndFeasibilityOutput",
    "LiteratureReviewOutput",
    "MetricPlan",
    "NoveltyDelta",
    "PreflightRecord",
    "ProtocolDraft",
    "QueryRecord",
    "ResourceDraft",
]


# --------------------------------------------------------------------------- #
# ① 文献调研
# --------------------------------------------------------------------------- #
class QueryRecord(SciLoopModel):
    """一条检索记录（检索式 + 来源 + 命中数）。"""

    query_text: str
    source: str = Field(description="local_library / arxiv / semantic_scholar 等，如实填写")
    result_count: int = 0
    searched_at: str | None = None


class EvidenceDraft(SciLoopModel):
    """一条证据草稿（落 ``evidences`` 表之前的形状）。"""

    evidence_type: Literal["method", "result", "limitation", "gap"]
    paper_id: int
    card_field: str | None = Field(
        default=None,
        description="解析卡片字段名：research_problem / core_method / key_innovation / "
        "technical_route / experimental_setup / main_conclusions / limitations / transferable",
    )
    paper_span_id: int | None = Field(default=None, description="原文定位片段 id（可空）")
    quote_text: str | None = Field(default=None, description="原文引文（可定位来源的一部分）")
    relation: Literal["support", "refute", "neutral"] = "neutral"
    verification: Literal["verified", "abstract_only", "inferred"] = "inferred"


class ClosestWork(SciLoopModel):
    """最接近的工作（研究空白判断的锚点）。"""

    paper_id: int
    overlap: str = Field(description="重合在什么地方")
    remaining_difference: str = Field(description="仍然存在的差异")
    worth_continuing: bool = True


class GapDraft(SciLoopModel):
    """研究空白候选（可以为空，但「空」不等于「不存在」）。"""

    gap_text: str
    raised_by_paper_ids: list[int] = Field(default_factory=list)
    novelty_hint: str | None = None


class LiteratureReviewOutput(SciLoopModel):
    """文献调研节点产出。"""

    research_question: str
    entry_mode: Literal["paper_driven", "idea_driven"] = "paper_driven"
    queries: list[QueryRecord] = Field(default_factory=list)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    closest_work: list[ClosestWork] = Field(default_factory=list)
    coverage_note: str = Field(default="", description="检索覆盖范围与盲区")
    gaps: list[GapDraft] = Field(default_factory=list)
    recommended_queries: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list, description="仅摘要 / 未解析 / 无法访问清单")


# --------------------------------------------------------------------------- #
# ② idea 与可行性
# --------------------------------------------------------------------------- #
class Hypothesis(SciLoopModel):
    """可证伪假设。"""

    statement: str
    scope: str = Field(default="", description="限定条件")
    baseline: str = Field(default="", description="明确基线")
    mechanism: str = Field(default="", description="核心机制")
    expected_direction: str = Field(default="", description="主要指标或可观察现象的方向性变化")
    falsification_condition: str = Field(default="", description="可检验的否定条件")
    effect_size: str | None = Field(default=None, description="无依据时必须写「待确定」")


class NoveltyDelta(SciLoopModel):
    """与最接近工作的差异（新颖性判断的唯一依据）。"""

    paper_id: int
    overlap: str
    difference: str
    still_novel: bool = True


class FeasibilityDraft(SciLoopModel):
    """可行性报告草稿（落 ``feasibilities`` 表）。"""

    data_availability: Any = Field(default_factory=dict)
    compute_cost: Any = Field(default_factory=dict)
    method_maturity: Any = Field(default_factory=dict)
    novelty_gap: Any = Field(default_factory=dict)
    risk_list: list[Any] = Field(default_factory=list)
    mve_plan: Any = Field(default_factory=dict, description="最小验证方案")
    total_score: float = 0.0


class IdeaAndFeasibilityOutput(SciLoopModel):
    """idea 与可行性节点产出。"""

    hypothesis: Hypothesis
    novelty_delta: list[NoveltyDelta] = Field(default_factory=list)
    feasibility: FeasibilityDraft = Field(default_factory=FeasibilityDraft)
    recommendation: Literal["continue", "narrow", "more_reading", "stop"] = "continue"
    revert_request: dict[str, Any] | None = Field(
        default=None,
        description="模型建议回退时填写：{target, reason, carried:{...}}；无建议则为 null",
    )


# --------------------------------------------------------------------------- #
# ③ 实验与数据准备
# --------------------------------------------------------------------------- #
class DatasetPlan(SciLoopModel):
    """数据集计划（硬规则要求带版本与可访问性）。"""

    name: str
    version: str = Field(default="", description="数据集版本；未知写「未知」并说明")
    location: str = Field(default="", description="可定位来源：路径 / URL / 标识")
    access_ok: bool = False
    split: str = Field(default="", description="划分方式")


class BaselinePlan(SciLoopModel):
    """基线计划。"""

    name: str
    runnable: bool = False
    source: str = Field(default="")


class MetricPlan(SciLoopModel):
    """指标计划（主指标必须带单位与方向）。"""

    name: str
    formula: str = ""
    unit: str = ""
    direction: Literal["higher_better", "lower_better"] = "higher_better"
    is_primary: bool = False


class ProtocolDraft(SciLoopModel):
    """实验协议骨架。"""

    unit_of_analysis: str = ""
    independent_vars: list[str] = Field(default_factory=list)
    dependent_vars: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    confounders: list[str] = Field(default_factory=list)


class FalsificationDraft(SciLoopModel):
    """成功与失败判据。"""

    success_criteria: str = ""
    failure_criteria: str = ""


class ResourceDraft(SciLoopModel):
    """资源估算。"""

    compute_budget: str = ""
    estimated_hours: float | None = None
    storage: str = ""


class PreflightRecord(SciLoopModel):
    """小规模预检记录（三级预检之一）。"""

    level: Literal["template_smoke", "researcher_script", "isolated_runner"] = "template_smoke"
    command: str = ""
    exit_code: int = -1
    duration_ms: int = 0
    artifact_path: str | None = None
    log_path: str = ""
    note: str = ""


class ExperimentPrepOutput(SciLoopModel):
    """实验与数据准备节点产出。"""

    protocol: ProtocolDraft = Field(default_factory=ProtocolDraft)
    datasets: list[DatasetPlan] = Field(default_factory=list)
    baselines: list[BaselinePlan] = Field(default_factory=list)
    metrics: list[MetricPlan] = Field(default_factory=list)
    falsification: FalsificationDraft = Field(default_factory=FalsificationDraft)
    resources: ResourceDraft = Field(default_factory=ResourceDraft)
    preflight: PreflightRecord = Field(default_factory=PreflightRecord)
    revert_request: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 手写 JSON Schema
# --------------------------------------------------------------------------- #
_CARD_FIELDS = [
    "research_problem",
    "core_method",
    "key_innovation",
    "technical_route",
    "experimental_setup",
    "main_conclusions",
    "limitations",
    "transferable",
]

_REVERT_REQUEST = {
    "type": ["object", "null"],
    "properties": {
        "target": {"type": "string"},
        "reason": {"type": "string"},
        "carried": {
            "type": "object",
            "properties": {
                "overlap_evidence": {"type": "string"},
                "remaining_difference": {"type": "string"},
                "worth_continuing": {"type": "boolean"},
            },
        },
    },
    "required": ["target", "reason", "carried"],
}

LITERATURE_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "research_question",
        "entry_mode",
        "queries",
        "evidence",
        "closest_work",
        "coverage_note",
    ],
    "properties": {
        "research_question": {"type": "string", "minLength": 1},
        "entry_mode": {"enum": ["paper_driven", "idea_driven"]},
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query_text", "source"],
                "properties": {
                    "query_text": {"type": "string"},
                    "source": {"type": "string"},
                    "result_count": {"type": "integer"},
                    "searched_at": {"type": ["string", "null"]},
                },
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["evidence_type", "paper_id"],
                "properties": {
                    "evidence_type": {"enum": ["method", "result", "limitation", "gap"]},
                    "paper_id": {"type": "integer"},
                    "card_field": {"type": ["string", "null"], "enum": [*_CARD_FIELDS, None]},
                    "paper_span_id": {"type": ["integer", "null"]},
                    "quote_text": {"type": ["string", "null"]},
                    "relation": {"enum": ["support", "refute", "neutral"]},
                    "verification": {"enum": ["verified", "abstract_only", "inferred"]},
                },
            },
        },
        "closest_work": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["paper_id", "overlap", "remaining_difference"],
                "properties": {
                    "paper_id": {"type": "integer"},
                    "overlap": {"type": "string"},
                    "remaining_difference": {"type": "string"},
                    "worth_continuing": {"type": "boolean"},
                },
            },
        },
        "coverage_note": {"type": "string"},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["gap_text"],
                "properties": {
                    "gap_text": {"type": "string"},
                    "raised_by_paper_ids": {"type": "array", "items": {"type": "integer"}},
                    "novelty_hint": {"type": ["string", "null"]},
                },
            },
        },
        "recommended_queries": {"type": "array", "items": {"type": "string"}},
        "limits": {"type": "array", "items": {"type": "string"}},
    },
}

IDEA_FEASIBILITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypothesis", "novelty_delta", "feasibility", "recommendation"],
    "properties": {
        "hypothesis": {
            "type": "object",
            "additionalProperties": False,
            "required": ["statement", "falsification_condition"],
            "properties": {
                "statement": {"type": "string"},
                "scope": {"type": "string"},
                "baseline": {"type": "string"},
                "mechanism": {"type": "string"},
                "expected_direction": {"type": "string"},
                "falsification_condition": {"type": "string"},
                "effect_size": {"type": ["string", "null"]},
            },
        },
        "novelty_delta": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["paper_id", "overlap", "difference"],
                "properties": {
                    "paper_id": {"type": "integer"},
                    "overlap": {"type": "string"},
                    "difference": {"type": "string"},
                    "still_novel": {"type": "boolean"},
                },
            },
        },
        "feasibility": {
            "type": "object",
            "properties": {
                "data_availability": {},
                "compute_cost": {},
                "method_maturity": {},
                "novelty_gap": {},
                "risk_list": {"type": "array"},
                "mve_plan": {},
                "total_score": {"type": "number"},
            },
        },
        "recommendation": {"enum": ["continue", "narrow", "more_reading", "stop"]},
        "revert_request": _REVERT_REQUEST,
    },
}

EXPERIMENT_PREP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["protocol", "datasets", "baselines", "metrics", "falsification", "resources"],
    "properties": {
        "protocol": {
            "type": "object",
            "properties": {
                "unit_of_analysis": {"type": "string"},
                "independent_vars": {"type": "array", "items": {"type": "string"}},
                "dependent_vars": {"type": "array", "items": {"type": "string"}},
                "controls": {"type": "array", "items": {"type": "string"}},
                "confounders": {"type": "array", "items": {"type": "string"}},
            },
        },
        "datasets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "version", "access_ok"],
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "location": {"type": "string"},
                    "access_ok": {"type": "boolean"},
                    "split": {"type": "string"},
                },
            },
        },
        "baselines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "runnable"],
                "properties": {
                    "name": {"type": "string"},
                    "runnable": {"type": "boolean"},
                    "source": {"type": "string"},
                },
            },
        },
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "formula": {"type": "string"},
                    "unit": {"type": "string"},
                    "direction": {"enum": ["higher_better", "lower_better"]},
                    "is_primary": {"type": "boolean"},
                },
            },
        },
        "falsification": {
            "type": "object",
            "properties": {
                "success_criteria": {"type": "string"},
                "failure_criteria": {"type": "string"},
            },
        },
        "resources": {
            "type": "object",
            "properties": {
                "compute_budget": {"type": "string"},
                "estimated_hours": {"type": ["number", "null"]},
                "storage": {"type": "string"},
            },
        },
        "preflight": {
            "type": "object",
            "properties": {
                "level": {"enum": ["template_smoke", "researcher_script", "isolated_runner"]},
                "command": {"type": "string"},
                "exit_code": {"type": "integer"},
                "duration_ms": {"type": "integer"},
                "artifact_path": {"type": ["string", "null"]},
                "log_path": {"type": "string"},
                "note": {"type": "string"},
            },
        },
        "revert_request": _REVERT_REQUEST,
    },
}

NODE_OUTPUT_MODELS: dict[str, type[SciLoopModel]] = {
    "literature_review": LiteratureReviewOutput,
    "idea_and_feasibility": IdeaAndFeasibilityOutput,
    "experiment_and_data_preparation": ExperimentPrepOutput,
}

NODE_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "literature_review": LITERATURE_REVIEW_SCHEMA,
    "idea_and_feasibility": IDEA_FEASIBILITY_SCHEMA,
    "experiment_and_data_preparation": EXPERIMENT_PREP_SCHEMA,
}
