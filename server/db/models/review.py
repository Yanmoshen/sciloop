# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""评审、决策与运维域 ORM 模型（附录 A.6）。"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BIGINT as _PG_BIGINT,
)
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

BIGINT = BigInteger

del _PG_BIGINT


class ReviewScore(Base):
    """review 环节四维评分。"""

    __tablename__ = "review_scores"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    novelty: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    rigor: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    completeness: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    reproducibility: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    comments: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DecisionLog(Base):
    """全自动决策留痕（核心审计凭证）。"""

    __tablename__ = "decision_logs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("pipeline_runs.id", ondelete="CASCADE")
    )
    decision_point: Mapped[str] = mapped_column(String(8), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(24))
    context_digest: Mapped[str] = mapped_column(Text, nullable=False)
    options_considered: Mapped[Any] = mapped_column(JSONB, nullable=False)
    chosen: Mapped[str] = mapped_column(String(64), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    risk_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    reversibility_score: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    policy_action: Mapped[str] = mapped_column(String(24), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    guardrail_checks: Mapped[Any] = mapped_column(JSONB, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), server_default="0")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "decision_point IN ('D1','D2','D3','D4','D5','D6')", name="dp"
        ),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="risk_score_range"),
        CheckConstraint(
            "confidence_score BETWEEN 0 AND 1", name="confidence_score_range"
        ),
        CheckConstraint(
            "reversibility_score BETWEEN 0 AND 1", name="reversibility_score_range"
        ),
        CheckConstraint(
            "policy_action IN ('auto_execute','need_human','circuit_break')",
            name="policy_action",
        ),
    )


class Intervention(Base):
    """人工介入记录（N1–N4）。"""

    __tablename__ = "interventions"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("pipeline_runs.id", ondelete="CASCADE")
    )
    node: Mapped[str] = mapped_column(String(8), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[Any | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LlmCallLog(Base):
    """LLM 调用日志（成本护栏的数据基础）。"""

    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="SET NULL")
    )
    stage: Mapped[str | None] = mapped_column(String(24))
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    duration_ms: Mapped[int | None] = mapped_column(BIGINT)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_replay: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_llm_logs_project", "project_id", "stage"),)


class ModelConfig(Base):
    """模型供应商配置（api_key 加密存储，接口不回明文）。"""

    __tablename__ = "model_configs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    models: Mapped[Any] = mapped_column(JSONB, nullable=False)
    is_default: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    last_tested_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    test_ok: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StageModelRouting(Base):
    """环节 → 模型路由（盲评隔离的落地基础）。"""

    __tablename__ = "stage_model_routing"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(64))
    model_config_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("model_configs.id"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(String(96), nullable=False)
    temperature: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaperDraft(Base):
    """论文草稿（claim_coverage = supported / 事实性 Claim 总数）。"""

    __tablename__ = "paper_drafts"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("pipeline_runs.id", ondelete="CASCADE")
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    content_md: Mapped[str] = mapped_column(Text, nullable=False)
    claim_coverage: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DraftClaim(Base):
    """Claim 级证据状态（三态）。"""

    __tablename__ = "draft_claims"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("paper_drafts.id", ondelete="CASCADE"), nullable=False
    )
    section_heading: Mapped[str | None] = mapped_column(Text)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_factual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    support_status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(Text)
    evidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "support_status IN ('supported','contradicted','insufficient')",
            name="support_status",
        ),
        Index("idx_claims_draft", "draft_id", "support_status"),
    )


class ExperimentPassport(Base):
    """实验可复现凭证：创建后不可 UPDATE，重跑生成新记录。"""

    __tablename__ = "experiment_passports"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    passport_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    experiment_run_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("experiment_runs.id", ondelete="RESTRICT"), nullable=False
    )
    parent_passport_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("experiment_passports.id", ondelete="SET NULL")
    )
    dataset_name: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    sample_manifest: Mapped[Any] = mapped_column(JSONB, nullable=False)
    provider: Mapped[str] = mapped_column(String(48), nullable=False)
    model_id: Mapped[str] = mapped_column(String(96), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(48), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    generation_params: Mapped[Any] = mapped_column(JSONB, nullable=False)
    template_id: Mapped[str] = mapped_column(String(48), nullable=False)
    template_config: Mapped[Any] = mapped_column(JSONB, nullable=False)
    code_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_lock_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    metrics: Mapped[Any] = mapped_column(JSONB, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    is_replay: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    artifact_manifest: Mapped[Any] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("passport_uid", name="uq_experiment_passports_passport_uid"),
        CheckConstraint(
            "status IN ('complete','incomplete','failed')", name="status"
        ),
    )


class ReviewCalibration(Base):
    """匿名盲评与人工校准结果。"""

    __tablename__ = "review_calibrations"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    generator_model_ref: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_model_ref: Mapped[str] = mapped_column(Text, nullable=False)
    anonymization_version: Mapped[str] = mapped_column(String(32), nullable=False)
    shuffle_seed: Mapped[int] = mapped_column(BIGINT, nullable=False)
    candidate_order: Mapped[Any] = mapped_column(JSONB, nullable=False)
    model_scores: Mapped[Any] = mapped_column(JSONB, nullable=False)
    human_labels: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    sample_size: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    agreement_metric: Mapped[str | None] = mapped_column(String(24))
    agreement_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    confidence_interval: Mapped[Any | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("sample_size >= 0", name="sample_size_non_negative"),
        CheckConstraint(
            "agreement_metric IN ('cohen_kappa','mae')", name="agreement_metric"
        ),
        CheckConstraint(
            "status IN ('pending','calibrated')", name="status"
        ),
    )


class DemoFixture(Base):
    """演示 fixture（LLM 回放 + 固定输入）。"""

    __tablename__ = "demo_fixtures"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    fixture_type: Mapped[str] = mapped_column(String(32), nullable=False)
    fixture_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "fixture_type", "fixture_key", name="uq_demo_fixtures_fixture_type_fixture_key"
        ),
    )
