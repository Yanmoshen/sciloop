# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文解析记录与原文定位端点（WP05-T6，附录 B.1）。

============================  ==========================================================
``GET  /papers/{id}/documents``   该论文的全部 ``paper_documents`` 记录 + ``fulltext`` 摘要
``GET  /papers/{id}/spans``       原文定位片段，每条带 ``verify_span`` 校验结论
``POST /papers/{id}/parse``      触发全文解析（长任务，owner 面，返回 ``task_id``）
``GET  /papers/{id}/parse-jobs/{}`` 查询解析任务状态（配套 POST 的轮询口）
============================  ==========================================================

口径（硬约束，禁止放宽）
------------------------
- ``document_version`` = 源 URL + 内容 SHA-256 前 12 位；同一论文允许多版本共存。
- ``parse_status`` / ``coverage`` / ``parse_error`` 如实返回，**未解析不等于已解析**
  （无记录 → ``parse_status=null`` 且 ``evidence_scope='abstract_only'``）。
- 只有 ``parse_status='ok'`` 且 ``coverage>=0.60`` 才允许产出正文 span
  （contracts.evidence_rules.fulltext_gate）；此时 ``spans_allowed=true``。
- ``verify_span`` 的 verdict 判定顺序为**哈希优先**：文本一致但偏移漂移 →
  ``valid_by_hash``；文本被改动 → ``invalid``；全文缓存缺失 → ``offset_match=null``，
  绝不伪报通过。
- 写操作（POST /parse）仅 owner 面可用，``public_demo`` 面一律 403。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.session import AsyncSessionLocal
from services.fulltext import (
    SECTION_NAMES,
    SqlDocumentRepository,
    summarize_documents,
    verify_span,
)
from services.fulltext.text_cache import default_text_cache
from tasks.jobs import parse_fulltext

logger = logging.getLogger("sciloop.wp05.documents")

router = APIRouter(tags=["documents"])

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100
#: 章节参数（受控值域）
SECTION_HELP = "受控章节名之一：" + " / ".join(SECTION_NAMES)


# --------------------------------------------------------------------------------------
# 会话依赖与错误体（统一 {code,message,detail}）
# --------------------------------------------------------------------------------------
async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_unavailable",
            "异步数据库会话工厂不可用（DATABASE_URL / asyncpg 未就绪）",
        )
    async with AsyncSessionLocal() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(_session)]


def _error(
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


async def _require_paper(repository: SqlDocumentRepository, paper_id: int) -> None:
    """论文不存在 → 404（禁止对不存在的论文伪造解析记录）。"""
    paper = await repository.get_paper(paper_id)
    if paper is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "paper_not_found",
            f"论文 {paper_id} 不存在",
        )


def _normalize_section(section: str | None) -> str | None:
    """``section`` 归一为受控值；非法值 → 400 契约错误体。"""
    if section is None:
        return None
    value = section.strip().lower()
    if not value:
        return None
    if value not in SECTION_NAMES:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_section",
            f"section='{section}' 不是受控章节名",
            {"allowed": list(SECTION_NAMES)},
        )
    return value


# --------------------------------------------------------------------------------------
# GET /papers/{id}/documents
# --------------------------------------------------------------------------------------
@router.get(
    "/papers/{paper_id}/documents",
    summary="全文解析记录（document_version / parse_status / coverage）",
)
async def list_paper_documents(paper_id: int, session: DbSession) -> dict[str, Any]:
    """返回该论文的全部解析版本记录，并附 ``fulltext`` 摘要（附录 B.1 口径）。

    摘要由 ``summarize_documents`` 依据「最能支撑证据」的一条记录（``spans_allowed``
    且 coverage 最高）计算；无记录时 ``parse_status=null`` 并明示仅摘要级证据。
    """
    repository = SqlDocumentRepository(session)
    await _require_paper(repository, paper_id)

    documents = await repository.list_documents(paper_id)
    items = [document.to_api_dict() for document in documents]
    summary = summarize_documents(documents)
    logger.info(
        "list_documents paper_id=%s total=%d parse_status=%s coverage=%s",
        paper_id,
        len(items),
        summary.get("parse_status"),
        summary.get("coverage"),
    )
    return {
        "paper_id": paper_id,
        "items": items,
        "total": len(items),
        "summary": summary,
        "latest_document_version": items[0]["document_version"] if items else None,
        "coverage_note": summary.get("coverage_note"),
        "spans_allowed": bool(summary.get("spans_allowed")),
        "evidence_scope": summary.get("evidence_scope"),
        "note": (
            "document_version = 源 URL + 内容 SHA-256 前 12 位；"
            "parse_status/coverage/parse_error 均为落库真实值，未解析不伪造"
        ),
    }


# --------------------------------------------------------------------------------------
# GET /papers/{id}/spans
# --------------------------------------------------------------------------------------
@router.get(
    "/papers/{paper_id}/spans",
    summary="原文定位片段（含 document_version / page_number / quote_sha256）",
)
async def list_paper_spans(
    paper_id: int,
    session: DbSession,
    section: str | None = Query(None, description=SECTION_HELP),
    document_version: str | None = Query(
        None, description="限定某个解析版本；不传则返回该论文全部版本的片段"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """返回可定位片段，每条附 ``verify_span`` 结论（哈希优先）。

    ``verification_summary`` 统计全部匹配片段的三种 verdict，
    ``valid`` 需全文缓存命中（偏移可校验），缓存缺失时退化为 ``valid_by_hash``。
    """
    normalized_section = _normalize_section(section)
    repository = SqlDocumentRepository(session)
    await _require_paper(repository, paper_id)

    documents = await repository.list_documents(paper_id)
    summary = summarize_documents(documents)

    spans = await repository.list_spans(
        paper_id, document_version=document_version, section=normalized_section
    )
    total = len(spans)
    start = (page - 1) * page_size

    # 每个 document_version 只读一次全文缓存，避免 N 次磁盘读
    cache = default_text_cache()
    texts: dict[str, str | None] = {}

    def _text_for(version: str) -> str | None:
        if version not in texts:
            texts[version] = cache.get(paper_id, version)
        return texts[version]

    verdicts = {"valid": 0, "valid_by_hash": 0, "invalid": 0}
    verified: list[tuple[Any, dict[str, Any]]] = []
    for span in spans:
        verification = verify_span(span, _text_for(span.document_version))
        verdicts[verification["verdict"]] = verdicts.get(verification["verdict"], 0) + 1
        verified.append((span, verification))

    items = [
        span.to_api_dict(verification=verification)
        for span, verification in verified[start : start + page_size]
    ]

    text_available = any(text is not None for text in texts.values())
    logger.info(
        "list_spans paper_id=%s section=%s total=%d returned=%d verdicts=%s",
        paper_id,
        normalized_section,
        total,
        len(items),
        verdicts,
    )
    return {
        "paper_id": paper_id,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "section": normalized_section,
        "document_version": document_version,
        "document_versions": sorted({span.document_version for span in spans}),
        "verification_summary": verdicts,
        "fulltext_cache_available": text_available,
        "coverage_note": summary.get("coverage_note"),
        "spans_allowed": bool(summary.get("spans_allowed")),
        "evidence_scope": summary.get("evidence_scope"),
        "note": (
            "verdict 判定顺序为哈希优先；全文缓存缺失时 offset_match=null 并退化为 "
            "valid_by_hash，不伪报 valid"
        ),
    }


# --------------------------------------------------------------------------------------
# POST /papers/{id}/parse（长任务，owner 面）
# --------------------------------------------------------------------------------------
class ParseRequest(BaseModel):
    """``POST /papers/{id}/parse`` 请求体（附录 B.1）。"""

    force: bool = Field(
        default=False,
        description="true=忽略已有记录强制重新抓取解析（内容不变则复用同一 document_version）",
    )


@router.post(
    "/papers/{paper_id}/parse",
    summary="触发全文解析（长任务，owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_parse(
    paper_id: int,
    session: DbSession,
    body: ParseRequest | None = None,
) -> dict[str, Any]:
    """后台触发 ``DocumentStore.ensure_fulltext(paper_id, force=...)``，返回 ``task_id``。

    长任务口径（contracts.api_contract.long_task）：进度不进本响应体；
    解析结果以 ``GET /papers/{id}/documents`` 为准，失败会如实写
    ``parse_status`` / ``parse_error``，不会静默标成已解析。
    """
    request = body or ParseRequest()
    repository = SqlDocumentRepository(session)
    await _require_paper(repository, paper_id)

    submitted = parse_fulltext.submit_parse(paper_id=paper_id, force=request.force)
    logger.info(
        "parse_task_submitted task_id=%s paper_id=%s force=%s",
        submitted["task_id"],
        paper_id,
        request.force,
    )
    payload = {key: value for key, value in submitted.items() if key != "thread"}
    payload["poll_url"] = f"/api/v1/papers/{paper_id}/parse-jobs/{submitted['task_id']}"
    payload["documents_url"] = f"/api/v1/papers/{paper_id}/documents"
    payload["paper_id"] = paper_id
    payload["force"] = bool(request.force)
    payload["progress_channel"] = "SSE /api/v1/stream/{project_id}"
    payload["note"] = (
        "解析在后台执行；本响应不含任何解析结果，"
        "禁止把未完成的解析当作已完成（结果见 documents_url）"
    )
    return payload


@router.get("/papers/{paper_id}/parse-jobs/{task_id}", summary="查询解析任务状态")
async def parse_job_status(paper_id: int, task_id: str, session: DbSession) -> dict[str, Any]:
    repository = SqlDocumentRepository(session)
    await _require_paper(repository, paper_id)

    task = parse_fulltext.get_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到解析任务 {task_id}（本进程重启后任务登记会丢失）",
        )
    if int(task.get("paper_id") or 0) != int(paper_id):
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"解析任务 {task_id} 不属于论文 {paper_id}",
        )
    return task


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ParseRequest",
    "router",
]
