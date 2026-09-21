# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""聚合与 idea 域 ORM 模型（附录 A.3）。"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

BIGINT = BigInteger


class Aggregation(Base):
    """论文对比矩阵 + 方法演进（聚合产物）。"""

    __tablename__ = "aggregations"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="SET NULL")
    )
    paper_ids: Mapped[Any] = mapped_column(JSONB, nullable=False)
    comparison_matrix: Mapped[Any] = mapped_column(JSONB, nullable=False)
    method_evolution: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Gap(Base):
    """研究空白（必须带提出者与被引用证据）。"""

    __tablename__ = "gaps"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    aggregation_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("aggregations.id", ondelete="CASCADE"), nullable=False
    )
    gap_text: Mapped[str] = mapped_column(Text, nullable=False)
    raised_by_paper_ids: Mapped[Any] = mapped_column(JSONB, nullable=False)
    unsolved_evidence: Mapped[Any] = mapped_column(JSONB, nullable=False)
    novelty_hint: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Idea(Base):
    """研究 idea（无 Evidence 的不允许输出）。"""

    __tablename__ = "ideas"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE")
    )
    aggregation_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("aggregations.id", ondelete="SET NULL")
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mechanism: Mapped[str | None] = mapped_column(String(32))
    novelty_note: Mapped[str | None] = mapped_column(Text)
    is_selected: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Evidence(Base):
    """多态证据表。

    ``experiment_run_id`` / ``decision_log_id`` 在 A.0 顺序中先于本表建成，故内联；
    ``experiment_passport_id`` 指向第 28 张表，按 A.0 在迁移末段 ALTER 补齐。
    """

    __tablename__ = "evidences"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(32), nullable=False)
    paper_id: Mapped[int | None] = mapped_column(BIGINT, ForeignKey("papers.id"))
    paper_span_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("paper_spans.id")
    )
    card_field: Mapped[str | None] = mapped_column(String(64))
    experiment_run_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("experiment_runs.id")
    )
    experiment_passport_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey(
            "experiment_passports.id",
            name="fk_evidences_passport",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    metric_name: Mapped[str | None] = mapped_column(String(64))
    metric_value: Mapped[Decimal | None] = mapped_column(Numeric)
    decision_log_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("decision_logs.id")
    )
    quote_text: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), server_default="1.0")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_evidences_owner", "owner_type", "owner_id"),)
