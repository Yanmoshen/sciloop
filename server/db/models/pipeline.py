# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""流水线与实验域 ORM 模型（附录 A.5）。"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

BIGINT = BigInteger

PIPELINE_STAGES = ("survey", "plan", "plan_review", "experiment", "writing", "review")
TEMPLATE_IDS = (
    "T1_prompt_variant",
    "T2_fewshot_ablation",
    "T3_model_compare",
    "T4_llm_as_judge",
    "T5_rag_ablation",
)


class PipelineRun(Base):
    """一次流水线执行（含迭代轮次与成本）。"""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    total_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), server_default="0"
    )
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StageOutput(Base):
    """六环节产出。

    ``UNIQUE (pipeline_run_id, stage, attempt)`` 为并发幂等的硬约束，
    写入必须走 ``INSERT ... ON CONFLICT DO UPDATE``（见附录 A.0/A.5）。
    """

    __tablename__ = "stage_outputs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    output_json: Mapped[Any | None] = mapped_column(JSONB)
    output_text: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int | None] = mapped_column(Integer, server_default="1")
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), server_default="0")
    duration_ms: Mapped[int | None] = mapped_column(BIGINT)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "stage IN ('survey','plan','plan_review','experiment','writing','review')",
            name="stage",
        ),
        UniqueConstraint("pipeline_run_id", "stage", "attempt", name="uq_stage_once"),
    )


class Experiment(Base):
    """实验定义（模板 + 参数）。"""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    stage_output_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("stage_outputs.id", ondelete="CASCADE")
    )
    template_id: Mapped[str] = mapped_column(String(48), nullable=False)
    config: Mapped[Any] = mapped_column(JSONB, nullable=False)
    script_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "template_id IN ('T1_prompt_variant','T2_fewshot_ablation',"
            "'T3_model_compare','T4_llm_as_judge','T5_rag_ablation')",
            name="template",
        ),
    )


class ExperimentRun(Base):
    """单次实验执行记录（进程内受限执行器）。"""

    __tablename__ = "experiment_runs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int | None] = mapped_column(Integer, server_default="1")
    executor_task_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text)
    artifact_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BIGINT)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExperimentMetric(Base):
    """实验指标（禁止只存黑箱总分）。"""

    __tablename__ = "experiment_metrics"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    experiment_run_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("experiment_runs.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    metric_unit: Mapped[str | None] = mapped_column(String(24))
    extra: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
