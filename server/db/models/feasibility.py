# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""可行性与任务书域 ORM 模型（附录 A.4）。"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

BIGINT = BigInteger


class Feasibility(Base):
    """可行性报告（v1.0 名为 feasibility_reports）。"""

    __tablename__ = "feasibilities"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    idea_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("ideas.id", ondelete="CASCADE"), nullable=False
    )
    data_availability: Mapped[Any] = mapped_column(JSONB, nullable=False)
    compute_cost: Mapped[Any] = mapped_column(JSONB, nullable=False)
    method_maturity: Mapped[Any] = mapped_column(JSONB, nullable=False)
    novelty_gap: Mapped[Any] = mapped_column(JSONB, nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    risk_list: Mapped[Any] = mapped_column(JSONB, nullable=False)
    mve_plan: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Taskbook(Base):
    """任务书（锁定后作为流水线执行依据）。"""

    __tablename__ = "taskbooks"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    idea_id: Mapped[int] = mapped_column(BIGINT, ForeignKey("ideas.id"), nullable=False)
    research_question: Mapped[str] = mapped_column(Text, nullable=False)
    target_datasets: Mapped[Any] = mapped_column(JSONB, nullable=False)
    baselines: Mapped[Any] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[Any] = mapped_column(JSONB, nullable=False)
    compute_budget: Mapped[Any] = mapped_column(JSONB, nullable=False)
    deliverables: Mapped[Any] = mapped_column(JSONB, nullable=False)
    max_iterations: Mapped[int | None] = mapped_column(Integer, server_default="3")
    score_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), server_default="80"
    )
    marginal_gain_threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), server_default="2"
    )
    max_retry: Mapped[int | None] = mapped_column(Integer, server_default="2")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft"
    )
    locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
