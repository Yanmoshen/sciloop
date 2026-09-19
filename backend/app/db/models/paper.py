# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""论文域 ORM 模型（附录 A.2）。

建表顺序：papers → paper_identities → paper_documents → paper_source_records
→ paper_cards → paper_spans → paper_feed_snapshots

``paper_cards.llm_call_log_id`` 与 ``paper_spans.(paper_id, document_version)`` 的
外键按附录 A.0 规定在迁移末段以 ALTER TABLE 补齐，因此这里使用 ``use_alter=True``。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

BIGINT = BigInteger


class Paper(Base):
    """论文主表（多源合并后的唯一记录）。"""

    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    doi: Mapped[str | None] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    authors: Mapped[Any | None] = mapped_column(JSONB)
    published_at: Mapped[datetime.date | None] = mapped_column(Date)
    updated_at_src: Mapped[datetime.date | None] = mapped_column(Date)
    venue: Mapped[str | None] = mapped_column(String(128))
    venue_source: Mapped[str | None] = mapped_column(String(32))
    venue_level: Mapped[int | None] = mapped_column(SmallInteger)
    citation_count: Mapped[int | None] = mapped_column(Integer, server_default="0")
    citation_velocity: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    code_url: Mapped[str | None] = mapped_column(String(512))
    code_heat: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    institution_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    llm_novelty: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    llm_novelty_low: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    llm_novelty_high: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    llm_novelty_note: Mapped[str | None] = mapped_column(Text)
    llm_novelty_stable: Mapped[bool | None] = mapped_column(Boolean)
    rank_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    rank_breakdown: Mapped[Any | None] = mapped_column(JSONB)
    influence_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    score_breakdown: Mapped[Any | None] = mapped_column(JSONB)
    score_coverage: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    pdf_url: Mapped[str | None] = mapped_column(Text)
    is_parsed: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    raw: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_papers_source_external_id"),
        # 排序方向（DESC NULLS LAST）由迁移脚本中的原始 SQL 精确表达
        Index("idx_papers_influence", text("influence_score DESC NULLS LAST")),
        Index("idx_papers_rank", text("rank_score DESC NULLS LAST")),
        Index("idx_papers_published", text("published_at DESC")),
        Index("idx_papers_venue_lvl", text("venue_level DESC NULLS LAST")),
        Index("idx_papers_doi", "doi"),
    )

    identities: Mapped[list[PaperIdentity]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )


class PaperIdentity(Base):
    """统一身份映射（防重复入库）。"""

    __tablename__ = "paper_identities"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    id_type: Mapped[str] = mapped_column(String(24), nullable=False)
    id_value: Mapped[str] = mapped_column(String(256), nullable=False)
    is_primary: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("id_type", "id_value", name="uq_paper_identities_id_type_id_value"),
        Index("idx_pident_paper", "paper_id"),
    )

    paper: Mapped[Paper] = relationship(back_populates="identities")


class PaperDocument(Base):
    """全文解析记录（证据定位的地基）。"""

    __tablename__ = "paper_documents"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    parser: Mapped[str] = mapped_column(String(48), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    text_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    char_count: Mapped[int | None] = mapped_column(Integer)
    locatable_chars: Mapped[int | None] = mapped_column(Integer)
    coverage: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text)
    parsed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "document_version",
            name="uq_paper_documents_paper_id_document_version",
        ),
        CheckConstraint(
            "parse_status IN ('ok','partial','unavailable','failed')",
            name="parse_status",
        ),
        Index("idx_pdoc_paper", "paper_id", "parse_status"),
    )


class PaperSourceRecord(Base):
    """外部数据源取数留痕。"""

    __tablename__ = "paper_source_records"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    request_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_src_records_paper", "paper_id", "field_name"),)


class PaperCard(Base):
    """单篇解析卡片（8 字段），支持 version 递增的 force 重解析。"""

    __tablename__ = "paper_cards"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    research_problem: Mapped[str] = mapped_column(Text, nullable=False)
    core_method: Mapped[str] = mapped_column(Text, nullable=False)
    key_innovation: Mapped[Any] = mapped_column(JSONB, nullable=False)
    technical_route: Mapped[Any] = mapped_column(JSONB, nullable=False)
    experimental_setup: Mapped[Any] = mapped_column(JSONB, nullable=False)
    main_conclusions: Mapped[Any] = mapped_column(JSONB, nullable=False)
    limitations: Mapped[Any] = mapped_column(JSONB, nullable=False)
    transferable: Mapped[Any] = mapped_column(JSONB, nullable=False)
    llm_call_log_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey(
            "llm_call_logs.id",
            name="fk_paper_cards_llm_call",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("paper_id", "version", name="uq_paper_cards_paper_id_version"),
    )


class PaperSpan(Base):
    """原文定位片段（evidence 与前端高亮）。"""

    __tablename__ = "paper_spans"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)
    section_name: Mapped[str | None] = mapped_column(String(64))
    page_number: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[Any | None] = mapped_column(JSONB)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    quote_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # fk_spans_document：(paper_id, document_version) -> paper_documents
        # 按附录 A.0 在迁移末段以 ALTER TABLE 补齐
        ForeignKeyConstraint(
            ["paper_id", "document_version"],
            ["paper_documents.paper_id", "paper_documents.document_version"],
            name="fk_spans_document",
            ondelete="CASCADE",
            use_alter=True,
        ),
    )


class PaperFeedSnapshot(Base):
    """论文库快照（三视图可复现 + 演示兜底）。"""

    __tablename__ = "paper_feed_snapshots"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    view_type: Mapped[str] = mapped_column(String(16), nullable=False)
    filters: Mapped[Any | None] = mapped_column(JSONB)
    paper_ids: Mapped[Any] = mapped_column(JSONB, nullable=False)
    is_demo: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
