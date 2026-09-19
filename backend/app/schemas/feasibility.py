# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""可行性与任务书域契约：``feasibilities`` / ``taskbooks``（附录 A.4 + 附录 B.3）。

口径要点
--------

- 四维（``data_availability`` / ``compute_cost`` / ``method_maturity`` /
  ``novelty_gap``）**必须分项落库**，每维形如 ``{score, rationale, evidence[]}``，
  禁止只存黑箱 ``total_score``（contracts.database.conventions 第 3 条）。
- 任务书是执行依据：``status='locked'`` 后流水线只读；``compute_budget`` 三键与
  contracts.guardrails 的双线成本口径对应（硬熔断线 / 演示配额）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.base import SciLoopModel

__all__ = [
    "ComputeBudget",
    "DimensionScore",
    "Feasibility",
    "FeasibilityRequest",
    "RiskItem",
    "Taskbook",
    "TaskbookCreateRequest",
    "TaskbookUpdateRequest",
]


class DimensionScore(SciLoopModel):
    """可行性某一维：分 + 理由 + 证据（缺证据就必须在 ``rationale`` 里说明）。"""

    score: float | None = Field(default=None, ge=0, le=100)
    rationale: str | None = None
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, description="paper_span / card_field / decision 等证据条目"
    )


class RiskItem(SciLoopModel):
    """``feasibilities.risk_list[]``。"""

    risk: str
    level: str | None = Field(
        default=None, description="L1 | L2 | L3（或 low/medium/high，保持原样）"
    )
    mitigation: str | None = None


class Feasibility(SciLoopModel):
    """``feasibilities``（v1.0 名 ``feasibility_reports``）。"""

    id: int | None = None
    idea_id: int
    data_availability: DimensionScore
    compute_cost: DimensionScore
    method_maturity: DimensionScore
    novelty_gap: DimensionScore
    total_score: float = Field(description="四维加权总分（分项必须同时落库，禁止只存本字段）")
    risk_list: list[RiskItem] = Field(default_factory=list)
    mve_plan: dict[str, Any] = Field(
        default_factory=dict, description="最小可行实验建议（含数据/算力/步骤/成功判据）"
    )
    created_at: datetime | None = None


class ComputeBudget(SciLoopModel):
    """``taskbooks.compute_budget``：与护栏双线口径一一对应。"""

    max_llm_cost_usd: float = Field(default=8.0, description="硬熔断线（工单不得放宽）")
    demo_cost_quota_usd: float = Field(default=3.0, description="演示配额：只告警不熔断")
    max_stage_minutes: int = Field(default=20, description="单环节上限，须 > 单 Run 上限")


class Taskbook(SciLoopModel):
    """``taskbooks``：锁定后即为流水线唯一执行依据。"""

    id: int | None = None
    project_id: int
    idea_id: int
    research_question: str
    target_datasets: list[Any] = Field(default_factory=list)
    baselines: list[Any] = Field(default_factory=list)
    metrics: list[Any] = Field(default_factory=list)
    compute_budget: ComputeBudget
    deliverables: list[str] = Field(
        default_factory=list, description="paper_draft | code | experiment_log"
    )
    max_iterations: int = Field(default=3, ge=1)
    score_threshold: float = Field(default=80.0, description="演示 Project 单独配 72")
    marginal_gain_threshold: float = Field(default=2.0)
    max_retry: int = Field(default=2, ge=0)
    status: str = Field(default="draft", description="draft | locked")
    locked_at: datetime | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 请求体（附录 B.3）
# --------------------------------------------------------------------------- #
class FeasibilityRequest(SciLoopModel):
    """``POST /feasibility``。"""

    idea_id: int


class TaskbookCreateRequest(SciLoopModel):
    """``POST /taskbooks``：只给必填项，其余走任务书默认值与系统默认。"""

    project_id: int
    idea_id: int
    research_question: str = Field(min_length=1)
    target_datasets: list[Any] = Field(default_factory=list)
    baselines: list[Any] = Field(default_factory=list)
    metrics: list[Any] = Field(default_factory=list)
    compute_budget: ComputeBudget | None = None
    deliverables: list[str] = Field(default_factory=list)


class TaskbookUpdateRequest(SciLoopModel):
    """``PATCH /taskbooks/{id}``：全部字段可选（``None`` = 不改）。"""

    research_question: str | None = None
    target_datasets: list[Any] | None = None
    baselines: list[Any] | None = None
    metrics: list[Any] | None = None
    compute_budget: ComputeBudget | None = None
    deliverables: list[str] | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    score_threshold: float | None = None
    marginal_gain_threshold: float | None = None
    max_retry: int | None = Field(default=None, ge=0)
