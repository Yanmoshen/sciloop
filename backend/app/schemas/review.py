# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""评审 / 草稿 / Claim 域契约：``review_scores`` / ``review_calibrations`` /
``paper_drafts`` / ``draft_claims``（附录 A.6 + 附录 B.4/B.5）。

两条红线（contracts.blind_review_rules / evidence_rules.claim_rule）
------------------------------------------------------------------

1. **盲评隔离**：``generator_model_ref != reviewer_model_ref`` 必须成立，否则
   ``plan_review`` 环节直接失败，禁止静默回退同一模型；``candidate_alias`` 与
   ``shuffle_seed`` 必须可复现，且评审输入不得含 provider/model/时间/原始下标。
2. **Claim 三态**：草稿里的事实性句子逐句拆 Claim，
   ``support_status ∈ {supported, contradicted, insufficient}``；
   ``claim_coverage = supported / 事实性 Claim 总数``（分母为 0 时必须为 null，
   不得用 0 或 1 冒充）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.base import SciLoopModel
from app.schemas.enums import AgreementMetric, CalibrationStatus, ClaimStatus

__all__ = [
    "CalibrationCommitRequest",
    "Claim",
    "PaperDraft",
    "ReviewCalibration",
    "ReviewScore",
    "VerifyEvidenceRequest",
]


class ReviewScore(SciLoopModel):
    """``review_scores``：五维各 0-20，``total`` = 五维之和。"""

    id: int | None = None
    pipeline_run_id: int
    novelty: float | None = Field(default=None, ge=0, le=20)
    rigor: float | None = Field(default=None, ge=0, le=20)
    completeness: float | None = Field(default=None, ge=0, le=20)
    reproducibility: float | None = Field(default=None, ge=0, le=20)
    total: float | None = None
    comments: list[dict[str, Any]] = Field(
        default_factory=list, description="[{dimension, issue, suggestion}]"
    )
    created_at: datetime | None = None


class PaperDraft(SciLoopModel):
    """``paper_drafts``。"""

    id: int | None = None
    project_id: int
    pipeline_run_id: int | None = None
    iteration: int = Field(ge=1)
    content_md: str
    claim_coverage: float | None = Field(
        default=None, ge=0, le=1, description="分母为 0（无事实性 Claim）时必须为 null"
    )
    created_at: datetime | None = None


class Claim(SciLoopModel):
    """``draft_claims``（v1.2：证据链细化到 Claim 级）。"""

    id: int | None = None
    draft_id: int
    section_heading: str | None = None
    claim_text: str
    is_factual: bool = Field(default=True, description="false = 过渡/方法描述句，不参与覆盖率统计")
    support_status: ClaimStatus = Field(description="三态必给，不得留空")
    status_reason: str | None = Field(default=None, description="不通过时必须写清被拒代码")
    evidence_count: int = Field(default=0, ge=0)
    created_at: datetime | None = None


class ReviewCalibration(SciLoopModel):
    """``review_calibrations``：匿名盲评顺序 + 人工一致率。"""

    id: int | None = None
    pipeline_run_id: int
    generator_model_ref: str
    reviewer_model_ref: str = Field(description="必须 != generator_model_ref（隔离红线）")
    anonymization_version: str
    shuffle_seed: int = Field(description="固定 seed → 顺序可复现")
    candidate_order: list[Any] = Field(
        default_factory=list,
        description="[{candidate_alias, source_index}]，source_index 不对外展示",
    )
    model_scores: dict[str, Any] = Field(default_factory=dict)
    human_labels: list[Any] = Field(default_factory=list)
    sample_size: int = Field(default=0, ge=0, description="**必须展示**，小样本不得宣称显著")
    agreement_metric: AgreementMetric | None = None
    agreement_value: float | None = None
    confidence_interval: dict[str, Any] | None = None
    status: CalibrationStatus = CalibrationStatus.PENDING
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 请求体（附录 B.4/B.5）
# --------------------------------------------------------------------------- #
class CalibrationCommitRequest(SciLoopModel):
    """``POST /pipelines/{project_id}/human-labels``（Owner）：录入人工评审标签。"""

    labels: list[dict[str, Any]] = Field(
        default_factory=list,
        description="[{candidate_alias, score}]；少于 CALIBRATION_HUMAN_LABEL_MIN 条只展示不宣称显著",
    )


class VerifyEvidenceRequest(SciLoopModel):
    """``POST /drafts/{id}/verify-evidence``：可只校验给定文本（不落库）。"""

    content_md: str | None = Field(default=None, description="显式正文；缺省读库内草稿")
    persist: bool = Field(default=True, description="false = 只校验不落库（调试用）")
