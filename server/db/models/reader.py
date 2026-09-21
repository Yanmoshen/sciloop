# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文阅读器域 ORM 模型（EasyPaper 四核心模块之 ③ 在 SciLoop 的落地）。

四张表与「原文锚点 + 多版本 + 批注同步」一一对应：

==========================  ====================================================
``reader_documents``        阅读文档：PyMuPDF 解析结果（pages/sections/blocks）
``reader_versions``         **不可变**版本：original / chinese / simple / bilingual
``reader_states``           阅读状态：当前 block / 偏移 / 模式 / 字号 / 已理解 / 术语
``reader_annotations``      批注：原文锚点 + 跨版本投影 + ``revision`` 乐观锁
==========================  ====================================================

不可变性（硬约束，与 ``experiment_passports`` 同一做法）
--------------------------------------------------------
``reader_versions`` 创建后**不得原地 UPDATE**：ORM 层由
``app/services/reader/versions.py`` 的 ``before_update`` 事件拦截
（``code=reader_version_immutable``），数据库层由迁移
``20260918_0003_reader_library.py`` 的 ``BEFORE UPDATE`` 触发器兜底，
报错信息同样含 ``reader_version_immutable`` 字面量，使两层拒绝口径可被同一断言识别。

为什么 ``reader_documents`` 允许 UPDATE
---------------------------------------
它是**解析缓存**而非凭证：同一份原文重复解析必须得到**相同 block id**（稳定 id），
``force=true`` 重解析只是把同一 fingerprint 的结果重算一遍并留下新警告，
不构成「改写历史」；真正需要不可变的是版本 PDF 与其哈希。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

BIGINT = BigInteger

#: 版本种类：``original`` 由创建阅读文档时自动生成，**不允许**通过登记接口提交
VERSION_KINDS: tuple[str, ...] = ("original", "chinese", "simple", "bilingual")
#: 只允许经 ``POST /reader/documents/{id}/versions`` 登记的三种（翻译产物）
REGISTRABLE_KINDS: tuple[str, ...] = ("chinese", "simple", "bilingual")
#: 解析状态（与 EasyPaper §4.3 的 warnings 语义一致：无文本层不得伪装成 ok）
PARSE_STATUSES: tuple[str, ...] = ("ok", "partial", "unavailable")
#: 批注对齐状态；``partial`` / ``pending`` 必须保留，禁止把不确定匹配伪装成 success
ALIGN_STATUSES: tuple[str, ...] = ("success", "partial", "pending")


def _kinds_check(column: str, name: str, values: tuple[str, ...]) -> CheckConstraint:
    rendered = ",".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({rendered})", name=name)


class ReaderDocument(Base):
    """阅读文档：一篇论文原文（PDF）的解析结果，block 携带**稳定 id**。"""

    __tablename__ = "reader_documents"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    paper_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    #: 标题来源：pdf_meta（PDF 元数据）/ heading（首个标题块）/ paper（库内论文题名）
    title_source: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="paper"
    )
    schema_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 原文 PDF 的 SHA-256（审计锚点：版本哈希、批注指纹、两次解析可比对）
    fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    section_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: ``{schema_version, fingerprint, title, page_count, pages[], sections[], blocks[], warnings[]}``
    document: Mapped[Any] = mapped_column(JSONB, nullable=False)
    #: 解析结果规范化 JSON 的 SHA-256（内容指纹，用于防旧客户端覆盖新修改）
    payload_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    warnings: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "fingerprint",
            name="uq_reader_documents_paper_id_fingerprint",
        ),
        _kinds_check("parse_status", "parse_status", PARSE_STATUSES),
        _kinds_check("title_source", "title_source", ("pdf_meta", "heading", "paper")),
        Index("idx_reader_documents_paper", "paper_id"),
    )


class ReaderVersion(Base):
    """不可变阅读版本（原文 / 中文 / 简单英语 / 双语）。

    保存 PDF 文件路径、文本索引、页面对应关系、内容指纹与**原样**保存的翻译
    ``manifest`` 审计字段（``sha256`` / ``layout_warnings`` / ``highlight_summary``）。
    创建后任何原地修改都会被 ORM 事件与数据库触发器双重拒绝。
    """

    __tablename__ = "reader_versions"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("reader_documents.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 翻译任务 id（``original`` 为 NULL；登记版本时必填，用于回溯 manifest）
    task_id: Mapped[str | None] = mapped_column(String(64))
    engine: Mapped[str | None] = mapped_column(String(48))
    #: reader-library 内的相对文件名（已安全化，禁止路径穿越）
    file_name: Mapped[str] = mapped_column(String(160), nullable=False)
    file_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False)
    index_file_name: Mapped[str] = mapped_column(String(160), nullable=False)
    index_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    #: ``manifest.sha256.source`` 原样保存（审计用，不做二次解释）
    source_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    #: ``manifest.sha256.mono`` / ``manifest.sha256.dual`` 原样保存
    pdf_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    hash_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: 页面对应关系（``identity`` / ``unavailable`` + 逐页映射）
    page_map: Mapped[Any] = mapped_column(JSONB, nullable=False)
    block_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    layout_warnings: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: manifest 原文快照（只增不改；便于事后审计，不参与业务计算）
    manifest_snapshot: Mapped[Any | None] = mapped_column(JSONB)
    #: 登记时所依据的阅读文档内容指纹（``reader_documents.payload_sha256``）
    content_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_reader_versions_document_id_version_no"),
        # 同一 kind 允许保留历史（迁移 0004）：唯一性下沉到 (document_id, kind, task_id)。
        # 用 coalesce 把 original 版本的 NULL task_id 归一成 ''，否则 PostgreSQL 不约束 NULL，
        # original 的「每文档唯一」就只剩代码在守。
        Index(
            "uq_reader_versions_document_kind_task",
            "document_id",
            "kind",
            text("coalesce(task_id, '')"),
            unique=True,
        ),
        _kinds_check("kind", "kind", VERSION_KINDS),
        Index("idx_reader_versions_document", "document_id"),
    )


class ReaderState(Base):
    """阅读状态：每个阅读文档一行（读取时惰性创建默认行）。"""

    __tablename__ = "reader_states"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BIGINT,
        ForeignKey("reader_documents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    current_block: Mapped[str | None] = mapped_column(String(96))
    #: 列名用 ``block_offset``：``offset`` 是 PostgreSQL 保留字（列定义处直接语法错误）。
    #: ORM 属性与对外 JSON 字段名仍为 ``offset``，契约不变。
    offset: Mapped[int] = mapped_column(
        "block_offset", Integer, nullable=False, server_default="0"
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="original")
    font_size: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="16")
    understood_blocks: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    favorite_terms: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    #: 状态修订号（每次 PATCH 递增，便于前端判断本地副本是否过期）
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        _kinds_check("mode", "mode", VERSION_KINDS),
        CheckConstraint("block_offset >= 0", name="offset_non_negative"),
        CheckConstraint("font_size BETWEEN 8 AND 48", name="font_size_range"),
    )


class ReaderAnnotation(Base):
    """批注：原文锚点 + 跨版本投影 + ``revision`` 乐观锁。

    ``anchor_text`` / ``anchor_sha256`` 是**原文语言**的锚点：无论批注建在哪个
    版本上，跨版本对齐都以它为唯一可比对键（翻译后文字长度会变，页码不可靠
    ——见 EasyPaper §4.8）。``projections`` 逐版本记录匹配方式与几何，未匹配到
    的版本不出现在其中，此时 ``align_status`` 必须是 ``partial`` 或 ``pending``。
    """

    __tablename__ = "reader_annotations"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("reader_documents.id", ondelete="CASCADE"), nullable=False
    )
    #: 稳定标识（32 位十六进制）：归档/恢复与幂等导入以它去重，不依赖自增 id
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[int | None] = mapped_column(
        BIGINT, ForeignKey("reader_versions.id", ondelete="SET NULL")
    )
    block_id: Mapped[str | None] = mapped_column(String(96))
    page: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[Any | None] = mapped_column(JSONB)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    quote_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    anchor_text: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    #: 锚点是否在源版本中真实命中某 block（未命中时如实置 false，供前端提示）
    anchor_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="highlight")
    note: Mapped[str | None] = mapped_column(Text)
    align_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    projections: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    #: 内容指纹（登记时阅读文档的 ``payload_sha256``），用于识别旧客户端
    content_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("document_id", "uid", name="uq_reader_annotations_document_id_uid"),
        _kinds_check("kind", "kind", ("highlight", "note", "question", "summary")),
        _kinds_check("align_status", "align_status", ALIGN_STATUSES),
        CheckConstraint("revision >= 1", name="revision_positive"),
        Index("idx_reader_annotations_document", "document_id", "align_status"),
    )


__all__ = [
    "ALIGN_STATUSES",
    "PARSE_STATUSES",
    "REGISTRABLE_KINDS",
    "VERSION_KINDS",
    "ReaderAnnotation",
    "ReaderDocument",
    "ReaderState",
    "ReaderVersion",
]
