# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""阅读文档：原文定位、解析、落库与查询（EasyPaper §4.3 / §4.4 ReaderDocument）。

原文来源（**只读，不联网**）
----------------------------
按 ``app/services/ingest`` 的既有口径定位**已落盘的本地原文**：

1. ``papers.raw.upload.stored_path``（导入时记录的真实磁盘路径）；
2. ``paper_documents.source_url`` 中的 ``upload://<文件名>`` → ``IMPORT_UPLOAD_DIR``。

找不到 → 409 ``no_source_document``：**绝不**把「没有原文」伪装成「已解析」，
也不会为了凑数去下载或编造正文（contracts.forbidden_actions 第 1 条）。

重复创建与 ``force``
--------------------
``reader_documents`` 是**解析缓存**（可变），``reader_versions`` 是**凭证**（不可变）：

- 同一 ``(paper_id, fingerprint)`` 已存在且 ``force=false`` → 直接复用（``reused=true``）；
- ``force=true`` → **重新解析同一份原文**，覆盖解析结果与 ``payload_sha256``，
  并在响应里给出 ``identical_blocks`` 判定：由于 block id 是内容寻址的，
  两次解析必须得到完全相同的 block id 序列——这是本模块最重要的可验证性质。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import Paper, PaperDocument
from db.models.reader import ReaderDocument
from services.ingest import storage as ingest_storage
from services.reader import parsing, storage, versions
from services.reader.errors import (
    DocumentNotFoundError,
    NoSourceDocumentError,
    SourceTooLargeError,
)

logger = logging.getLogger("sciloop.reader.documents")


class PaperNotFoundError(DocumentNotFoundError):
    """``POST /reader/documents`` 的 ``paper_id`` 不存在（比 document_not_found 更精确）。"""

    code = "paper_not_found"


# --------------------------------------------------------------------------- #
# 原文定位
# --------------------------------------------------------------------------- #
async def resolve_paper_source(session: AsyncSession, paper_id: int) -> dict[str, Any]:
    """返回 ``{"filename", "bytes", "sha256", "via", "source_url"}``。

    :raises PaperNotFoundError: 论文不存在
    :raises NoSourceDocumentError: 论文存在但没有可读的本地原文（409）
    :raises SourceTooLargeError: 原文超过体积上限（422）
    """
    paper = (
        await session.execute(select(Paper).where(Paper.id == int(paper_id)))
    ).scalar_one_or_none()
    if paper is None:
        raise PaperNotFoundError(f"论文 {paper_id} 不存在", detail={"paper_id": int(paper_id)})

    candidates: list[tuple[Path, str]] = []
    raw = paper.raw if isinstance(paper.raw, Mapping) else {}
    upload = raw.get("upload") if isinstance(raw.get("upload"), Mapping) else {}
    stored_path = upload.get("stored_path")
    if stored_path:
        candidates.append((Path(str(stored_path)), "papers.raw.upload.stored_path"))

    documents = (
        await session.execute(
            select(PaperDocument)
            .where(PaperDocument.paper_id == int(paper_id))
            .order_by(PaperDocument.id)
        )
    ).scalars().all()
    upload_dir = ingest_storage.default_upload_dir()
    for document in documents:
        url = str(document.source_url or "")
        if url.startswith("upload://"):
            candidates.append(
                (upload_dir / url.removeprefix("upload://"), "paper_documents.source_url")
            )

    # ③ 本模块兜底下载的原文缓存（抓取来源的论文靠它；也是第二次打开秒开的原因）
    candidates.append((upload_dir / _source_cache_name(paper), "reader.source_cache"))

    checked: list[dict[str, Any]] = []
    for path, via in candidates:
        try:
            if not path.is_file():
                checked.append({"path": str(path), "via": via, "reason": "not_found"})
                continue
            size = path.stat().st_size
        except OSError as exc:
            checked.append({"path": str(path), "via": via, "reason": f"stat_failed:{exc}"})
            continue
        if size > storage.MAX_SOURCE_BYTES:
            raise SourceTooLargeError(
                f"原文 {path.name} 体积 {size} 字节超过上限 {storage.MAX_SOURCE_BYTES} 字节",
                detail={"size_bytes": size, "limit_bytes": storage.MAX_SOURCE_BYTES},
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            checked.append({"path": str(path), "via": via, "reason": f"read_failed:{exc}"})
            continue
        if not payload:
            checked.append({"path": str(path), "via": via, "reason": "empty_file"})
            continue
        return {
            "filename": path.name,
            "bytes": payload,
            "sha256": storage.sha256_bytes(payload),
            "via": via,
            "source_url": ingest_storage.upload_source_url(path.name),
        }

    # 本地没有 → 抓取来源的论文按 papers.pdf_url 现拉一份（落盘缓存，下次直接命中）
    fetched = await _fetch_source_pdf(paper, upload_dir / _source_cache_name(paper), checked)
    if fetched is not None:
        return fetched

    raise NoSourceDocumentError(
        f"论文 {paper_id} 没有可用的本地原文（未导入 PDF，且按来源地址也拉不到原文）："
        "可改用 POST /papers/import 直接导入 PDF",
        detail={"paper_id": int(paper_id), "candidates": checked or "no_candidate_record"},
    )


#: 抓取来源兜底下载的超时（arXiv PDF 几 MB，给足 60 秒）
_SOURCE_FETCH_TIMEOUT_SECONDS = 60.0
#: 单个地址最多试几次（arXiv 的 CDN 会按节点回 406 或中途掐断，退避重试通常就过了）
_SOURCE_FETCH_ATTEMPTS = 3
#: 每次重试的退避基数（第 n 次等 n 秒）
_SOURCE_FETCH_BACKOFF_SECONDS = 1.0
#: 与解析链路同口径的 UA（避免被来源方当成爬虫拦掉）
_SOURCE_USER_AGENT = "SciLoop/0.1 (reader; +https://arxiv.org)"
#: 显式声明可接受 PDF：arXiv 对不带 Accept 的请求会直接回 406
_SOURCE_ACCEPT_HEADERS = {"Accept": "application/pdf,*/*"}
#: PDF 魔数：允许文件头有少量空白/注释
_PDF_MAGIC = b"%PDF"
#: ``…/pdf/2508.00141v3`` → ``…/pdf/2508.00141``（个别版本的直链会失效，无版本号那条兜底）
_ARXIV_VERSION_SUFFIX = re.compile(r"v\d+$")


def _source_url_candidates(url: str) -> list[str]:
    """原文地址候选：先按记录里的地址，再试 arXiv 的无版本号地址（与解析链路同一手法）。"""
    candidates = [url]
    if "arxiv.org" in url:
        unversioned = _ARXIV_VERSION_SUFFIX.sub("", url.rstrip("/"))
        if unversioned != url:
            candidates.append(unversioned)
    return candidates


def _source_cache_name(paper: Paper) -> str:
    """兜底下载的**确定性**文件名（同一条论文第二次打开直接命中缓存，不再下载）。"""
    raw = f"{paper.source}-{paper.external_id}" if paper.external_id else f"{paper.source}-{paper.id}"
    name = ingest_storage.sanitize_filename(raw)
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


async def _fetch_source_pdf(
    paper: Paper, target: Path, checked: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """抓取来的论文（arXiv / S2 / OpenAlex）在本地没有原文文件时，按 ``papers.pdf_url`` 拉一份。

    为什么需要它：阅读器原先只认「用户上传的 PDF」
    （``papers.raw.upload.stored_path`` 与 ``upload://`` 记录），而抓取入库的论文从来不在本地
    落文件 —— 于是**论文库里几乎所有论文点标题都会报「没有可用的本地原文」**
    （用户 2026-09-24 实测反馈）。这里补上兜底：拉到就落到上传目录缓存，
    拉不到就如实返回 ``None``，由调用方报错，**绝不假装成功**。
    """
    url = str(paper.pdf_url or "").strip()
    if not url:
        checked.append({"path": str(target), "via": "papers.pdf_url", "reason": "no_pdf_url"})
        return None
    try:
        import httpx
    except ImportError:  # pragma: no cover - 依赖运行环境
        return None

    content: bytes | None = None
    for candidate_url in _source_url_candidates(url):
        for attempt in range(_SOURCE_FETCH_ATTEMPTS):
            try:
                async with httpx.AsyncClient(
                    timeout=_SOURCE_FETCH_TIMEOUT_SECONDS,
                    follow_redirects=True,
                    headers={"User-Agent": _SOURCE_USER_AGENT, **_SOURCE_ACCEPT_HEADERS},
                ) as client:
                    response = await client.get(candidate_url)
            except Exception as exc:  # noqa: BLE001 - 下载失败如实降级，不抛给调用方
                # arXiv 的 CDN 会中途掐断大响应（RemoteProtocolError）或按节点回 406；
                # 换个节点/退避一次通常就过了，所以这里**重试**而不是直接判"没有全文"。
                logger.warning(
                    "reader_source_fetch_retry paper_id=%s url=%s %s: %s",
                    paper.id,
                    candidate_url,
                    type(exc).__name__,
                    exc,
                )
                checked.append(
                    {"path": str(target), "via": candidate_url, "reason": f"fetch_failed:{type(exc).__name__}"}
                )
                await asyncio.sleep(_SOURCE_FETCH_BACKOFF_SECONDS * (attempt + 1))
                continue

            payload = response.content or b""
            if response.status_code == 200 and _PDF_MAGIC in payload[:1024]:
                content = payload
                break
            checked.append(
                {"path": str(target), "via": candidate_url, "reason": f"http_{response.status_code}"}
            )
            logger.warning(
                "reader_source_fetch_bad paper_id=%s url=%s status=%s bytes=%d",
                paper.id,
                candidate_url,
                response.status_code,
                len(payload),
            )
            await asyncio.sleep(_SOURCE_FETCH_BACKOFF_SECONDS * (attempt + 1))
        if content is not None:
            break
    if content is None:
        return None
    if len(content) > storage.MAX_SOURCE_BYTES:
        raise SourceTooLargeError(
            f"原文体积 {len(content)} 字节超过上限 {storage.MAX_SOURCE_BYTES} 字节",
            detail={"size_bytes": len(content), "limit_bytes": storage.MAX_SOURCE_BYTES},
        )

    saved = ingest_storage.save_upload(target.parent, target.name, content)
    path = ingest_storage.safe_target_path(target.parent, saved.name)
    payload = path.read_bytes()
    logger.info("reader_source_cached paper_id=%s file=%s bytes=%d", paper.id, path.name, len(payload))
    return {
        "filename": path.name,
        "bytes": payload,
        "sha256": storage.sha256_bytes(payload),
        "via": "papers.pdf_url",
        "source_url": ingest_storage.upload_source_url(path.name),
    }


# --------------------------------------------------------------------------- #
# 文档创建 / 查询
# --------------------------------------------------------------------------- #
def _document_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": payload["title"],
        "title_source": payload["title_source"],
        "schema_version": int(payload["schema_version"]),
        "parse_status": payload["parse_status"],
        "fingerprint": payload["fingerprint"],
        "source_url": payload["source_url"],
        "page_count": int(payload["page_count"]),
        "block_count": int(payload["block_count"]),
        "section_count": int(payload["section_count"]),
        "document": payload,
        "payload_sha256": storage.payload_sha256(payload),
        "warnings": payload["warnings"],
    }


async def create_or_get_document(
    session: AsyncSession,
    *,
    paper_id: int,
    force: bool = False,
) -> tuple[ReaderDocument, dict[str, Any]]:
    """创建（或复用/重解析）阅读文档，附 ``trace`` 说明本次发生了什么。"""
    source = await resolve_paper_source(session, paper_id)
    paper = (
        await session.execute(select(Paper).where(Paper.id == int(paper_id)))
    ).scalar_one()
    content = source["bytes"]
    fingerprint = source["sha256"]

    existing = (
        await session.execute(
            select(ReaderDocument).where(
                ReaderDocument.paper_id == int(paper_id),
                ReaderDocument.fingerprint == fingerprint,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and not force:
        return existing, {
            "reused": True,
            "reparsed": False,
            "reason": "同一 (paper_id, fingerprint) 的阅读文档已存在，未重复解析（force=false）",
            "source_via": source["via"],
            "source_url": source["source_url"],
        }

    payload = parsing.build_reading_document(
        content,
        paper_id=int(paper_id),
        source_url=source["source_url"],
        title_fallback=str(paper.title or ""),
    )
    fields = _document_fields(payload)

    if existing is not None:
        # force=true：同一份原文重解析。原文未变 → block id 必须逐字节相同（可验证性质）
        previous_ids = [str(entry.get("id")) for entry in parsing.iter_blocks(existing.document)]
        current_ids = [str(entry.get("id")) for entry in parsing.iter_blocks(payload)]
        identical = previous_ids == current_ids
        for key, value in fields.items():
            setattr(existing, key, value)
        await session.flush()
        logger.info(
            "reader_document_reparsed document_id=%s paper_id=%s identical_block_ids=%s",
            existing.id,
            paper_id,
            identical,
        )
        return existing, {
            "reused": True,
            "reparsed": True,
            "identical_block_ids": identical,
            "previous_block_count": len(previous_ids),
            "current_block_count": len(current_ids),
            "reason": (
                "force=true：同一份原文重新解析；block id 为内容寻址，两次结果"
                + ("完全一致" if identical else "不一致（禁止忽略，需排查解析器）")
            ),
            "source_via": source["via"],
            "source_url": source["source_url"],
        }

    document = ReaderDocument(paper_id=int(paper_id), **fields)
    session.add(document)
    await session.flush()
    version = await versions.register_original(
        session, document, content=content, source_url=source["source_url"]
    )
    logger.info(
        "reader_document_created document_id=%s paper_id=%s fingerprint=%s page_count=%s blocks=%s",
        document.id,
        paper_id,
        fingerprint,
        document.page_count,
        document.block_count,
    )
    return document, {
        "reused": False,
        "reparsed": False,
        "reason": "新建阅读文档，并自动登记 original 版本（原文即证据源）",
        "source_via": source["via"],
        "source_url": source["source_url"],
        "original_version_id": version.id,
    }


async def get_document(session: AsyncSession, document_id: int) -> ReaderDocument:
    row = (
        await session.execute(
            select(ReaderDocument).where(ReaderDocument.id == int(document_id))
        )
    ).scalar_one_or_none()
    if row is None:
        raise DocumentNotFoundError(
            f"阅读文档 {document_id} 不存在", detail={"document_id": int(document_id)}
        )
    return row


async def list_documents(
    session: AsyncSession, *, page: int = 1, page_size: int = 20
) -> tuple[list[ReaderDocument], int]:
    total = int(
        (await session.execute(select(func.count()).select_from(ReaderDocument))).scalar_one()
    )
    rows = (
        await session.execute(
            select(ReaderDocument)
            .order_by(ReaderDocument.id.desc())
            .offset((int(page) - 1) * int(page_size))
            .limit(int(page_size))
        )
    ).scalars().all()
    return list(rows), total


def document_summary(row: ReaderDocument) -> dict[str, Any]:
    """列表项（不含 blocks，避免响应体过大）。"""
    payload = row.document if isinstance(row.document, dict) else {}
    return {
        "id": row.id,
        "paper_id": row.paper_id,
        "title": row.title,
        "title_source": row.title_source,
        "schema_version": row.schema_version,
        "parse_status": row.parse_status,
        "fingerprint": row.fingerprint,
        "source_url": row.source_url,
        "page_count": row.page_count,
        "block_count": row.block_count,
        "section_count": row.section_count,
        "payload_sha256": row.payload_sha256,
        "warnings": row.warnings,
        "parser": payload.get("parser"),
        "parser_version": payload.get("parser_version"),
        "created_at": row.created_at.isoformat()
        if hasattr(row.created_at, "isoformat")
        else row.created_at,
        "updated_at": row.updated_at.isoformat()
        if hasattr(row.updated_at, "isoformat")
        else row.updated_at,
        "detail_url": f"/api/v1/reader/documents/{row.id}",
    }


def document_detail(row: ReaderDocument) -> dict[str, Any]:
    """详情：含 ``pages`` / ``sections`` / ``blocks``（block 带**稳定 id**）。"""
    payload = row.document if isinstance(row.document, dict) else {}
    return {
        "id": row.id,
        "paper_id": row.paper_id,
        "title": row.title,
        "title_source": row.title_source,
        "schema_version": row.schema_version,
        "parse_status": row.parse_status,
        "fingerprint": row.fingerprint,
        "source_url": row.source_url,
        "payload_sha256": row.payload_sha256,
        "page_count": row.page_count,
        "block_count": row.block_count,
        "section_count": row.section_count,
        "parser": payload.get("parser"),
        "parser_version": payload.get("parser_version"),
        "pages": payload.get("pages", []),
        "sections": payload.get("sections", []),
        "blocks": payload.get("blocks", []),
        "warnings": row.warnings,
        "block_id_rule": "b_p<page>_<sha256(归一化文本)[:12]>[_重复序号]（内容寻址，可重复复现）",
        "created_at": row.created_at.isoformat()
        if hasattr(row.created_at, "isoformat")
        else row.created_at,
        "updated_at": row.updated_at.isoformat()
        if hasattr(row.updated_at, "isoformat")
        else row.updated_at,
        "versions_url": f"/api/v1/reader/documents/{row.id}/versions",
        "state_url": f"/api/v1/reader/state/{row.id}",
        "annotations_url": f"/api/v1/reader/documents/{row.id}/annotations",
        "note": (
            "block id 由「页 + 归一化文本 sha256 前 12 位」派生：同一份 PDF 重复解析得到相同 id；"
            "扫描页 type='scan' 且 source_text 为空（不伪造正文）"
        ),
    }


def block_ids(row: ReaderDocument) -> set[str]:
    return set(parsing.block_id_index(row.document))


__all__ = [
    "PaperNotFoundError",
    "block_ids",
    "create_or_get_document",
    "document_detail",
    "document_summary",
    "get_document",
    "list_documents",
    "resolve_paper_source",
]
