# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""聚合与构思域契约：``aggregations`` / ``gaps`` / ``ideas`` / ``evidences``。

字段来源：附录 A.3（建表 DDL）+ 附录 B.2（``/aggregations``、``/ideas``）。

两条硬口径（contracts.evidence_rules）
------------------------------------

1. ``ideas.origin='ai_generated'`` 的 idea **必须至少挂 1 条 Evidence**，
   否则服务端直接丢弃并记 warning（故 :class:`IdeaWithEvidence` 的
   ``evidences`` 长度下限为 1）。
2. Evidence 是**多态**的：``owner_type`` 指向 idea / draft_claim / decision /
   feasibility / plan_review，``evidence_type`` 决定用哪个 id 字段，二者组合由
   ``app.services.evidence.resolver`` 解析为可跳转信息。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from app.schemas.base import SciLoopModel
from app.schemas.enums import EvidenceType

__all__ = [
    "Aggregation",
    "AggregationCreateRequest",
    "ComparisonMatrix",
    "Evidence",
    "Gap",
    "Idea",
    "IdeaCreateRequest",
    "IdeaGenerateRequest",
    "IdeaWithEvidence",
    "MethodEvolutionStep",
]


class Evidence(SciLoopModel):
    """``evidences``（多态属主 + 多态证据类型）。

    不做「id 字段必须与 ``evidence_type`` 一一匹配」的强校验：证据行由服务端构造，
    前端拿到的是解析后的可跳转信息；此处保留全字段是为了审计时能逐列核对。
    """

    id: int | None = None
    owner_type: str = Field(description="idea | draft_claim | decision | feasibility | plan_review")
    owner_id: int
    evidence_type: EvidenceType

    paper_id: int | None = None
    paper_span_id: int | None = None
    card_field: str | None = Field(
        default=None, description="卡片 8 字段之一：research_problem/core_method/key_innovation/…"
    )
    experiment_run_id: int | None = None
    experiment_passport_id: int | None = None
    decision_log_id: int | None = None

    metric_name: str | None = None
    metric_value: float | None = None
    quote_text: str | None = None
    weight: float = Field(default=1.0, description="证据权重，默认 1.0")
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _require_anchor(self) -> Evidence:
        """至少要有一种可定位锚点，否则这条证据不具备审计价值。"""
        anchors = (
            self.paper_span_id,
            self.paper_id,
            self.experiment_run_id,
            self.experiment_passport_id,
            self.decision_log_id,
        )
        if all(item is None for item in anchors):
            raise ValueError(
                "evidence 必须至少携带一个锚点（paper_span_id / paper_id / run / passport / decision）"
            )
        return self


class ComparisonMatrix(SciLoopModel):
    """``aggregations.comparison_matrix``：维度 + 行（每行一篇论文的取值）。"""

    dimensions: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(
        default_factory=list, description="[{paper_id, values{<dimension>: <取值|'unknown'>}}]"
    )


class MethodEvolutionStep(SciLoopModel):
    """``aggregations.method_evolution[]``。"""

    from_paper_id: int
    to_paper_id: int
    change: str
    evidence: Any = Field(default=None, description="paper_span 或卡片证据条目（多态，保持原样）")


class Aggregation(SciLoopModel):
    """``aggregations``。"""

    id: int | None = None
    project_id: int | None = None
    paper_ids: list[int] = Field(default_factory=list)
    comparison_matrix: ComparisonMatrix
    method_evolution: list[MethodEvolutionStep] = Field(default_factory=list)
    created_at: datetime | None = None


class Gap(SciLoopModel):
    """``gaps``：文献空白；``unsolved_evidence`` 必须是真实 paper_span 列表。"""

    id: int | None = None
    aggregation_id: int
    gap_text: str
    raised_by_paper_ids: list[int] = Field(default_factory=list)
    unsolved_evidence: list[dict[str, Any]] = Field(
        default_factory=list, description="paper_span 列表（含 document_version / quote_sha256）"
    )
    novelty_hint: str | None = None
    created_at: datetime | None = None


class Idea(SciLoopModel):
    """``ideas``。"""

    id: int | None = None
    project_id: int | None = Field(default=None, description="选中 idea 创建 Project 后回填")
    aggregation_id: int | None = None
    origin: str = Field(description="ai_generated | user_input")
    title: str
    content: str
    mechanism: str | None = Field(default=None, description="combination | transfer | refinement")
    novelty_note: str | None = None
    is_selected: bool = False
    created_at: datetime | None = None


class IdeaWithEvidence(Idea):
    """``POST /ideas/generate`` 的返回条目：**必须** 带 evidences（否则该条已被丢弃）。"""

    evidences: list[Evidence] = Field(min_length=1, description="硬校验：<1 条即被服务端丢弃")


# --------------------------------------------------------------------------- #
# 请求体（附录 B.2）
# --------------------------------------------------------------------------- #
class AggregationCreateRequest(SciLoopModel):
    """``POST /aggregations``（长任务）。"""

    paper_ids: list[int] = Field(min_length=2, description="少于 2 篇就没有『矩阵与演进』可言")


class IdeaGenerateRequest(SciLoopModel):
    """``POST /ideas/generate``。"""

    aggregation_id: int
    count: int = Field(default=3, ge=1, le=20)
    project_id: int | None = None


class IdeaCreateRequest(SciLoopModel):
    """``POST /ideas``（用户手输，``origin='user_input'``）。"""

    project_id: int | None = None
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
