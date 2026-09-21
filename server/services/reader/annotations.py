# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""批注与**跨版本对齐**（EasyPaper §4.7）。

原文锚点
--------
批注无论建在哪个版本上，都先解析出**原文语言的锚点**（``anchor_text`` /
``anchor_sha256``）——因为翻译后文字长度会变，页码不可靠（EasyPaper §4.8）。
锚点解析失败时 ``anchor_resolved=false`` 并如实返回，**不做猜测**。

对齐流程（与 §4.7 一致，逐级降级，绝不把不确定伪装成成功）
------------------------------------------------------------
1. **页面对应关系复制**：目标版本的 ``page_map.mode == 'identity'`` 时，
   在对应页上找**完全相同**的锚点文本 → ``method='page_map+exact_text'``；
2. **文本精确匹配**：全页范围找完全相同文本 → ``method='text_match'``；
3. **短语匹配**：锚点与候选文本互为子串且长度比 ≥ 0.5 → ``partial``；
4. **词元相似**：Jaccard ≥ 0.75 且两侧都 ≥ 40 字符 → ``partial``（带 ``similarity``）；
5. 都不中 → 该版本**不产生投影**；全部版本都不中 → 整体 ``status='pending'``。

整体状态：全部目标版本精确命中 → ``success``；有命中但有 ``partial`` 或未覆盖全部版本 →
``partial``；一个都没命中（或只有单一版本）→ ``pending``。``success`` 必须"名副其实"。

乐观锁
------
``PUT`` 必须带 ``revision``；不匹配 → 409 ``annotation_conflict``（旧客户端不许覆盖新修改）。
篡改摘录时锚点会重算并把 ``align_status`` 退回 ``pending``/``projections=[]``
——位置必须重新对齐，不允许沿用旧投影。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.reader import ReaderAnnotation, ReaderDocument, ReaderVersion
from services.reader import documents, parsing, storage, versions
from services.reader.errors import (
    AnnotationConflictError,
    AnnotationNotFoundError,
    AnnotationValidationError,
)

logger = logging.getLogger("sciloop.reader.annotations")

ANNOTATION_KINDS: tuple[str, ...] = ("highlight", "note", "question", "summary")
MAX_QUOTE_CHARS = 2000
MAX_NOTE_CHARS = 4000
#: 锚点过短时拒绝模糊匹配（避免把「the」匹配到全篇）
MIN_ANCHOR_CHARS = 4
#: 短语匹配的长度比下限
CONTAINMENT_MIN_RATIO = 0.5
#: 词元相似度阈值与最小长度
TOKEN_JACCARD_MIN = 0.75
TOKEN_MIN_CHARS = 40

_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9\u4e00-\u9fff]{3,}")


def normalize_text(text: str | None) -> str:
    """匹配用归一化：折叠空白（**不做大小写与标点改写**，避免过度宽松）。"""
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(str(text or "").lower()))


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
def serialize(row: ReaderAnnotation) -> dict[str, Any]:
    return {
        "id": row.id,
        "uid": row.uid,
        "document_id": row.document_id,
        "version_id": row.version_id,
        "block_id": row.block_id,
        "page": row.page,
        "bbox": row.bbox,
        "quote_text": row.quote_text,
        "quote_sha256": row.quote_sha256,
        "anchor_text": row.anchor_text,
        "anchor_sha256": row.anchor_sha256,
        "anchor_resolved": bool(row.anchor_resolved),
        "kind": row.kind,
        "note": row.note,
        "align_status": row.align_status,
        "projections": row.projections,
        "content_fingerprint": row.content_fingerprint,
        "revision": row.revision,
        "created_at": row.created_at.isoformat()
        if hasattr(row.created_at, "isoformat")
        else row.created_at,
        "updated_at": row.updated_at.isoformat()
        if hasattr(row.updated_at, "isoformat")
        else row.updated_at,
    }


def export_payload(
    document: ReaderDocument,
    rows: list[ReaderAnnotation],
    version_rows: list[ReaderVersion],
) -> dict[str, Any]:
    """``GET /annotations/export`` 的 JSON 载荷（可被重新解析，含审计口径）。"""
    return {
        "schema_version": 1,
        "document": {
            "id": document.id,
            "paper_id": document.paper_id,
            "title": document.title,
            "fingerprint": document.fingerprint,
            "payload_sha256": document.payload_sha256,
            "page_count": document.page_count,
            "block_count": document.block_count,
            "parse_status": document.parse_status,
        },
        "versions": [
            {
                "id": version.id,
                "version_no": version.version_no,
                "kind": version.kind,
                "task_id": version.task_id,
                "file_sha256": version.file_sha256,
                "pdf_sha256": version.pdf_sha256,
                "hash_verified": bool(version.hash_verified),
                "page_map": version.page_map,
                "layout_warnings": version.layout_warnings,
                "created_at": version.created_at.isoformat()
                if hasattr(version.created_at, "isoformat")
                else version.created_at,
            }
            for version in version_rows
        ],
        "annotations": [serialize(row) for row in rows],
        "total": len(rows),
        "align_status_counts": {
            status: sum(1 for row in rows if row.align_status == status)
            for status in ("success", "partial", "pending")
        },
        "note": (
            "批注锚点为原文语言 anchor_text；projections 记录逐版本匹配方式与几何，"
            "partial / pending 一律如实保留（不伪装 success）"
        ),
    }


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
async def list_annotations(
    session: AsyncSession,
    document: ReaderDocument,
    *,
    q: str | None = None,
    kind: str | None = None,
    align_status: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[ReaderAnnotation], int]:
    """列出批注（``q`` 搜索摘录 / 笔记 / 锚点；``kind`` 与 ``align_status`` 可筛选）。"""
    conditions = [ReaderAnnotation.document_id == int(document.id)]
    if kind:
        value = str(kind).strip()
        if value not in ANNOTATION_KINDS:
            raise AnnotationValidationError(
                f"kind='{kind}' 非法", detail={"allowed": list(ANNOTATION_KINDS)}
            )
        conditions.append(ReaderAnnotation.kind == value)
    if align_status:
        value = str(align_status).strip()
        if value not in ("success", "partial", "pending"):
            raise AnnotationValidationError(
                f"align_status='{align_status}' 非法",
                detail={"allowed": ["success", "partial", "pending"]},
            )
        conditions.append(ReaderAnnotation.align_status == value)
    if q:
        escaped = (
            str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        conditions.append(
            or_(
                ReaderAnnotation.quote_text.ilike(pattern, escape="\\"),
                ReaderAnnotation.anchor_text.ilike(pattern, escape="\\"),
                ReaderAnnotation.note.ilike(pattern, escape="\\"),
            )
        )

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(ReaderAnnotation).where(*conditions)
            )
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(ReaderAnnotation)
            .where(*conditions)
            .order_by(ReaderAnnotation.id.desc())
            .offset((int(page) - 1) * int(page_size))
            .limit(int(page_size))
        )
    ).scalars().all()
    return list(rows), total


async def get_annotation(
    session: AsyncSession, document: ReaderDocument, annotation_id: int
) -> ReaderAnnotation:
    row = (
        await session.execute(
            select(ReaderAnnotation).where(
                ReaderAnnotation.id == int(annotation_id),
                ReaderAnnotation.document_id == int(document.id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AnnotationNotFoundError(
            f"批注 {annotation_id} 不存在或不属于阅读文档 {document.id}",
            detail={"document_id": int(document.id), "annotation_id": int(annotation_id)},
        )
    return row


# --------------------------------------------------------------------------- #
# 锚点解析
# --------------------------------------------------------------------------- #
async def _version_corpus(
    session: AsyncSession, version: ReaderVersion
) -> tuple[list[dict[str, Any]], str | None]:
    """版本语料：``[{page, source_text, target_text, bbox}]``；索引缺失时返回原因。"""
    try:
        index = versions.read_index(version)
    except Exception as exc:  # noqa: BLE001 - 索引缺失必须如实降级，不阻断其他版本
        return [], f"版本 {version.id}({version.kind}) 文本索引不可读：{exc}"
    corpus: list[dict[str, Any]] = []
    for page_entry in index.get("pages", []) if isinstance(index.get("pages"), list) else []:
        if not isinstance(page_entry, dict):
            continue
        page_number = page_entry.get("page")
        for block in page_entry.get("blocks", []) if isinstance(page_entry.get("blocks"), list) else []:
            if not isinstance(block, dict):
                continue
            corpus.append(
                {
                    "page": page_number,
                    "source_text": normalize_text(block.get("source_text")),
                    "target_text": normalize_text(block.get("target_text")),
                    "bbox": block.get("bbox"),
                }
            )
    return corpus, None


def _exact_match(
    corpus: list[dict[str, Any]], anchor: str, *, page: int | None = None
) -> dict[str, Any] | None:
    for entry in corpus:
        if page is not None and entry.get("page") != page:
            continue
        if entry["source_text"] == anchor or (entry["target_text"] and entry["target_text"] == anchor):
            return entry
    return None


def _containment_match(
    corpus: list[dict[str, Any]], anchor: str, *, page: int | None = None
) -> tuple[dict[str, Any], float] | None:
    best: tuple[dict[str, Any], float] | None = None
    for entry in corpus:
        if page is not None and entry.get("page") != page:
            continue
        for candidate in (entry["source_text"], entry["target_text"]):
            if not candidate:
                continue
            shorter, longer = sorted((anchor, candidate), key=len)
            if not shorter or shorter not in longer:
                continue
            ratio = len(shorter) / len(longer)
            if ratio < CONTAINMENT_MIN_RATIO:
                continue
            if best is None or ratio > best[1]:
                best = (entry, ratio)
    return best


def _token_match(corpus: list[dict[str, Any]], anchor: str) -> tuple[dict[str, Any], float] | None:
    if len(anchor) < TOKEN_MIN_CHARS:
        return None
    anchor_tokens = _tokens(anchor)
    if len(anchor_tokens) < 5:
        return None
    best: tuple[dict[str, Any], float] | None = None
    for entry in corpus:
        for candidate in (entry["source_text"], entry["target_text"]):
            if len(candidate or "") < TOKEN_MIN_CHARS:
                continue
            candidate_tokens = _tokens(candidate)
            if not candidate_tokens:
                continue
            union = anchor_tokens | candidate_tokens
            jaccard = len(anchor_tokens & candidate_tokens) / len(union)
            if jaccard >= TOKEN_JACCARD_MIN and (best is None or jaccard > best[1]):
                best = (entry, jaccard)
    return best


async def resolve_anchor(
    session: AsyncSession,
    document: ReaderDocument,
    *,
    version: ReaderVersion,
    quote_text: str,
    block_id: str | None,
    page: int | None,
) -> dict[str, Any]:
    """解析批注锚点：优先落到具体 block，其次落到版本语料的对应块。"""
    quote = normalize_text(quote_text)
    if version.kind == "original":
        known = parsing.block_id_index(document.document)
        corpus = [
            {
                "page": block.get("page"),
                "source_text": normalize_text(block.get("source_text")),
                "target_text": "",
                "bbox": block.get("bbox"),
            }
            for block in parsing.iter_blocks(document.document)
        ]
        if block_id and block_id in known:
            block = known[block_id]
            block_text = normalize_text(block.get("source_text"))
            if quote and (quote in block_text or block_text in quote):
                return {
                    "anchor_text": block_text or quote,
                    "block_id": block_id,
                    "page": int(block.get("page") or page or 0) or page,
                    "bbox": block.get("bbox"),
                    "anchor_resolved": True,
                    "method": "block_id",
                }
        hit = _exact_match(corpus, quote, page=page)
        if hit is None:
            found = _containment_match(corpus, quote, page=page)
            hit = found[0] if found else None
        if hit is not None:
            return {
                "anchor_text": hit["source_text"] or quote,
                "block_id": block_id,
                "page": hit.get("page"),
                "bbox": hit.get("bbox"),
                "anchor_resolved": True,
                "method": "original_block_text",
            }
        return {
            "anchor_text": quote,
            "block_id": block_id,
            "page": page,
            "bbox": None,
            "anchor_resolved": False,
            "method": "quote_only",
        }

    corpus, reason = await _version_corpus(session, version)
    if reason:
        return {
            "anchor_text": quote,
            "block_id": block_id,
            "page": page,
            "bbox": None,
            "anchor_resolved": False,
            "method": "index_unavailable",
            "note": reason,
        }
    hit = _exact_match(corpus, quote, page=page)
    if hit is None:
        found = _containment_match(corpus, quote, page=page)
        if found:
            hit = found[0]
    if hit is None:
        return {
            "anchor_text": quote,
            "block_id": block_id,
            "page": page,
            "bbox": None,
            "anchor_resolved": False,
            "method": "quote_only",
        }
    return {
        "anchor_text": hit["source_text"] or quote,
        "block_id": block_id,
        "page": hit.get("page"),
        "bbox": hit.get("bbox"),
        "anchor_resolved": True,
        "method": "version_block_text",
    }


# --------------------------------------------------------------------------- #
# 创建 / 更新 / 删除
# --------------------------------------------------------------------------- #
async def create_annotation(
    session: AsyncSession, document: ReaderDocument, *, fields: dict[str, Any]
) -> ReaderAnnotation:
    version_id = fields.get("version_id")
    if version_id is None:
        raise AnnotationValidationError("version_id 必填（批注必须标注来源版本）")
    version = await versions.get_version(session, document, int(version_id))

    quote_raw = fields.get("quote_text")
    if quote_raw is None or not str(quote_raw).strip():
        raise AnnotationValidationError("quote_text 必填且不能为空白")
    quote_text = str(quote_raw).strip()
    if len(quote_text) > MAX_QUOTE_CHARS:
        raise AnnotationValidationError(
            f"quote_text 长度 {len(quote_text)} 超过上限 {MAX_QUOTE_CHARS}",
            detail={"limit": MAX_QUOTE_CHARS},
        )
    note = fields.get("note")
    if note is not None:
        note = str(note)
        if len(note) > MAX_NOTE_CHARS:
            raise AnnotationValidationError(
                f"note 长度 {len(note)} 超过上限 {MAX_NOTE_CHARS}", detail={"limit": MAX_NOTE_CHARS}
            )
    kind = str(fields.get("kind") or "highlight").strip()
    if kind not in ANNOTATION_KINDS:
        raise AnnotationValidationError(
            f"kind='{kind}' 非法", detail={"allowed": list(ANNOTATION_KINDS)}
        )
    block_id = fields.get("block_id")
    page = fields.get("page")
    page = int(page) if isinstance(page, int) and not isinstance(page, bool) else None
    if block_id and version.kind == "original":
        known = documents.block_ids(document)
        if str(block_id) not in known:
            raise AnnotationValidationError(
                f"block_id='{block_id}' 不在阅读文档 {document.id} 的稳定 block id 集合内",
                detail={"document_id": int(document.id)},
            )

    anchor = await resolve_anchor(
        session,
        document,
        version=version,
        quote_text=quote_text,
        block_id=str(block_id) if block_id else None,
        page=page,
    )
    row = ReaderAnnotation(
        document_id=int(document.id),
        uid=uuid.uuid4().hex,
        version_id=int(version.id),
        block_id=anchor.get("block_id"),
        page=anchor.get("page"),
        bbox=anchor.get("bbox"),
        quote_text=quote_text,
        quote_sha256=storage.sha256_bytes(quote_text.encode("utf-8")),
        anchor_text=anchor["anchor_text"],
        anchor_sha256=storage.sha256_bytes(anchor["anchor_text"].encode("utf-8")),
        anchor_resolved=bool(anchor["anchor_resolved"]),
        kind=kind,
        note=note,
        align_status="pending",
        projections=[],
        content_fingerprint=document.payload_sha256,
        revision=1,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "reader_annotation_created document_id=%s annotation_id=%s kind=%s anchor_resolved=%s method=%s",
        document.id,
        row.id,
        kind,
        row.anchor_resolved,
        anchor.get("method"),
    )
    return row


async def update_annotation(
    session: AsyncSession,
    document: ReaderDocument,
    annotation_id: int,
    *,
    fields: dict[str, Any],
) -> ReaderAnnotation:
    row = await get_annotation(session, document, annotation_id)
    if "revision" not in fields or fields["revision"] is None:
        raise AnnotationValidationError(
            "PUT 必须携带 revision（乐观锁）：先 GET 批注再回写",
            detail={"current_revision": row.revision},
        )
    provided = fields["revision"]
    if isinstance(provided, bool) or not isinstance(provided, int):
        raise AnnotationValidationError("revision 必须是整数")
    if provided != int(row.revision):
        raise AnnotationConflictError(
            f"批注 {row.id} 的 revision={row.revision}，请求携带 {provided}："
            "拒绝旧客户端覆盖新修改",
            detail={"annotation_id": row.id, "current_revision": row.revision, "provided": provided},
        )

    changed: list[str] = []
    if fields.get("quote_text") is not None:
        quote_text = str(fields["quote_text"]).strip()
        if not quote_text:
            raise AnnotationValidationError("quote_text 不能为空白")
        if len(quote_text) > MAX_QUOTE_CHARS:
            raise AnnotationValidationError(
                f"quote_text 长度 {len(quote_text)} 超过上限 {MAX_QUOTE_CHARS}"
            )
        if quote_text != row.quote_text:
            version = await versions.get_version(session, document, int(row.version_id or 0)) if row.version_id else None
            row.quote_text = quote_text
            row.quote_sha256 = storage.sha256_bytes(quote_text.encode("utf-8"))
            if version is not None:
                anchor = await resolve_anchor(
                    session,
                    document,
                    version=version,
                    quote_text=quote_text,
                    block_id=row.block_id,
                    page=row.page,
                )
                row.anchor_text = anchor["anchor_text"]
                row.anchor_sha256 = storage.sha256_bytes(anchor["anchor_text"].encode("utf-8"))
                row.anchor_resolved = bool(anchor["anchor_resolved"])
                row.page = anchor.get("page")
                row.bbox = anchor.get("bbox")
            # 摘录变了 → 旧投影失效，必须重新对齐（不允许沿用旧位置）
            row.align_status = "pending"
            row.projections = []
            changed.append("quote_text")
    if "note" in fields:
        note = fields["note"]
        if note is not None and len(str(note)) > MAX_NOTE_CHARS:
            raise AnnotationValidationError(
                f"note 长度 {len(str(note))} 超过上限 {MAX_NOTE_CHARS}"
            )
        row.note = None if note is None else str(note)
        changed.append("note")
    if "kind" in fields and fields["kind"] is not None:
        kind = str(fields["kind"]).strip()
        if kind not in ANNOTATION_KINDS:
            raise AnnotationValidationError(
                f"kind='{kind}' 非法", detail={"allowed": list(ANNOTATION_KINDS)}
            )
        row.kind = kind
        changed.append("kind")
    if fields.get("block_id") is not None:
        block_id = str(fields["block_id"])
        if block_id not in documents.block_ids(document):
            raise AnnotationValidationError(
                f"block_id='{block_id}' 不在阅读文档 {document.id} 的稳定 block id 集合内"
            )
        row.block_id = block_id
        changed.append("block_id")

    if not changed:
        raise AnnotationValidationError(
            "请求体未包含任何可修改字段（quote_text / note / kind / block_id）"
        )
    row.revision = int(row.revision or 1) + 1
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "reader_annotation_updated document_id=%s annotation_id=%s fields=%s revision=%s",
        document.id,
        row.id,
        changed,
        row.revision,
    )
    return row


async def delete_annotation(
    session: AsyncSession, document: ReaderDocument, annotation_id: int
) -> None:
    row = await get_annotation(session, document, annotation_id)
    await session.delete(row)
    await session.commit()
    logger.info("reader_annotation_deleted document_id=%s annotation_id=%s", document.id, row.id)


# --------------------------------------------------------------------------- #
# 跨版本对齐
# --------------------------------------------------------------------------- #
async def align_annotation(
    session: AsyncSession, document: ReaderDocument, annotation_id: int
) -> dict[str, Any]:
    """把批注投影到该文档的其他版本上（``success`` / ``partial`` / ``pending``）。"""
    row = await get_annotation(session, document, annotation_id)
    version_rows = await versions.list_versions(session, document)
    targets = [version for version in version_rows if version.id != row.version_id]
    anchor = normalize_text(row.anchor_text)
    notes: list[str] = []
    projections: list[dict[str, Any]] = []

    if not targets:
        status = "pending"
        notes.append(
            "该阅读文档在当前批注来源之外没有其他版本（例如只有 original）："
            "无可对齐目标，如实返回 pending，不伪造成功"
        )
    elif len(anchor) < MIN_ANCHOR_CHARS:
        status = "pending"
        notes.append(
            f"锚点文本过短（{len(anchor)} 字符 < {MIN_ANCHOR_CHARS}）：拒绝模糊匹配，返回 pending"
        )
    else:
        partial_hit = False
        exact_miss = False
        for target in targets:
            corpus, reason = await _version_corpus(session, target)
            if reason:
                notes.append(reason)
                exact_miss = True
                continue
            hit: dict[str, Any] | None = None
            page_hint: int | None = None
            page_map = target.page_map if isinstance(target.page_map, dict) else {}
            if row.page is not None and page_map.get("mode") == "identity":
                page_hint = int(row.page)
                found = _exact_match(corpus, anchor, page=page_hint)
                if found is not None:
                    hit = {
                        "entry": found,
                        "method": "page_map+exact_text",
                        "similarity": 1.0,
                        "partial": False,
                        "note": "页数一致 → 先按页号复制位置，再要求文本完全相同",
                    }
            if hit is None:
                found = _exact_match(corpus, anchor)
                if found is not None:
                    hit = {
                        "entry": found,
                        "method": "text_match",
                        "similarity": 1.0,
                        "partial": False,
                        "note": "全页范围内锚点文本完全相同",
                    }
            if hit is None:
                found = _containment_match(corpus, anchor, page=page_hint)
                if found is None:
                    found = _containment_match(corpus, anchor)
                if found is not None:
                    hit = {
                        "entry": found[0],
                        "method": "phrase_match",
                        "similarity": round(found[1], 4),
                        "partial": True,
                        "note": "锚点与候选文本互为子串（长度比不足 1.0）：只能给部分匹配几何",
                    }
            if hit is None:
                found = _token_match(corpus, anchor)
                if found is not None:
                    hit = {
                        "entry": found[0],
                        "method": "phrase_match",
                        "similarity": round(found[1], 4),
                        "partial": True,
                        "note": "词元 Jaccard 相似度命中：属于部分匹配，禁止标 success",
                    }
            if hit is None:
                exact_miss = True
                continue
            entry = hit["entry"]
            partial_hit = partial_hit or bool(hit["partial"])
            projections.append(
                {
                    "version_id": target.id,
                    "version_no": target.version_no,
                    "kind": target.kind,
                    "task_id": target.task_id,
                    "method": hit["method"],
                    "status": "partial" if hit["partial"] else "matched",
                    "similarity": hit["similarity"],
                    "target_page": entry.get("page"),
                    "target_bbox": entry.get("bbox"),
                    "target_text": entry.get("target_text") or entry.get("source_text"),
                    "matched_source_text": entry.get("source_text"),
                    "note": hit["note"],
                }
            )

        if not projections:
            status = "pending"
            notes.append(
                "所有目标版本均未匹配到锚点：返回 pending（不猜测位置），"
                "可人工确认后重新创建批注"
            )
        elif partial_hit or exact_miss or len(projections) < len(targets):
            status = "partial"
            if partial_hit:
                notes.append("存在部分匹配（子串/词元相似）：状态必须为 partial，禁止伪装 success")
            if len(projections) < len(targets):
                notes.append(
                    f"仅覆盖 {len(projections)}/{len(targets)} 个目标版本，其余保持未投影"
                )
        else:
            status = "success"
            notes.append("全部目标版本均按精确文本命中")

    row.align_status = status
    row.projections = projections
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "reader_annotation_aligned document_id=%s annotation_id=%s status=%s projections=%d targets=%d",
        document.id,
        row.id,
        status,
        len(projections),
        len(targets),
    )
    return {
        "annotation_id": row.id,
        "document_id": row.document_id,
        "status": status,
        "projections": projections,
        "anchor_text": row.anchor_text,
        "anchor_resolved": bool(row.anchor_resolved),
        "target_version_ids": [version.id for version in targets],
        "checked_at": datetime.now(UTC).isoformat(),
        "notes": notes,
        "revision": row.revision,
    }


__all__ = [
    "ANNOTATION_KINDS",
    "CONTAINMENT_MIN_RATIO",
    "MAX_NOTE_CHARS",
    "MAX_QUOTE_CHARS",
    "MIN_ANCHOR_CHARS",
    "TOKEN_JACCARD_MIN",
    "align_annotation",
    "create_annotation",
    "delete_annotation",
    "export_payload",
    "get_annotation",
    "list_annotations",
    "normalize_text",
    "resolve_anchor",
    "serialize",
    "update_annotation",
]
