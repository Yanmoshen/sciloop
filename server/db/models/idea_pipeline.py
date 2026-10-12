# Copyright 2026 SciLoop contributors
"""Auditable multi-expert Idea generation pipeline models."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from db.types import JSONB

BIGINT = BigInteger().with_variant(__import__("sqlalchemy").Integer(), "sqlite")


class IdeaPipelineRun(Base):
    """One immutable input snapshot and lifecycle for a 20-expert run."""

    __tablename__ = "idea_pipeline_runs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("projects.id", ondelete="SET NULL"))
    aggregation_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("aggregations.id", ondelete="SET NULL"))
    paper_ids: Mapped[Any] = mapped_column(JSONB, nullable=False)
    input_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    model_ref: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="running")
    current_expert: Mapped[int | None] = mapped_column(Integer)
    confirmation_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    decision: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class IdeaPipelineExpertRun(Base):
    """Exactly one audit record for each logical expert invocation."""

    __tablename__ = "idea_pipeline_experts"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BIGINT, ForeignKey("idea_pipeline_runs.id", ondelete="CASCADE"), nullable=False)
    expert_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expert_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="running")
    input_snapshot: Mapped[Any] = mapped_column(JSONB, nullable=False)
    output: Mapped[Any | None] = mapped_column(JSONB)
    raw_output: Mapped[str | None] = mapped_column(Text)
    model_ref: Mapped[str | None] = mapped_column(String(256))
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="v1")
    cost_usd: Mapped[float | None] = mapped_column()
    attempts: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))


class IdeaPipelineCandidate(Base):
    """The eight direction-balanced candidates and their final decision data."""

    __tablename__ = "idea_pipeline_candidates"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BIGINT, ForeignKey("idea_pipeline_runs.id", ondelete="CASCADE"), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[Any] = mapped_column(JSONB, nullable=False, default=list)
    score: Mapped[Any | None] = mapped_column(JSONB)
    rank: Mapped[int | None] = mapped_column(Integer)
    recommendation: Mapped[str | None] = mapped_column(String(32))
    is_selected: Mapped[bool] = mapped_column(nullable=False, server_default="0")
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
