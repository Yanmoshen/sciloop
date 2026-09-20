# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文取数端点（WP03-T8）。

实现（``/papers/feed`` 归 WP04 的 ``feed.py``，本模块**不碰**它）
----------------------------------------------------------------
============================  ==========================================================
``GET  /papers/search``       本地库检索（q/field/limit），统一分页 ``{items,total,page,page_size}``
``GET  /papers/{id}``         论文详情（含身份映射、分数分项、取数来源摘要）
``GET  /papers/{id}/sources`` 该论文的取数留痕（每字段一行，含 request_url/http_status/confidence）
``POST /papers/fetch``        触发一轮多源抓取（长任务：返回 ``task_id``；owner 面写操作）
``GET  /papers/fetch-jobs/{}`` 查询抓取任务状态（配套 POST 的轮询口）
``GET  /sources/health``      四源连通性/最近成功时间（``?probe=true`` 时做真实探活）
============================  ==========================================================

口径
----
- 只读端点遵守「取不到就 null」：分数分项、引用数、venue 等级缺失一律 ``null``，不填默认值。
- 写操作（POST /papers/fetch）在 ``public_demo`` 面一律 403（contracts.forbidden_actions）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import require_owner
from app.db.models.paper import Paper
from app.db.session import SessionLocal
from app.services.paper_source import arxiv_client, github_client, identity, openalex_client
from app.services.paper_source import s2_client as s2_module
from app.services.paper_source import source_records as sr
from app.services.paper_source.cache import cache_stats
from app.tasks.jobs import fetch_papers

logger = logging.getLogger("sciloop.wp03.papers")

router = APIRouter(tags=["papers"])

SOURCE_NAMES: tuple[str, ...] = ("arxiv", "semantic_scholar", "openalex", "github")
MAX_PAGE_SIZE = 100
SEARCH_LIMIT_MAX = 200


# --------------------------------------------------------------------------------------
# 会话依赖（SessionLocal 不可用时给出 503 + 契约错误体，而不是 500）
# --------------------------------------------------------------------------------------
def _session() -> Iterator[Session]:
    if SessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "database_unavailable",
                "message": "数据库会话工厂不可用（DATABASE_URL / psycopg 未就绪）",
                "detail": None,
            },
        )
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(_session)]


# --------------------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------------------
def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def paper_to_item(paper: Paper, *, abstract_limit: int | None = 600) -> dict[str, Any]:
    abstract = paper.abstract
    if abstract_limit is not None and abstract and len(abstract) > abstract_limit:
        abstract = abstract[:abstract_limit].rstrip() + "…"
    return {
        "id": paper.id,
        "source": paper.source,
        "external_id": paper.external_id,
        "doi": paper.doi,
        "title": paper.title,
        "abstract": abstract,
        "authors": paper.authors,
        "published_at": _iso(paper.published_at),
        "updated_at_src": _iso(paper.updated_at_src),
        "venue": paper.venue,
        "venue_source": paper.venue_source,
        "venue_level": paper.venue_level,
        "citation_count": paper.citation_count,
        "citation_velocity": _num(paper.citation_velocity),
        "code_url": paper.code_url,
        "code_heat": _num(paper.code_heat),
        "pdf_url": paper.pdf_url,
        "rank_score": _num(paper.rank_score),
        "rank_breakdown": paper.rank_breakdown,
        "influence_score": _num(paper.influence_score),
        "score_breakdown": paper.score_breakdown,
        "score_coverage": _num(paper.score_coverage),
        "is_parsed": bool(paper.is_parsed) if paper.is_parsed is not None else None,
        "created_at": _iso(paper.created_at),
        "updated_at": _iso(paper.updated_at),
    }


def paper_to_detail(paper: Paper, session: Session, *, include_raw: bool = False) -> dict[str, Any]:
    item = paper_to_item(paper, abstract_limit=None)
    item["identities"] = identity.paper_identity_rows(session, paper.id)
    item["source_summary"] = _source_summary(session, paper.id)
    item["raw_sources"] = sorted(paper.raw.keys()) if isinstance(paper.raw, Mapping) else []
    item["raw"] = paper.raw if include_raw else None
    item["raw_included"] = bool(include_raw)
    return item


def _source_summary(session: Session, paper_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            sr.PaperSourceRecord.source,
            func.count().label("records"),
            func.min(sr.PaperSourceRecord.confidence).label("confidence_min"),
            func.max(sr.PaperSourceRecord.confidence).label("confidence_max"),
            func.max(sr.PaperSourceRecord.fetched_at).label("last_fetched_at"),
        )
        .where(sr.PaperSourceRecord.paper_id == int(paper_id))
        .group_by(sr.PaperSourceRecord.source)
    ).all()
    summary: list[dict[str, Any]] = []
    for row in rows:
        last_status = session.execute(
            select(sr.PaperSourceRecord.http_status)
            .where(
                sr.PaperSourceRecord.paper_id == int(paper_id),
                sr.PaperSourceRecord.source == row.source,
            )
            .order_by(sr.PaperSourceRecord.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        summary.append(
            {
                "source": row.source,
                "source_label": sr.SOURCE_LABELS.get(row.source, row.source),
                "records": int(row.records or 0),
                "confidence": _num(row.confidence_max),
                "confidence_min": _num(row.confidence_min),
                "last_http_status": last_status,
                "last_fetched_at": _iso(row.last_fetched_at),
            }
        )
    return summary


def paginate(
    items: Sequence[Any], total: int, page: int, page_size: int, **extra: Any
) -> dict[str, Any]:
    payload = {"items": list(items), "total": int(total), "page": page, "page_size": page_size}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------------------
# GET /papers/search
# --------------------------------------------------------------------------------------
@router.get("/papers/search", summary="检索本地库中的论文")
def search_papers(
    session: DbSession,
    q: str | None = Query(None, description="标题/摘要关键词（不区分大小写）"),
    field: str | None = Query(None, description="arXiv 分类（如 cs.CL）或 OpenAlex 领域"),
    source: str | None = Query(None, description="来源：arxiv / semantic_scholar / openalex / github"),
    parse_status: str | None = Query(
        None,
        pattern="^(parsed|unparsed)$",
        description="解析状态：parsed=已解析 / unparsed=未解析；非法值 → 422",
    ),
    sort: str = Query(
        "published",
        pattern="^(published|citation)$",
        description="排序：published=发表时间倒序（默认）/ citation=引用数倒序；非法值 → 422",
    ),
    limit: int = Query(20, ge=1, le=SEARCH_LIMIT_MAX),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """本地检索：``q`` 命中标题/摘要，``field`` 命中 arXiv 分类（primary/categories）。

    2026-09-20 补 ``source`` / ``parse_status`` / ``sort`` 三个**全库**参数：
    此前来源、解析状态、排序都只在**当前页 20 条**上做（前端切片），翻页即失效 ——
    用户要在 389 篇里选几篇对比时很容易漏选。下沉到库里后，分页是"筛选结果的分页"。

    口径：缺失字段一律 null，**不做推断**；``citation`` 排序把 null 排最后（不当 0 参与排序）。
    """
    size = int(page_size or limit)
    size = max(1, min(size, MAX_PAGE_SIZE))

    conditions = []
    if q:
        pattern = f"%{q.strip()}%"
        conditions.append(
            or_(Paper.title.ilike(pattern), cast(Paper.abstract, Text).ilike(pattern))
        )
    if field:
        category = field.strip()
        conditions.append(
            or_(
                func.jsonb_extract_path_text(Paper.raw, "arxiv", "primary_category") == category,
                func.jsonb_exists(
                    func.jsonb_extract_path(Paper.raw, "arxiv", "categories"), category
                ),
            )
        )
    if source and source.strip():
        conditions.append(Paper.source == source.strip())
    if parse_status == "parsed":
        conditions.append(Paper.is_parsed.is_(True))
    elif parse_status == "unparsed":
        # 未解析 = 明确 False 或历史 null（两者都不算"已解析"）
        conditions.append(Paper.is_parsed.isnot(True))

    stmt = select(Paper)
    count_stmt = select(func.count()).select_from(Paper)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    if sort == "citation":
        order_by = (Paper.citation_count.desc().nullslast(), Paper.id.desc())
    else:
        order_by = (Paper.published_at.desc().nullslast(), Paper.id.desc())

    total = int(session.execute(count_stmt).scalar_one() or 0)
    rows = (
        session.execute(stmt.order_by(*order_by).offset((page - 1) * size).limit(size))
        .scalars()
        .all()
    )
    return paginate(
        [paper_to_item(paper) for paper in rows],
        total,
        page,
        size,
        query=q,
        field=field,
        limit=limit,
        source="local_db",
        source_filter=source,
        parse_status=parse_status,
        sort=sort,
        note="本地库检索（全库筛选/排序；取数由 POST /papers/fetch 触发）；缺失字段一律 null，不做推断",
    )


# --------------------------------------------------------------------------------------
# POST /papers/fetch（长任务，owner 面）
# --------------------------------------------------------------------------------------
class FetchRequest(BaseModel):
    """``POST /papers/fetch`` 请求体（附录 B.1）。"""

    fields: list[str] | None = Field(default=None, description="arXiv 分类，默认 ARXIV_FIELDS")
    date_from: str | None = Field(default=None, description="增量起点（YYYY-MM-DD）")
    date_to: str | None = Field(
        default=None, description="时间窗终点（YYYY-MM-DD）；与 date_from 合用可做历史窗口回填"
    )
    limit: int | None = Field(default=None, ge=1, le=1000, description="本轮抓取上限")
    sources: list[str] | None = Field(default=None, description="启用哪些富化源")


def _parse_date(value: Any) -> Any:
    return identity.coerce_date(value)


@router.post(
    "/papers/fetch",
    summary="触发多源抓取（长任务）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_fetch(body: FetchRequest | None = None) -> dict[str, Any]:
    """按 ``{fields[], date_from, limit, sources[]}`` 触发一轮抓取，返回 ``task_id``。

    长任务口径（contracts.api_contract.long_task）：进度不进 HTTP 响应体，
    这里返回 ``task_id`` 与轮询地址；执行在后台线程，不阻塞事件循环。
    """
    request = body or FetchRequest()
    sources = [s for s in (request.sources or []) if s]
    if sources:
        unknown = [s for s in sources if s not in fetch_papers.ALL_SOURCES]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "unknown_source",
                    "message": f"不支持的数据源: {', '.join(unknown)}",
                    "detail": {"allowed": list(fetch_papers.ALL_SOURCES)},
                },
            )
    limit_value = int(request.limit or fetch_papers.DEFAULT_LIMIT)

    date_from = _parse_date(request.date_from)
    date_to = _parse_date(request.date_to)
    result = fetch_papers.submit_fetch(
        fields=request.fields or None,
        date_from=date_from,
        date_to=date_to,
        limit=limit_value,
        sources=sources or None,
    )
    logger.info(
        "fetch_task_submitted task_id=%s fields=%s limit=%s sources=%s",
        result["task_id"],
        request.fields,
        limit_value,
        sources or list(fetch_papers.ALL_SOURCES),
    )
    return {
        **{k: v for k, v in result.items() if k != "thread"},
        "poll_url": f"/api/v1/papers/fetch-jobs/{result['task_id']}",
        "progress_channel": "SSE /api/v1/stream/{project_id}",
        "note": "抓取在后台执行；本响应不含任何抓取结果，禁止把未完成的抓取当作已完成",
    }


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    """把「内存任务」与「落盘历史」两种形状归一，供前端历史列表直接渲染。"""
    report = task.get("report") or {}
    counts = dict(report.get("counts") or {}) or dict(task.get("counts") or {})
    by_source = report.get("by_source") or task.get("source_states") or {}
    failed_total = task.get("failed_total")
    if failed_total is None:
        failed_total = sum(int((state or {}).get("failed") or 0) for state in by_source.values())
    progress = task.get("progress") or {}
    started, finished = task.get("started_at"), task.get("finished_at")
    duration = progress.get("elapsed_seconds")
    if duration is None and started and finished:
        try:
            duration = (
                datetime.fromisoformat(str(finished)) - datetime.fromisoformat(str(started))
            ).total_seconds()
        except ValueError:
            duration = None
    return {
        "task_id": task.get("task_id"),
        "job": task.get("job"),
        "status": task.get("status"),
        "stage": task.get("stage"),
        "started_at": started,
        "finished_at": finished,
        "duration_seconds": None if duration is None else round(float(duration), 1),
        "params": task.get("params") or {},
        "counts": counts,
        "failed_total": failed_total,
        "sources": {
            name: {
                "requests": (state or {}).get("requests"),
                "ok": (state or {}).get("ok"),
                "failed": (state or {}).get("failed"),
                "http_status": (state or {}).get("http_status"),
                "last_error": (state or {}).get("last_error"),
            }
            for name, state in (by_source or {}).items()
        },
        "events": task.get("events") or [],
    }


@router.get("/papers/fetch-jobs", summary="同步任务历史（新的在前）")
def fetch_job_list(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    """历史同步任务：内存中的任务 + 落盘快照（容器重启前的记录也能查到）。"""
    tasks = [_task_summary(item) for item in fetch_papers.list_tasks(limit=limit)]
    return {"tasks": tasks, "total": len(tasks)}


@router.get("/papers/fetch-jobs/{task_id}", summary="查询抓取任务状态")
def fetch_job_status(task_id: str) -> dict[str, Any]:
    task = fetch_papers.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "task_not_found",
                "message": f"未找到抓取任务 {task_id}（本进程重启后任务登记会丢失）",
                "detail": None,
            },
        )
    return task


TERMINAL_STATUSES = {"done", "failed", "cancelled"}


def _control_task(task_id: str, *, pause: bool | None = None, cancel: bool | None = None) -> dict[str, Any]:
    """下发暂停/恢复/终止指令并回读任务快照（Owner 面）。"""
    task = fetch_papers.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "task_not_found",
                "message": f"未找到抓取任务 {task_id}（本进程重启后任务登记会丢失）",
                "detail": None,
            },
        )
    if str(task.get("status")) in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "task_already_finished",
                "message": f"任务已处于终态 {task.get('status')}，无需再操作",
                "detail": {"status": task.get("status")},
            },
        )
    updated = fetch_papers.set_task_control(task_id, pause=pause, cancel=cancel)
    fetch_papers.emit_event(
        task_id,
        "warn" if (pause or cancel) else "ok",
        "已收到终止指令，将在当前条目处理完后停止" if cancel
        else ("已暂停（正在处理完当前条目）" if pause else "已恢复执行"),
    )
    return updated or task


@router.post("/papers/fetch-jobs/{task_id}/pause", summary="暂停抓取任务（Owner）")
def pause_fetch_job(task_id: str, _: str = Depends(require_owner)) -> dict[str, Any]:
    return _control_task(task_id, pause=True)


@router.post("/papers/fetch-jobs/{task_id}/resume", summary="恢复抓取任务（Owner）")
def resume_fetch_job(task_id: str, _: str = Depends(require_owner)) -> dict[str, Any]:
    return _control_task(task_id, pause=False)


@router.post("/papers/fetch-jobs/{task_id}/cancel", summary="终止抓取任务（Owner）")
def cancel_fetch_job(task_id: str, _: str = Depends(require_owner)) -> dict[str, Any]:
    return _control_task(task_id, cancel=True)


# --------------------------------------------------------------------------------------
# GET /papers/{id} 与 /papers/{id}/sources
# --------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------
# GET /papers/overview（文献总览页的统计条，公开只读）
# --------------------------------------------------------------------------------------
@router.get("/papers/overview", summary="文献总览统计（论文/解析/卡片/聚合）")
def papers_overview(session: DbSession) -> dict[str, Any]:
    """文献总览页顶部的四项计数 + 定时拉取状态。

    全部为库内真实计数；任何取不到的项**返回 null**（前端显示"未获取"），
    不做估算、不使用默认值填充。
    """
    from datetime import timedelta

    from app.db.models import Aggregation, PaperCard, PaperDocument, PaperSourceRecord

    now = datetime.now(UTC)
    since_7d = now - timedelta(days=7)
    since_24h = now - timedelta(hours=24)

    papers_total = int(session.execute(select(func.count()).select_from(Paper)).scalar_one() or 0)
    papers_new_7d = int(
        session.execute(
            select(func.count()).select_from(Paper).where(Paper.created_at >= since_7d)
        ).scalar_one()
        or 0
    )
    papers_new_24h = int(
        session.execute(
            select(func.count()).select_from(Paper).where(Paper.created_at >= since_24h)
        ).scalar_one()
        or 0
    )

    documents_total = int(
        session.execute(select(func.count()).select_from(PaperDocument)).scalar_one() or 0
    )
    documents_ok = int(
        session.execute(
            select(func.count())
            .select_from(PaperDocument)
            .where(PaperDocument.parse_status == "ok")
        ).scalar_one()
        or 0
    )

    cards_total = int(
        session.execute(select(func.count()).select_from(PaperCard)).scalar_one() or 0
    )
    cards_papers = int(
        session.execute(select(func.count(func.distinct(PaperCard.paper_id)))).scalar_one() or 0
    )

    aggregations_total = int(
        session.execute(select(func.count()).select_from(Aggregation)).scalar_one() or 0
    )

    last_sync_at = session.execute(select(func.max(PaperSourceRecord.fetched_at))).scalar_one_or_none()

    return {
        "papers_total": papers_total,
        "papers_new_7d": papers_new_7d,
        "papers_new_24h": papers_new_24h,
        "documents_total": documents_total,
        "documents_ok": documents_ok,
        "cards_total": cards_total,
        "cards_papers": cards_papers,
        "aggregations_total": aggregations_total,
        "last_sync_at": _iso(last_sync_at),
        "checked_at": now.isoformat(),
        "note": "全部为库内真实计数；null 表示取不到该统计，不做估算",
    }


@router.get("/papers/{paper_id}", summary="论文详情")
def get_paper(
    paper_id: int,
    session: DbSession,
    include_raw: bool = Query(False, description="是否返回 papers.raw 原始响应（体积较大）"),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "paper_not_found",
                "message": f"论文 {paper_id} 不存在",
                "detail": None,
            },
        )
    return paper_to_detail(paper, session, include_raw=include_raw)


@router.get("/papers/{paper_id}/sources", summary="该论文的取数留痕")
def get_paper_sources(
    paper_id: int,
    session: DbSession,
    source: str | None = Query(None, description="按来源过滤"),
    field_name: str | None = Query(None, description="按字段过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    paper = session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "paper_not_found",
                "message": f"论文 {paper_id} 不存在",
                "detail": None,
            },
        )
    rows, total = sr.list_records(
        session, paper_id, source=source, field_name=field_name, page=page, page_size=page_size
    )
    return paginate(
        [sr.record_to_dict(row) for row in rows],
        total,
        page,
        page_size,
        paper_id=paper_id,
        summary=_source_summary(session, paper_id),
        confidence_by_source={k: float(v) for k, v in sr.CONFIDENCE_BY_SOURCE.items()},
    )


# --------------------------------------------------------------------------------------
# GET /sources/health
# --------------------------------------------------------------------------------------
@dataclass
class ProbeOutcome:
    ok: bool
    http_status: int | None
    detail: str | None
    request_url: str | None = None
    latency_ms: float | None = None
    extra: dict[str, Any] | None = None


async def _probe_arxiv() -> ProbeOutcome:
    papers, results = await arxiv_client.iter_papers(limit=1, page_size=1)
    result = results[0] if results else None
    if result is None:
        return ProbeOutcome(ok=False, http_status=None, detail="no_request_result")
    return ProbeOutcome(
        ok=result.ok and bool(papers),
        http_status=result.status_code,
        detail=None if result.ok and papers else (result.error or "empty_feed"),
        request_url=result.request_url,
        latency_ms=result.elapsed_ms,
        extra={"entries": len(papers), "from_cache": result.from_cache},
    )


async def _probe_s2() -> ProbeOutcome:
    client = s2_module.S2Client()
    try:
        item = await client.get_paper(arxiv_id="1706.03762", use_cache=False)
    except s2_module.S2Unavailable as exc:
        return ProbeOutcome(
            ok=False,
            http_status=exc.status_code,
            detail=f"{exc.reason}: {exc.detail}" if exc.detail else exc.reason,
            request_url=exc.request_url,
            extra={"attempts": exc.attempts, "configured": client.configured},
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeOutcome(ok=False, http_status=None, detail=f"{type(exc).__name__}: {exc}")
    if item is None:
        return ProbeOutcome(
            ok=True,
            http_status=404,
            detail="paper_not_found",
            extra={"configured": client.configured},
        )
    return ProbeOutcome(
        ok=True,
        http_status=item.http_status,
        detail=None,
        request_url=item.request_url,
        extra={"configured": client.configured, "cited_by": item.citation_count},
    )


async def _probe_openalex() -> ProbeOutcome:
    client = openalex_client.OpenAlexClient()
    # 用一篇**已确认存在于 OpenAlex**的 arXiv DOI 探活（1706.03762 是 2017 年论文，
    # 没有 arXiv-DOI，会稳定 404，不适合当探活样本）
    probe_doi = openalex_client.arxiv_doi("2507.01234")
    try:
        work = await client.get_work(doi=probe_doi, use_cache=False)
    except openalex_client.OpenAlexUnavailable as exc:
        return ProbeOutcome(
            ok=False,
            http_status=exc.status_code,
            detail=f"{exc.reason}: {exc.detail}" if exc.detail else exc.reason,
            request_url=exc.request_url,
            extra={"configured": client.configured},
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeOutcome(ok=False, http_status=None, detail=f"{type(exc).__name__}: {exc}")
    if work is None:
        # 404 说明"源是通的、只是没这篇"，不应判成源故障
        last = client.last_lookup
        return ProbeOutcome(
            ok=True,
            http_status=last.get("http_status") or 404,
            detail="probe_work_not_found",
            request_url=last.get("request_url"),
            extra={"configured": client.configured, "probe_doi": probe_doi},
        )
    return ProbeOutcome(
        ok=True,
        http_status=work.http_status,
        detail=None,
        request_url=work.request_url,
        extra={
            "configured": client.configured,
            "cited_by": work.cited_by_count,
            "probe_doi": probe_doi,
        },
    )


async def _probe_github() -> ProbeOutcome:
    client = github_client.GitHubClient()
    repo = await client.get_repo("https://github.com/pytorch/pytorch", use_cache=False)
    if repo is None:
        return ProbeOutcome(ok=False, http_status=None, detail="url_unparsable")
    # 仓库存在（200）即为连通；403/429 才是限流降级
    reachable = repo.http_status == 200
    return ProbeOutcome(
        ok=reachable,
        http_status=repo.http_status,
        detail=repo.error,
        request_url=repo.request_url,
        extra={
            "token_present": client.configured,
            "stargazers_count": repo.stargazers_count,
            "anonymous_quota_hint": None if client.configured else "anonymous_60_per_hour",
        },
    )


_PROBES = {
    "arxiv": _probe_arxiv,
    "semantic_scholar": _probe_s2,
    "openalex": _probe_openalex,
    "github": _probe_github,
}


# 哪些源"必须有凭证"才能正常工作：
# - arxiv：无凭证需求
# - semantic_scholar：**必须带 key**（无 key 官方 429）
# - openalex：**必须带 mailto**
# - github：token 可选（匿名 60 次/小时，缺 token 不算降级）
CREDENTIALS_REQUIRED: dict[str, bool] = {
    "arxiv": False,
    "semantic_scholar": True,
    "openalex": True,
    "github": False,
}


def _configured_flags() -> dict[str, Any]:
    settings = get_settings()
    s2 = s2_module.S2Client()
    openalex = openalex_client.OpenAlexClient()
    github = github_client.GitHubClient()
    return {
        "arxiv": {
            "configured": bool(settings.arxiv_api_base),
            "api_base": settings.arxiv_api_base,
            "credentials_required": CREDENTIALS_REQUIRED["arxiv"],
        },
        "semantic_scholar": {
            "configured": s2.configured,
            # 只暴露"是否配置"，绝不回显密钥本身
            "api_key_present": s2.configured,
            "credentials_required": CREDENTIALS_REQUIRED["semantic_scholar"],
            "qps": s2.qps,
        },
        "openalex": {
            "configured": openalex.configured,
            "mailto_present": openalex.configured,
            "credentials_required": CREDENTIALS_REQUIRED["openalex"],
            "api_base": openalex.base_url,
        },
        "github": {
            "configured": github.configured,
            "token_present": github.configured,
            "credentials_required": CREDENTIALS_REQUIRED["github"],
            "note": "token 可选：匿名 60 次/小时，不影响功能（缺 token 不算降级）",
        },
    }


@router.get("/sources/health", summary="四源连通性与最近成功时间")
async def sources_health(
    session: DbSession,
    probe: bool = Query(False, description="是否做真实探活（会消耗外部配额，默认只用库内留痕）"),
) -> dict[str, Any]:
    """返回 arxiv / semantic_scholar / openalex / github 四源状态。

    - 默认（``probe=false``）：只读 ``paper_source_records`` 的历史留痕（快、零外部调用）；
    - ``probe=true``：各发一次真实请求，给出实时 ``ok / http_status / latency_ms``。
    任一源不可用 → 顶层 ``status='degraded'`` 并列出 ``degraded_sources``。
    """
    stats = sr.source_stats(session)
    configured = _configured_flags()
    payload_sources: dict[str, Any] = {}

    for name in SOURCE_NAMES:
        node = dict(stats.get(name) or {})
        conf = configured.get(name, {})
        node.update(
            {
                "source": name,
                "label": sr.SOURCE_LABELS.get(name, name),
                "configured": bool(conf.get("configured")),
                "config": conf,
                "distinct_papers": sr.distinct_papers_by_source(session, name),
                "confidence": (
                    float(sr.CONFIDENCE_BY_SOURCE[name])
                    if name in sr.CONFIDENCE_BY_SOURCE
                    else None
                ),
                "ok": True,
                "status": "ok",
                "degraded_reason": None,
            }
        )
        credentials_required = bool(
            conf.get("credentials_required", name in ("semantic_scholar", "openalex"))
        )
        node["credentials_required"] = credentials_required
        # 只有"必需凭证缺失"才算降级；GitHub 缺 token 只是匿名限额（仍可用）
        if not node["configured"] and credentials_required:
            node["ok"] = False
            node["status"] = "degraded"
            node["degraded_reason"] = "not_configured"
        last_status = node.get("last_http_status")
        if last_status is not None and int(last_status) >= 400:
            node["ok"] = False
            node["status"] = "degraded"
            node["degraded_reason"] = f"last_http_status={last_status}"
        payload_sources[name] = node

    if probe:
        names = list(SOURCE_NAMES)
        outcomes = await asyncio.gather(
            *(_PROBES[name]() for name in names), return_exceptions=True
        )
        for name, outcome in zip(names, outcomes, strict=True):
            node = payload_sources[name]
            if isinstance(outcome, BaseException):
                node.update(
                    {
                        "probe": {
                            "ok": False,
                            "http_status": None,
                            "detail": f"{type(outcome).__name__}: {outcome}",
                            "request_url": None,
                            "latency_ms": None,
                        }
                    }
                )
                node["ok"] = False
                node["status"] = "degraded"
                node["degraded_reason"] = "probe_failed"
                continue
            probe_payload = asdict(outcome)
            node["probe"] = probe_payload
            # 探活是实时结论，优先于库内历史留痕
            if outcome.ok:
                node["ok"] = True
                node["status"] = "ok"
                node["degraded_reason"] = None
            else:
                node["ok"] = False
                node["status"] = "degraded"
                node["degraded_reason"] = outcome.detail or "probe_failed"

    degraded = [name for name, node in payload_sources.items() if not node["ok"]]
    return {
        "status": "ok" if not degraded else "degraded",
        "degraded_sources": degraded,
        "sources": payload_sources,
        "checked_at": datetime.now(UTC).isoformat(),
        "probe_enabled": bool(probe),
        "cache": cache_stats(),
        "notes": [
            "默认只读库内留痕；probe=true 才会真实探活",
            "semantic_scholar 必须带 key：未配置时官方返回 429，状态会显示 degraded 并降级 OpenAlex",
            "openalex 必须带 mailto：未配置时状态 degraded 且不发起请求",
            "github 的 token 可选：匿名 60 次/小时，缺 token 只影响额度不影响可用性",
            "credentials_required=false 的源即使未配置凭证也不会被判为 degraded",
        ],
    }



__all__ = [
    "cancel_fetch_job",
    "pause_fetch_job",
    "resume_fetch_job",
    "MAX_PAGE_SIZE",
    "SOURCE_NAMES",
    "get_paper",
    "get_paper_sources",
    "paper_to_detail",
    "paper_to_item",
    "papers_overview",
    "router",
    "search_papers",
    "sources_health",
    "trigger_fetch",
]
