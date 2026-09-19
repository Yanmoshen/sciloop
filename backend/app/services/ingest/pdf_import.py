# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""PDF 导入：解析（复用 WP05）+ 身份映射（复用 WP03）+ 文档落库。

链路（每一步都可审计）
----------------------

1. **解析**：``app.services.fulltext.pdf_parser.parse_pdf``（pymupdf），
   产出带页码 / bbox / 章节的块序列；
2. **身份映射**：从首页文本正则抽 DOI / arXiv ID，用启发式抽标题，
   交给 ``app/services/paper_source/identity.py`` 的 ``upsert_paper``
   做「命中复用 / 未命中新建」（新建时 ``source='upload'``）——
   因此**同一篇论文重复导入不会产生第二条 ``papers``**（同 DOI / 同 arXiv ID /
   同标题哈希任一命中即复用）；
3. **落库**：写入 ``paper_documents``，``source_type='upload'``、
   ``source_url='upload://<文件名>'``、``parser='pymupdf'``，
   ``parse_status`` / ``coverage`` / ``text_sha256`` 均为**真实计算值**；
   ``parse_status='ok'`` 且 ``coverage>=0.60`` 时才通过 ``build_spans`` 写正文片段
   （contracts.evidence_rules.fulltext_gate）；
4. **归一全文**落 WP05 的文本缓存，使 ``verify_span`` 的偏移校验可用。

失败处理（禁止编造）
--------------------

=========================  =========================================================
有效 PDF，有文本层        正常解析；``parse_status ∈ {ok, partial}``
有效 PDF，**无文本层**    新建/复用论文 + 写 ``parse_status='unavailable'`` +
                          ``parse_error``（不伪装成已解析）
不是合法 PDF（打不开）    **不建论文**，任务项 ``status='failed'`` + 原因
同一文件内容已导入过      ``status='duplicate'``（按 ``paper_documents.text_sha256``
                          查重，只读短路，不写任何行）
=========================  =========================================================
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.paper import PaperDocument
from app.services.fulltext import (
    NoTextLayerError,
    PaperDocumentRecord,
    SqlDocumentRepository,
    build_document_version,
    build_spans,
    classify_parse_status,
    compute_coverage,
    default_text_cache,
    parse_pdf,
    sha256_hex,
    spans_allowed,
)
from app.services.ingest import jobs, storage
from app.services.ingest.jobs import ITEM_CREATED, ITEM_DUPLICATE, ITEM_FAILED, ITEM_REUSED
from app.services.paper_source import identity as paper_identity

logger = logging.getLogger("sciloop.ingest.pdf_import")

SOURCE_NAME = "upload"
PDF_PARSER = "pymupdf"
#: ``paper_documents.source_type``（契约：导入的 PDF 统一记 'upload'）
DOCUMENT_SOURCE_TYPE = "upload"

# ------------------------------------------------------------------ 元数据抽取
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>{}|\\^\[\]]+", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"\barxiv[:\s/]*(\d{4}\.\d{4,5})(v\d+)?", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(
    r"\barxiv[:\s/]*([a-z][a-z-]*(?:\.[A-Za-z]{2})?/\d{7})(v\d+)?", re.IGNORECASE
)
_ARXIV_URL_RE = re.compile(
    r"\barxiv\.org/(?:abs|pdf)/([a-z-]+(?:\.[A-Za-z]{2})?/\d{7}|\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)
_DOI_TRAILING = ".,;:)]}\"'"
_TITLE_MIN_CHARS = 20
_TITLE_MAX_CHARS = 350
_NOISE_HINTS = (
    "@",
    "http://",
    "https://",
    "doi.org",
    "arxiv.org",
    "abstract",
    "keywords",
    "university",
    "institute",
    "proceedings of",
    "preprint",
)


def normalize_doi(raw: str | None) -> str | None:
    """归一 DOI：去 ``doi:`` 前缀与结尾标点，转小写。"""
    text = str(raw or "").strip()
    if not text:
        return None
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text, flags=re.IGNORECASE)
    return text.rstrip(_DOI_TRAILING).lower() or None


def extract_doi(text: str) -> str | None:
    match = _DOI_RE.search(text or "")
    return normalize_doi(match.group(0)) if match else None


def extract_arxiv_id(text: str) -> str | None:
    """抽 arXiv ID（新式 ``2409.12345`` 与旧式 ``cs.AI/0701001`` 都支持）。"""
    payload = text or ""
    for pattern in (_ARXIV_NEW_RE, _ARXIV_URL_RE, _ARXIV_OLD_RE):
        match = pattern.search(payload)
        if match:
            value = f"{match.group(1)}{match.group(2) or ''}"
            return paper_identity.normalize_arxiv_id(value)
    return None


def _looks_like_title(text: str) -> bool:
    candidate = (text or "").strip()
    if not (_TITLE_MIN_CHARS <= len(candidate) <= _TITLE_MAX_CHARS):
        return False
    lowered = candidate.lower()
    if any(hint in lowered for hint in _NOISE_HINTS):
        return False
    words = candidate.split()
    if len(words) < 3:
        return False
    letters = sum(1 for char in candidate if char.isalpha())
    if letters < len(candidate) * 0.5:
        return False
    # 以句号结尾且很长时更像摘要正文
    return not (len(candidate) > 160 and candidate.endswith("."))


def guess_title(parsed_blocks: Sequence[Any]) -> str | None:
    """首页启发式抽标题：取阅读顺序中第一个像标题的块。"""
    for block in parsed_blocks:
        if int(getattr(block, "page_number", 0) or 0) != 1:
            continue
        if getattr(block, "kind", "") == "heading" and _looks_like_title(block.text):
            return " ".join(block.text.split())
        if _looks_like_title(getattr(block, "text", "")):
            return " ".join(block.text.split())
    return None


def extract_metadata(parsed: Any, *, fallback_title: str) -> dict[str, Any]:
    """从解析结果抽身份信息；抽不到就如实留空并标注来源。"""
    head_blocks = [block for block in parsed.blocks if int(block.page_number or 0) <= 2]
    head_text = "\n".join(block.text for block in head_blocks) or (parsed.full_text or "")[:8000]
    title = guess_title(parsed.blocks)
    title_source = "pdf_first_page"
    if not title:
        title = fallback_title or "upload"
        title_source = "filename"
    return {
        "title": title,
        "title_source": title_source,
        "doi": extract_doi(head_text),
        "arxiv_id": extract_arxiv_id(head_text),
        "page_count": parsed.page_count,
    }


# ------------------------------------------------------------------ 查重
def find_document_by_content_sha(
    session: Session, text_sha256: str
) -> dict[str, Any] | None:
    """按 ``paper_documents.text_sha256``（= PDF 文件内容 SHA-256）查重。

    命中说明**同一份文件内容**此前已成功落过 ``paper_documents``，
    本次无需再建论文，直接复用（``status='duplicate'``）。
    """
    if not text_sha256:
        return None
    row = session.execute(
        select(
            PaperDocument.paper_id,
            PaperDocument.document_version,
            PaperDocument.source_url,
            PaperDocument.parse_status,
            PaperDocument.coverage,
        )
        .where(PaperDocument.text_sha256 == str(text_sha256))
        .order_by(PaperDocument.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    return {
        "paper_id": int(row[0]),
        "document_version": row[1],
        "source_url": row[2],
        "parse_status": row[3],
        "coverage": float(row[4]) if row[4] is not None else None,
    }


# ------------------------------------------------------------------ 单文件导入
async def _persist_document(
    session: Session,
    *,
    paper_id: int,
    parsed: Any,
    source_url: str,
    content_sha256: str,
    min_coverage: float,
) -> PaperDocumentRecord:
    """写 ``paper_documents``（source_type='upload'）并按门禁写 ``paper_spans``。"""
    char_count = parsed.char_count or len(parsed.full_text or "")
    locatable = parsed.locatable_chars
    coverage = compute_coverage(locatable, char_count)
    status = classify_parse_status(
        char_count=char_count,
        locatable_chars=locatable,
        coverage=coverage,
        min_coverage=min_coverage,
    )
    document_version = build_document_version(source_url, content_sha256)
    record = PaperDocumentRecord(
        paper_id=int(paper_id),
        document_version=document_version,
        source_type=DOCUMENT_SOURCE_TYPE,
        source_url=source_url,
        parser=parsed.parser or PDF_PARSER,
        parser_version=parsed.parser_version,
        text_sha256=content_sha256,
        parse_status=status,  # type: ignore[arg-type]
        page_count=parsed.page_count,
        char_count=char_count,
        locatable_chars=locatable,
        coverage=coverage,
        parse_error=None,
    )
    repository = SqlDocumentRepository(session)
    stored = await repository.upsert_document(record)

    allowed = spans_allowed(status, coverage, min_coverage=min_coverage)
    spans = (
        build_spans(
            paper_id=int(paper_id),
            document_version=document_version,
            blocks=parsed.blocks,
            min_paragraph_chars=40,
            max_spans=0,
        )
        if allowed
        else []
    )
    # 不允许正文 span 时必须清空历史片段，避免残留过期证据
    await repository.replace_spans(int(paper_id), document_version, spans)
    if parsed.full_text:
        default_text_cache().put(int(paper_id), document_version, parsed.full_text)
    logger.info(
        "upload_document paper_id=%s version=%s status=%s coverage=%.3f spans=%d",
        paper_id,
        document_version,
        status,
        coverage,
        len(spans),
    )
    return stored


async def _persist_unavailable_document(
    session: Session,
    *,
    paper_id: int,
    source_url: str,
    content_sha256: str,
    parse_error: str,
    page_count: int | None,
) -> PaperDocumentRecord:
    """无文本层：如实写 ``parse_status='unavailable'`` + ``parse_error``。"""
    record = PaperDocumentRecord(
        paper_id=int(paper_id),
        document_version=build_document_version(source_url, content_sha256),
        source_type=DOCUMENT_SOURCE_TYPE,
        source_url=source_url,
        parser=PDF_PARSER,
        parser_version="1.0.0",
        text_sha256=content_sha256,
        parse_status="unavailable",
        page_count=page_count,
        char_count=0,
        locatable_chars=0,
        coverage=0.0,
        parse_error=parse_error,
    )
    repository = SqlDocumentRepository(session)
    stored = await repository.upsert_document(record)
    await repository.replace_spans(int(paper_id), stored.document_version, [])
    return stored


async def import_pdf_bytes(
    session: Session,
    *,
    stored_name: str,
    content: bytes,
    project_id: int | None = None,
    stored_path: str | None = None,
) -> dict[str, Any]:
    """导入一份已落盘的 PDF，返回契约中的单条结果项。"""
    from app.core.config import get_settings

    content_sha256 = sha256_hex(content)
    source_url = storage.upload_source_url(stored_name)
    fallback_title = re.sub(r"\.pdf$", "", stored_name, flags=re.IGNORECASE) or "upload"
    base_item: dict[str, Any] = {
        "input": stored_name,
        "status": ITEM_FAILED,
        "paper_id": None,
        "reason": None,
        "source": SOURCE_NAME,
    }

    # 1) 内容级查重（只读短路：同一份文件已导入过就不再建论文）
    duplicate = find_document_by_content_sha(session, content_sha256)
    if duplicate is not None:
        logger.info(
            "upload_duplicate name=%s paper_id=%s version=%s",
            stored_name,
            duplicate["paper_id"],
            duplicate["document_version"],
        )
        return {
            **base_item,
            "status": ITEM_DUPLICATE,
            "paper_id": duplicate["paper_id"],
            "reason": "identical_content_already_imported",
            "document_version": duplicate["document_version"],
            "parse_status": duplicate["parse_status"],
        }

    # 2) 解析（复用 WP05）
    parsed = None
    no_text_error: str | None = None
    page_count: int | None = None
    try:
        parsed = parse_pdf(content, source_url=source_url, content_sha256=content_sha256)
    except NoTextLayerError as exc:
        no_text_error = str(exc)
        page_count = getattr(exc, "page_count", None)
    except ValueError as exc:
        logger.warning("upload_invalid_pdf name=%s err=%s", stored_name, exc)
        return {**base_item, "reason": f"invalid_pdf: {exc}"}
    except Exception as exc:  # noqa: BLE001 - 解析器异常如实上报，绝不静默成功
        logger.exception("upload_parse_failed name=%s", stored_name)
        return {**base_item, "reason": f"parse_exception: {type(exc).__name__}: {exc}"}

    min_coverage = float(get_settings().fulltext_min_coverage or 0.60)
    metadata = (
        extract_metadata(parsed, fallback_title=fallback_title)
        if parsed is not None
        else {
            "title": fallback_title,
            "title_source": "filename",
            "doi": None,
            "arxiv_id": None,
            "page_count": page_count,
        }
    )

    # 3) 身份映射（复用 WP03）：命中复用 / 未命中新建，source='upload'
    meta = {
        "source": SOURCE_NAME,
        "title": metadata["title"],
        "doi": metadata["doi"],
        "arxiv_id": metadata["arxiv_id"],
        "raw": {
            "filename": stored_name,
            "source_url": source_url,
            "stored_path": stored_path,
            "file_sha256": content_sha256,
            "size_bytes": len(content),
            "title_source": metadata["title_source"],
            "extracted_doi": metadata["doi"],
            "extracted_arxiv_id": metadata["arxiv_id"],
            "page_count": metadata["page_count"],
            "project_id": project_id,
            "imported_at": datetime.now(UTC).isoformat(),
        },
    }
    try:
        upsert = paper_identity.upsert_paper(session, meta)
        paper_id = int(upsert.paper_id)
        # 记录本次导入结果（历史端点 GET /papers/imports 由此聚合）
        paper_identity.merge_source_payload(
            session,
            paper_id,
            SOURCE_NAME,
            {**meta["raw"], "status": ITEM_CREATED if upsert.created else ITEM_REUSED},
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 入库失败必须如实返回
        session.rollback()
        logger.exception("upload_upsert_failed name=%s", stored_name)
        return {**base_item, "reason": f"upsert_failed: {type(exc).__name__}: {exc}"}

    # 4) 文档落库（真实 parse_status / coverage / text_sha256）
    try:
        if parsed is None:
            document = await _persist_unavailable_document(
                session,
                paper_id=paper_id,
                source_url=source_url,
                content_sha256=content_sha256,
                parse_error=no_text_error or "PDF 无文本层",
                page_count=page_count,
            )
        else:
            document = await _persist_document(
                session,
                paper_id=paper_id,
                parsed=parsed,
                source_url=source_url,
                content_sha256=content_sha256,
                min_coverage=min_coverage,
            )
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.exception("upload_document_failed paper_id=%s name=%s", paper_id, stored_name)
        return {
            **base_item,
            "paper_id": paper_id,
            "reason": f"document_persist_failed: {type(exc).__name__}: {exc}",
        }

    return {
        "input": stored_name,
        "status": ITEM_CREATED if upsert.created else ITEM_REUSED,
        "paper_id": paper_id,
        "reason": None,
        "source": SOURCE_NAME,
        "title": metadata["title"],
        "title_source": metadata["title_source"],
        "document_version": document.document_version,
        "parse_status": document.parse_status,
        "coverage": document.coverage,
        "text_sha256": document.text_sha256,
        "matched_by": upsert.matched_by,
    }


# ------------------------------------------------------------------ 批处理
async def run_pdf_import_batch(
    task_id: str,
    files: Sequence[Mapping[str, Any]],
    *,
    project_id: int | None = None,
) -> None:
    """后台批处理：逐个文件导入并实时写入任务项（供轮询）。"""
    from app.db.session import SessionLocal

    if SessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        message = "database_unavailable：同步会话工厂不可用（DATABASE_URL / psycopg 未就绪）"
        jobs.add_note(task_id, message)
        jobs.finish_task(task_id, status=jobs.STATUS_FAILED, error=message)
        return

    for entry in files:
        name = str(entry.get("name") or "")
        path = entry.get("path")
        content = entry.get("content")
        if content is None and path:
            try:
                content = Path(path).read_bytes()
            except OSError as exc:
                jobs.add_item(
                    task_id,
                    {
                        "input": name,
                        "status": ITEM_FAILED,
                        "paper_id": None,
                        "reason": f"stored_file_unreadable: {exc}",
                        "source": SOURCE_NAME,
                    },
                )
                continue
        if not content:
            jobs.add_item(
                task_id,
                {
                    "input": name,
                    "status": ITEM_FAILED,
                    "paper_id": None,
                    "reason": "empty_file",
                    "source": SOURCE_NAME,
                },
            )
            continue

        session = SessionLocal()
        try:
            item = await import_pdf_bytes(
                session,
                stored_name=name,
                content=bytes(content),
                project_id=project_id,
                stored_path=str(path) if path else None,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整批
            session.rollback()
            logger.exception("upload_item_failed name=%s task_id=%s", name, task_id)
            item = {
                "input": name,
                "status": ITEM_FAILED,
                "paper_id": None,
                "reason": f"unexpected_error: {type(exc).__name__}: {exc}",
                "source": SOURCE_NAME,
            }
        finally:
            session.close()
        jobs.add_item(task_id, item)

    jobs.finish_task(task_id, status=jobs.STATUS_DONE)


__all__ = [
    "DOCUMENT_SOURCE_TYPE",
    "PDF_PARSER",
    "SOURCE_NAME",
    "extract_arxiv_id",
    "extract_doi",
    "extract_metadata",
    "find_document_by_content_sha",
    "guess_title",
    "import_pdf_bytes",
    "normalize_doi",
    "run_pdf_import_batch",
]
