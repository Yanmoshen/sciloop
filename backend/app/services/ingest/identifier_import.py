# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""标识符导入：DOI / arXiv ID → 复用 WP03 身份映射落库。

``app/api/v1/imports.py`` 只依赖本模块的四个符号（冻结接口，勿改签名）::

    classify(raw) -> (kind, normalized) | None
    rejection_reason(raw) -> str
    await import_one(raw, project_id=None) -> dict
    await import_many(values, project_id=None) -> list[dict]

链路（每一步都可审计）
----------------------

1. ``classify`` 把输入归一成 ``("arxiv"|"doi", normalized)``；
   **识别不了一律不发任何网络请求**，由端点直接进 ``rejected``；
2. ``import_one`` 先按标识查 ``paper_identities``：**命中即复用并短路**
   （不重复取数、不新建行）——这是"重复导入必须是 ``reused``"的第一道保证；
3. 未命中才取数：

   - ``arxiv``：走 :mod:`app.services.paper_source.arxiv_client`
     （``SOURCE_NAME`` / ``ARXIV_QPS`` / ``parse_feed``）＋该源共享的
     :func:`app.services.paper_source.cache.get_source_http`（同一份限流与缓存）；
     实测 arXiv 在短时间内多次检索会返回**空响应体的 406**（限流信号，冷却后恢复），
     故本次调用把 406 并入重试集；仍失败则降级 OpenAlex
     （``10.48550/arxiv.<id>`` 精确对应同一篇，不会张冠李戴）；
   - ``doi``：先 ``s2_client.S2Client.get_paper``；``S2Unavailable``（缺 key / 限流 /
     5xx / 网络故障）如实降级到 ``openalex_client.OpenAlexClient.get_work``；
   - 两个源都取不到 → ``status="failed"`` + 可读 ``reason``
     （如 ``s2_429_openalex_miss`` / ``arxiv_http_406_openalex_miss`` / ``not_found``），
     **绝不编造字段**；

4. 取到的真实元数据交给 ``identity.upsert_paper``（WP03）做「命中复用 / 未命中新建」，
   再按 :func:`source_records.record_fields_for_fetch` 逐字段写 ``paper_source_records``
   （``request_url`` / ``http_status`` 都来自**真实响应**，不含猜测值）；
5. ``import_many`` 并发度 ≤2，单条异常被 try/except 兜住，不影响批内其它条目，
   返回与入参**等长且同序**的列表。

已知边界（如实披露，不美化）
----------------------------

- ``arxiv_client.search()`` **无法按 ID 精确取单篇**：它把 ``query`` 拼成
  ``all:"2409.12345"``，实测 arXiv 该检索式 ``totalResults=0``
  （而 ``id_list=2409.12345`` 返回 1 条）。因此这里复用该模块的公开解析器
  ``parse_feed`` 与共享 HTTP 客户端，自行用官方 ``id_list`` 参数取单篇——
  **没有改动 arxiv_client**，也**没有用关键词检索结果冒充 ID 命中**。
- 取数**失败**时库里没有对应 ``papers`` 行，而 ``paper_source_records.paper_id``
  是 NOT NULL 外键，故失败条目不写留痕（与 ``source_records`` 的
  "只有真的发出了 HTTP 请求且有落点才写记录"口径一致）；失败原因只出现在
  任务项的 ``reason`` 里。
- 本模块**不写** ``papers.raw['upload']``：那是 PDF 上传导入的键，
  ``GET /papers/imports`` 按它聚合；标识符导入的痕迹写在
  ``papers.raw['identifier_import']``，避免污染上传历史。
- 本模块没有回放通路（不读任何预存 fixtures），因此 ``raw`` 里的
  ``is_replay`` 恒为 ``false``；HTTP 缓存命中由 ``from_cache`` 如实标注。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.paper import Paper
from app.services.ingest.jobs import ITEM_CREATED, ITEM_FAILED, ITEM_REUSED
from app.services.paper_source import arxiv_client, identity, openalex_client, s2_client
from app.services.paper_source import source_records as sr
from app.services.paper_source.cache import (
    RETRYABLE_STATUSES,
    HttpResult,
    clean_env_value,
    get_source_http,
)

logger = logging.getLogger("sciloop.ingest.identifier_import")

#: 两个 DOI 来源的 source 名（取自客户端模块，避免各写一份字面量）
S2_SOURCE = s2_client.SOURCE_NAME
OPENALEX_SOURCE = openalex_client.SOURCE_NAME

#: classify 的 kind 取值（契约冻结）
KIND_ARXIV = "arxiv"
KIND_DOI = "doi"

#: 单条标识符长度上限（与 ``api/v1/imports.py`` 的 MAX_IDENTIFIER_CHARS 同口径）
MAX_IDENTIFIER_CHARS = 512
#: 批内并发度上限（契约：≤2）
MAX_CONCURRENCY = 2

REASON_EMPTY = "empty"
REASON_TOO_LONG = "too_long"
REASON_UNRECOGNIZED = "unrecognized_identifier"
REASON_NOT_FOUND = "not_found"
REASON_DB_UNAVAILABLE = "database_unavailable"

#: arXiv 官方查询端点（与 arxiv_client 的默认值一致，仅在其未配置时兜底）
ARXIV_API_BASE_DEFAULT = "https://export.arxiv.org/api/query"

#: arXiv 在短时间内连续检索时会返回**空响应体的 406**（实测，见交付报告）：
#: 把它并入该源的重试集（仅在本次调用生效，不影响 ``arxiv_client.search`` 的口径）。
ARXIV_RETRY_STATUSES = frozenset({*RETRYABLE_STATUSES, 406})

#: arXiv ID 主体：新式 ``2409.12345`` 或旧式 ``cs.AI/0701001``，可选 ``vN`` 后缀
#: （与 arxiv_client._ARXIV_ID_RE 同形，但这里要求**整串匹配**，避免误收正文片段）
_ARXIV_CORE = r"(?P<id>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Za-z]{2})?/\d{7})(?P<version>v\d+)?"
_ARXIV_BARE_RE = re.compile(rf"^{_ARXIV_CORE}$", re.IGNORECASE)
#: 允许 ``arXiv:`` / ``arXiv#`` / ``arxiv.org/abs|pdf/`` 前缀（scheme 可省）
_ARXIV_PREFIX_RE = re.compile(
    r"^(?:arxiv\s*[:#]\s*|(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/)",
    re.IGNORECASE,
)
_ARXIV_PDF_SUFFIX_RE = re.compile(r"\.pdf$", re.IGNORECASE)

#: DOI：``10.<registrant>/<suffix>``（registrant 4~9 位数字，与 DOI 手册一致）
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_PREFIX_RE = re.compile(
    r"^(?:https?://)?(?:dx\.|www\.)?doi\.org/|^doi:\s*",
    re.IGNORECASE,
)
#: DOI 结尾常见的正文标点（从文本里复制粘贴时带上），归一阶段剥掉
_DOI_TRAILING_CHARS = ".,;:)]}\"'"


# --------------------------------------------------------------------------------------
# classify / rejection_reason（纯函数，不发网络请求）
# --------------------------------------------------------------------------------------
def _as_text(raw: Any) -> str:
    return str(raw).strip() if raw is not None else ""


def _classify_arxiv(text: str) -> str | None:
    """``arXiv:2409.12345v2`` / ``https://arxiv.org/pdf/2409.12345.pdf`` → ``2409.12345v2``。"""
    candidate = _ARXIV_PREFIX_RE.sub("", text, count=1)
    candidate = _ARXIV_PDF_SUFFIX_RE.sub("", candidate)
    match = _ARXIV_BARE_RE.match(candidate)
    if match is None:
        return None
    # 契约：arxiv 的 normalized **保留 vN**（去版本由 identity.normalize_arxiv_id 负责）
    return f"{match.group('id')}{match.group('version') or ''}"


def _classify_doi(text: str) -> str | None:
    """``https://doi.org/10.1145/x`` / ``doi:10.1145/x`` → ``10.1145/x``。"""
    candidate = _DOI_PREFIX_RE.sub("", text, count=1).strip()
    candidate = candidate.rstrip(_DOI_TRAILING_CHARS)
    if not _DOI_RE.match(candidate):
        return None
    return candidate


def classify(raw: str) -> tuple[str, str] | None:
    """把输入归一为 ``(kind, normalized)``；无法识别返回 ``None``（kind ∈ {arxiv, doi}）。

    支持形态（实测通过，见 ``tests/.reports/import_identifiers.json``）::

        arxiv  2409.12345 / 2409.12345v2 / arXiv:2409.12345v2 / arXiv#2409.12345
               https://arxiv.org/abs/2409.12345 / http://export.arxiv.org/abs/2409.12345
               https://arxiv.org/pdf/2409.12345v1.pdf / cs.AI/0701001
        doi    10.1145/3368089.3409711 / doi:10.1145/3368089.3409711
               https://doi.org/10.1145/3368089.3409711 / http://dx.doi.org/10.1145/x
    空串、超长（>512）、以及其它一切写法（含裸标题、裸 URL）都不识别。
    """
    text = _as_text(raw)
    if not text or len(text) > MAX_IDENTIFIER_CHARS:
        return None
    arxiv_id = _classify_arxiv(text)
    if arxiv_id:
        return KIND_ARXIV, arxiv_id
    doi = _classify_doi(text)
    if doi:
        return KIND_DOI, doi
    return None


def rejection_reason(raw: str) -> str:
    """无法识别时返回稳定的机器可读 code（``empty`` / ``too_long`` / ``unrecognized_identifier``）。"""
    text = _as_text(raw)
    if not text:
        return REASON_EMPTY
    if len(text) > MAX_IDENTIFIER_CHARS:
        return REASON_TOO_LONG
    return REASON_UNRECOGNIZED


# --------------------------------------------------------------------------------------
# 结果项
# --------------------------------------------------------------------------------------
def _item(
    raw: str,
    status: str,
    *,
    paper_id: int | None = None,
    reason: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """契约形状：``{input, status, paper_id, reason, source}``。"""
    return {
        "input": raw,
        "status": status,
        "paper_id": paper_id,
        "reason": reason,
        "source": source,
    }


def _failed(raw: str, reason: str, *, source: str | None = None) -> dict[str, Any]:
    return _item(raw, ITEM_FAILED, reason=reason, source=source)


def _open_session() -> Session | None:
    """同步会话（与 ``pdf_import`` 同口径：后台线程里跑同步 Session）。"""
    from app.db.session import SessionLocal

    if SessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        return None
    return SessionLocal()


def _stored_source(session: Session, paper_id: int) -> str | None:
    """命中复用时回报**库里那篇论文真实的 source**（不冒充本次取数的来源）。"""
    return session.execute(
        select(Paper.source).where(Paper.id == int(paper_id))
    ).scalar_one_or_none()


def _trace(kind: str, raw: str, normalized: str, *, source: str, project_id: int | None) -> dict:
    """``papers.raw['identifier_import']`` 的留痕（可选字段一律来自真实响应）。"""
    return {
        "kind": kind,
        "input": raw,
        "normalized": normalized,
        "source": source,
        "project_id": project_id,
        "imported_at": datetime.now(UTC).isoformat(),
        "is_replay": False,
    }


def _persist(
    session: Session,
    *,
    raw: str,
    kind: str,
    normalized: str,
    meta: Mapping[str, Any],
    fields: Mapping[str, Any],
    record_source: str,
    request_url: str | None,
    http_status: int | None,
    project_id: int | None,
) -> dict[str, Any]:
    """upsert（WP03）＋逐字段留痕（WP03）＋提交；失败如实返回 ``failed``。"""
    source = str(meta.get("source") or record_source)
    try:
        result = identity.upsert_paper(session, meta)
        sr.record_fields_for_fetch(
            session,
            paper_id=int(result.paper_id),
            source=record_source,
            fields=fields,
            request_url=request_url,
            http_status=http_status,
        )
        if result.created and fields.get("citation_count") is None:
            # 本次没取到引用数（如 arXiv 不提供）→ 显式 NULL，
            # 避免 DB DEFAULT 0 被读成"零引用"（与 fetch_papers 同口径）
            identity.force_null(session, int(result.paper_id), "citation_count")
        session.commit()
    except Exception as exc:  # noqa: BLE001 - 入库失败必须如实返回，绝不静默成功
        session.rollback()
        logger.exception("identifier_upsert_failed raw=%r source=%s", raw, source)
        return _failed(raw, f"persist_failed: {type(exc).__name__}", source=source)

    status_value = ITEM_CREATED if result.created else ITEM_REUSED
    logger.info(
        "identifier_imported raw=%r kind=%s normalized=%s source=%s paper_id=%s status=%s",
        raw,
        kind,
        normalized,
        source,
        result.paper_id,
        status_value,
    )
    return _item(raw, status_value, paper_id=int(result.paper_id), source=source)


def _http_failure_code(source: str, result: HttpResult) -> str:
    """真实 HTTP 结果 → 可读失败 code（不做任何猜测）。"""
    if result.status_code is not None:
        return f"{source}_http_{result.status_code}"
    if result.error_kind:
        return f"{source}_{result.error_kind}"
    return f"{source}_unavailable"


# --------------------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------------------
async def _fetch_arxiv(arxiv_id: str) -> tuple[arxiv_client.ArxivPaper | None, str | None]:
    """按官方 ``id_list`` 取单篇；返回 ``(paper | None, 失败 code)``。"""
    base = clean_env_value(get_settings().arxiv_api_base) or ARXIV_API_BASE_DEFAULT
    client = get_source_http(arxiv_client.SOURCE_NAME, qps=arxiv_client.ARXIV_QPS)
    result = await client.get_json(
        base,
        {"id_list": arxiv_id, "start": 0, "max_results": 1},
        use_cache=True,
        expect_json=False,
        retry_statuses=ARXIV_RETRY_STATUSES,
    )
    if not result.ok:
        logger.warning("arxiv_id_fetch_failed id=%s result=%s", arxiv_id, result.to_dict())
        return None, _http_failure_code(arxiv_client.SOURCE_NAME, result)
    try:
        papers, _meta = arxiv_client.parse_feed(
            result.text, request_url=result.request_url, http_status=result.status_code
        )
    except ValueError as exc:
        logger.warning("arxiv_id_parse_failed id=%s err=%s", arxiv_id, exc)
        return None, "arxiv_parse_error"
    if not papers:
        logger.info("arxiv_id_not_found id=%s url=%s", arxiv_id, result.request_url)
        return None, "arxiv_not_found"
    return papers[0], None


async def _fetch_openalex_arxiv(arxiv_id: str) -> tuple[Any | None, str | None]:
    """arXiv 不可用时的兜底：OpenAlex 用 ``10.48550/arxiv.<id>`` 精确取同一篇。"""
    try:
        work = await openalex_client.OpenAlexClient().get_work(arxiv_id=arxiv_id)
    except openalex_client.OpenAlexUnavailable as exc:
        logger.warning("openalex_arxiv_unavailable id=%s reason=%s", arxiv_id, exc.reason)
        return None, exc.reason
    if work is None:
        return None, None
    if not work.title:
        return None, "openalex_missing_title"
    return work, None


def _arxiv_failure_code(arxiv_code: str | None, openalex_code: str | None) -> str:
    """arXiv 取不到时的可读 code（两个源都查无此篇 → ``not_found``）。"""
    if arxiv_code == "arxiv_not_found" and openalex_code is None:
        return REASON_NOT_FOUND
    return f"{arxiv_code or 'arxiv_miss'}_openalex_{openalex_code or 'miss'}"


def _arxiv_meta(
    paper: arxiv_client.ArxivPaper, *, raw: str, arxiv_id: str, project_id: int | None
) -> dict[str, Any]:
    meta = paper.to_meta()
    meta["raw"] = paper.raw_payload()
    meta["raw_extra"] = {
        "identifier_import": _trace(
            KIND_ARXIV, raw, arxiv_id, source=arxiv_client.SOURCE_NAME, project_id=project_id
        )
    }
    return meta


def _arxiv_fields(paper: arxiv_client.ArxivPaper) -> dict[str, Any]:
    return {
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors or None,
        "published_at": paper.published,
        "updated_at_src": paper.updated,
        "pdf_url": paper.pdf_url,
        "comment": paper.comment,
        "doi": paper.doi,
        "primary_category": paper.primary_category,
    }


async def _import_arxiv(raw: str, arxiv_id: str, *, project_id: int | None) -> dict[str, Any]:
    session = _open_session()
    if session is None:  # pragma: no cover - 部署期驱动缺失
        return _failed(raw, REASON_DB_UNAVAILABLE, source=arxiv_client.SOURCE_NAME)
    try:
        # 1) 身份命中即复用（不重复取数，保证重复导入恒为 reused）
        existing = identity.resolve_paper_by_identifiers(
            session, {identity.ID_ARXIV: arxiv_id}
        )
        if existing is not None:
            logger.info("identifier_reused raw=%r arxiv=%s paper_id=%s", raw, arxiv_id, existing)
            return _item(
                raw,
                ITEM_REUSED,
                paper_id=int(existing),
                source=_stored_source(session, int(existing)),
            )

        # 2) 未命中 → 真实取数（arXiv 自身元数据是权威源）
        paper, arxiv_code = await _fetch_arxiv(arxiv_id)
        if paper is not None:
            if not paper.title:
                return _failed(raw, "missing_title_from_source", source=arxiv_client.SOURCE_NAME)
            return _persist(
                session,
                raw=raw,
                kind=KIND_ARXIV,
                normalized=arxiv_id,
                meta=_arxiv_meta(paper, raw=raw, arxiv_id=arxiv_id, project_id=project_id),
                fields=_arxiv_fields(paper),
                record_source=arxiv_client.SOURCE_NAME,
                request_url=paper.request_url,
                http_status=paper.http_status,
                project_id=project_id,
            )

        # 3) arXiv 不可用/查无（实测会返回空响应体 406 限流）→ 降级 OpenAlex
        work, openalex_code = await _fetch_openalex_arxiv(arxiv_id)
        if work is not None:
            meta = work.to_meta()
            meta["raw"] = work.raw_payload()
            meta["raw_extra"] = {
                "identifier_import": {
                    **_trace(
                        KIND_ARXIV,
                        raw,
                        arxiv_id,
                        source=OPENALEX_SOURCE,
                        project_id=project_id,
                    ),
                    "arxiv_failure": arxiv_code,
                }
            }
            return _persist(
                session,
                raw=raw,
                kind=KIND_ARXIV,
                normalized=arxiv_id,
                meta=meta,
                fields={
                    "title": work.title,
                    "venue": work.venue,
                    "published_at": work.publication_date,
                    "citation_count": work.cited_by_count,
                    "doi": work.doi,
                    "institutions": work.institutions or None,
                    "counts_by_year": work.counts_by_year or None,
                },
                record_source=OPENALEX_SOURCE,
                request_url=work.request_url,
                http_status=work.http_status,
                project_id=project_id,
            )

        # 4) 两个源都取不到 → 如实失败（不建行、不编造字段）
        code = _arxiv_failure_code(arxiv_code, openalex_code)
        logger.info("arxiv_not_imported raw=%r arxiv=%s code=%s", raw, arxiv_id, code)
        return _failed(raw, code, source=arxiv_client.SOURCE_NAME)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# DOI：S2 → 降级 OpenAlex
# --------------------------------------------------------------------------------------
def _s2_code(exc: s2_client.S2Unavailable) -> str:
    """S2 异常 → code；429（缺 key 或真限流）统一标 ``s2_429``。"""
    if exc.status_code == 429:
        return "s2_429"
    return f"s2_{exc.reason}"


def _doi_failure_code(s2_code: str | None, openalex_code: str | None) -> str:
    """两个源都没给出论文时的可读 code（``not_found`` / ``s2_429_openalex_miss`` …）。"""
    if s2_code is None and openalex_code is None:
        return REASON_NOT_FOUND
    return f"{s2_code or 's2_miss'}_{openalex_code or 'openalex_miss'}"


async def _fetch_s2_doi(doi: str) -> tuple[Any | None, str | None]:
    """S2 取单篇；不可用返回 ``(None, code)``（由调用方降级 OpenAlex）。"""
    try:
        paper = await s2_client.S2Client().get_paper(doi=doi)
    except s2_client.S2Unavailable as exc:
        logger.warning("s2_doi_unavailable doi=%s detail=%s", doi, exc.to_dict())
        return None, _s2_code(exc)
    if paper is None:
        return None, None  # 请求成功但 S2 无此 DOI
    if not paper.title:
        return None, "s2_missing_title"
    return paper, None


async def _fetch_openalex_doi(doi: str) -> tuple[Any | None, str | None]:
    """OpenAlex 兜底取单篇；不可用返回 ``(None, code)``。"""
    try:
        work = await openalex_client.OpenAlexClient().get_work(doi=doi)
    except openalex_client.OpenAlexUnavailable as exc:
        logger.warning("openalex_doi_unavailable doi=%s reason=%s", doi, exc.reason)
        return None, exc.reason
    if work is None:
        return None, None
    if not work.title:
        return None, "openalex_missing_title"
    return work, None


async def _import_doi(raw: str, doi: str, *, project_id: int | None) -> dict[str, Any]:
    session = _open_session()
    if session is None:  # pragma: no cover - 部署期驱动缺失
        return _failed(raw, REASON_DB_UNAVAILABLE)
    try:
        existing = identity.resolve_paper_by_identifiers(session, {identity.ID_DOI: doi})
        if existing is not None:
            logger.info("identifier_reused raw=%r doi=%s paper_id=%s", raw, doi, existing)
            return _item(
                raw,
                ITEM_REUSED,
                paper_id=int(existing),
                source=_stored_source(session, int(existing)),
            )

        # 1) Semantic Scholar 优先
        s2_paper, s2_code = await _fetch_s2_doi(doi)
        if s2_paper is not None:
            meta = s2_paper.to_meta()
            meta["raw"] = s2_paper.as_dict()
            meta["raw_extra"] = {
                "identifier_import": _trace(
                    KIND_DOI, raw, doi, source=S2_SOURCE, project_id=project_id
                )
            }
            return _persist(
                session,
                raw=raw,
                kind=KIND_DOI,
                normalized=doi,
                meta=meta,
                fields={
                    "title": s2_paper.title,
                    "abstract": s2_paper.abstract,
                    "venue": s2_paper.venue,
                    "published_at": s2_paper.publication_date,
                    "citation_count": s2_paper.citation_count,
                    "external_ids": s2_paper.external_ids or None,
                    "open_access_pdf": s2_paper.open_access_pdf,
                },
                record_source=S2_SOURCE,
                request_url=s2_paper.request_url,
                http_status=s2_paper.http_status,
                project_id=project_id,
            )

        # 2) 降级 OpenAlex（S2 缺 key / 429 / 5xx / 网络故障，或 S2 无此 DOI）
        work, openalex_code = await _fetch_openalex_doi(doi)
        if work is not None:
            meta = work.to_meta()
            meta["raw"] = work.raw_payload()
            meta["raw_extra"] = {
                "identifier_import": _trace(
                    KIND_DOI, raw, doi, source=OPENALEX_SOURCE, project_id=project_id
                )
            }
            return _persist(
                session,
                raw=raw,
                kind=KIND_DOI,
                normalized=doi,
                meta=meta,
                fields={
                    "title": work.title,
                    "venue": work.venue,
                    "published_at": work.publication_date,
                    "citation_count": work.cited_by_count,
                    "doi": work.doi,
                    "institutions": work.institutions or None,
                    "counts_by_year": work.counts_by_year or None,
                },
                record_source=OPENALEX_SOURCE,
                request_url=work.request_url,
                http_status=work.http_status,
                project_id=project_id,
            )

        # 3) 两个源都取不到 → 如实失败（不建行、不编造字段）
        code = _doi_failure_code(s2_code, openalex_code)
        logger.info("doi_not_imported raw=%r doi=%s code=%s", raw, doi, code)
        return _failed(raw, code)
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------------------
async def import_one(raw: str, *, project_id: int | None = None) -> dict:
    """导入单条标识符。

    → ``{"input", "status": created|reused|failed, "paper_id", "reason", "source"}``
    """
    text = _as_text(raw)
    classified = classify(text)
    if classified is None:
        return _failed(text, rejection_reason(text))
    kind, normalized = classified
    try:
        if kind == KIND_ARXIV:
            return await _import_arxiv(text, normalized, project_id=project_id)
        return await _import_doi(text, normalized, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 - 单条异常不得冒泡成任务级失败
        logger.exception("identifier_single_failed raw=%r", text)
        return _failed(text, f"unexpected_error: {type(exc).__name__}")


async def import_many(values: list[str], *, project_id: int | None = None) -> list[dict]:
    """批量导入：并发度 ≤2，返回与入参**等长且同序**的结果列表。

    无法识别的条目也占位返回 ``status="failed"`` + ``reason=rejection_reason(...)``；
    单条抛错被兜成 ``failed``，不影响批内其它条目。
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _run(item: Any) -> dict[str, Any]:
        text = _as_text(item)
        async with semaphore:
            try:
                return await import_one(text, project_id=project_id)
            except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都转成单条失败
                logger.exception("identifier_item_failed raw=%r", text)
                return _failed(text, f"unexpected_error: {type(exc).__name__}: {exc}")

    gathered = await asyncio.gather(*(_run(item) for item in values))
    return list(gathered)


__all__ = [
    "KIND_ARXIV",
    "KIND_DOI",
    "MAX_CONCURRENCY",
    "MAX_IDENTIFIER_CHARS",
    "REASON_EMPTY",
    "REASON_NOT_FOUND",
    "REASON_TOO_LONG",
    "REASON_UNRECOGNIZED",
    "classify",
    "import_many",
    "import_one",
    "rejection_reason",
]
