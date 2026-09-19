# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文解析与定位服务（WP05）。

对外契约（供 WP06 / WP07 / WP13 使用）::

    from app.services.fulltext import DocumentStore, verify_span, list_spans

    store = DocumentStore(repository=SqlDocumentRepository(session))
    result = await store.ensure_fulltext(paper_id)      # 解析并落库
    result = await store.ensure_document(paper_id)      # 只取 paper_documents 记录
    spans = await store.list_spans(paper_id, section="method")
    check = verify_span(span)                           # 哈希优先校验

``verify_span`` 返回 ``{verdict, hash_match, offset_match}``，
``verdict ∈ {valid, valid_by_hash, invalid}``。
"""

from __future__ import annotations

from app.services.fulltext.coverage import (
    classify_parse_status,
    compute_coverage,
    coverage_note,
    evidence_scope,
    spans_allowed,
)
from app.services.fulltext.document_store import (
    DocumentStore,
    Fetcher,
    FetchResult,
    HttpxFetcher,
    LocalFileFetcher,
    PaperNotFoundError,
    SourceCandidate,
    document_sources,
    pick_primary_document,
    summarize_documents,
)
from app.services.fulltext.html_parser import parse_html
from app.services.fulltext.locator import (
    build_span,
    build_spans,
    relocate_quote,
    verify_span,
    verify_spans,
)
from app.services.fulltext.pdf_parser import NoTextLayerError, parse_pdf
from app.services.fulltext.records import (
    FulltextResult,
    PaperDocumentRecord,
    PaperRef,
    PaperSpanRecord,
    ParseAttempt,
    ParsedBlock,
    ParsedDocument,
    build_document_version,
    quote_sha256,
    sha256_hex,
)
from app.services.fulltext.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
    SqlDocumentRepository,
)
from app.services.fulltext.section_map import (
    SECTION_NAMES,
    looks_like_heading,
    normalize_section_name,
)
from app.services.fulltext.text_cache import TextCache, default_text_cache

__all__ = [
    # 编排
    "DocumentStore",
    "Fetcher",
    "FetchResult",
    "HttpxFetcher",
    "LocalFileFetcher",
    "PaperNotFoundError",
    "SourceCandidate",
    "document_sources",
    "pick_primary_document",
    "summarize_documents",
    # 解析器
    "parse_html",
    "parse_pdf",
    "NoTextLayerError",
    # 定位与校验
    "verify_span",
    "verify_spans",
    "build_span",
    "build_spans",
    "relocate_quote",
    "quote_sha256",
    "sha256_hex",
    "build_document_version",
    # 覆盖度
    "compute_coverage",
    "classify_parse_status",
    "spans_allowed",
    "evidence_scope",
    "coverage_note",
    # 章节
    "SECTION_NAMES",
    "normalize_section_name",
    "looks_like_heading",
    # 数据结构
    "FulltextResult",
    "PaperDocumentRecord",
    "PaperRef",
    "PaperSpanRecord",
    "ParseAttempt",
    "ParsedBlock",
    "ParsedDocument",
    # 持久化
    "DocumentRepository",
    "InMemoryDocumentRepository",
    "SqlDocumentRepository",
    # 文本缓存
    "TextCache",
    "default_text_cache",
]
