# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""论文库三视图端点（WP04-T6）：``GET /papers/feed``。

口径分离（计划书 §2.7.5，**硬约束**）
----------------------------------
==============  ==========================  ====================================
视图            排序依据                     说明
==============  ==========================  ====================================
recommended     ``rank_score`` DESC（四维）  主口径
influence       ``influence_score`` DESC     辅助展示分，明确标注"不用于默认排序"
latest          ``published_at`` DESC        **不按 influence_score 排序**
==============  ==========================  ====================================

每条返回项都带 ``rank_breakdown`` / ``score_breakdown``（每项含 value/source/confidence）、
``llm_novelty_tag``（带不确定性区间，不稳定时只给区间）与 ``fulltext``（parse_status/coverage/
document_version）；缺失一律 null，不编造。
"""

from __future__ import annotations

import importlib
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import date
from functools import lru_cache
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Text, cast, create_engine, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models.paper import Paper, PaperDocument, PaperFeedSnapshot
from app.services.paper_source import influence_score as influence
from app.services.paper_source import ranking

logger = logging.getLogger("sciloop.wp04.feed")

router = APIRouter(tags=["papers"])

FeedView = Literal["recommended", "influence", "latest"]

# 每个视图的排序字段（按优先级）。latest 中**绝不允许**出现 influence_score/rank_score。
VIEW_ORDER: dict[str, tuple[str, ...]] = {
    "recommended": ("rank_score", "published_at", "id"),
    "influence": ("influence_score", "published_at", "id"),
    "latest": ("published_at", "id"),
}

VIEW_SORT_LABEL: dict[str, str] = {
    "recommended": "rank_score DESC NULLS LAST（四维：relevance/recency/citation_trend/evidence_completeness）",
    "influence": "influence_score DESC NULLS LAST（辅助展示分：venue/citation_velocity/code_heat）",
    "latest": "published_at DESC NULLS LAST（不按任何分数排序）",
}

_ORDER_COLUMNS: dict[str, Any] = {
    "rank_score": Paper.rank_score,
    "influence_score": Paper.influence_score,
    "published_at": Paper.published_at,
    "id": Paper.id,
}

_RANK_DIM_TEMPLATE: dict[str, dict[str, Any]] = {
    name: {"value": None, "source": "not_scored", "confidence": 0.0}
    for name in ranking.RANK_DIMENSIONS
}
_INFLUENCE_DIM_TEMPLATE: dict[str, dict[str, Any]] = {
    name: {"value": None, "source": "not_scored", "confidence": 0.0}
    for name in influence.INFLUENCE_DIMENSIONS
}


# --------------------------------------------------------------------------------------
# 数据库会话（优先复用 WP01 的同步会话工厂，否则按 DATABASE_URL 自建）
# --------------------------------------------------------------------------------------
_SESSION_FACTORY_NAMES = ("SessionLocal", "SyncSessionLocal", "session_factory", "sync_session_factory")
_ENGINE_NAMES = ("engine", "sync_engine")
_MODULE_CACHE: dict[str, bool] = {}


def _module_available(name: str) -> bool:
    if name not in _MODULE_CACHE:
        try:
            importlib.import_module(name)
            _MODULE_CACHE[name] = True
        except Exception:  # noqa: BLE001
            _MODULE_CACHE[name] = False
    return _MODULE_CACHE[name]


def _sync_database_url() -> str | None:
    """取同步驱动 URL（alembic/任务队列同样需要同步驱动，故此处复用同一份配置）。"""
    url: str | None = None
    try:
        from app.core.config import settings  # WP01 配置中心

        url = settings.sync_database_url
    except Exception:  # noqa: BLE001 - 配置中心未就绪时退回环境变量
        url = os.getenv("DATABASE_URL") or os.getenv("SYNC_DATABASE_URL")
    if not url:
        return None
    if "+asyncpg" in url:
        driver = "psycopg" if _module_available("psycopg") else "psycopg2"
        url = url.replace("+asyncpg", f"+{driver}")
    elif url.startswith("postgresql://"):
        if _module_available("psycopg"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif _module_available("psycopg2"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def _wp01_session_factory() -> sessionmaker | None:
    """若 WP01 提供了同步会话工厂则直接复用（避免出现第二个连接池）。"""
    try:
        module = importlib.import_module("app.db.session")
    except Exception:  # noqa: BLE001 - 会话模块尚未落地时静默降级
        return None
    factory = next(
        (getattr(module, name) for name in _SESSION_FACTORY_NAMES if getattr(module, name, None)),
        None,
    )
    if factory is not None:
        return factory
    engine = next(
        (getattr(module, name) for name in _ENGINE_NAMES if getattr(module, name, None)), None
    )
    if engine is None or type(engine).__name__ == "AsyncEngine":
        return None
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker | None:
    factory = _wp01_session_factory()
    if factory is not None:
        logger.info("feed 复用 WP01 的同步会话工厂")
        return factory
    url = _sync_database_url()
    if not url:
        logger.warning("未配置 DATABASE_URL，feed 端点将返回 503")
        return None
    try:
        engine = create_engine(url, pool_pre_ping=True, future=True)
    except SQLAlchemyError as exc:  # pragma: no cover - 驱动缺失属于部署问题
        logger.error("创建数据库引擎失败: %s", exc)
        return None
    logger.info("feed 自建同步会话工厂（driver=%s）", url.split("://", 1)[0])
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def reset_session_factory_cache() -> None:
    """清空会话工厂缓存（测试或运维切换数据库后调用）。"""
    _session_factory.cache_clear()


def get_session_factory() -> sessionmaker | None:
    """同步会话工厂（``None`` 表示数据库不可用）。

    与批量任务 ``app.tasks.jobs.score_papers`` 共用，保证两者使用同一份驱动探测与连接池。
    """
    return _session_factory()


def get_db() -> Any:
    """FastAPI 依赖：同步会话（端点用 ``def``，由 FastAPI 放入线程池执行）。"""
    factory = _session_factory()
    if factory is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "db_unavailable", "message": "数据库不可用", "detail": None},
        )
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------------------
# 纯函数：视图排序（供快照路径使用，同时作为口径分离的可测证据）
# --------------------------------------------------------------------------------------
class _DescNullsLast:
    """DESC 排序键包装：None 视为最小 -> ``reverse=True`` 时落到最后（NULLS LAST）。"""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: _DescNullsLast) -> bool:
        left, right = self.value, other.value
        if left is None:
            return right is not None
        if right is None:
            return False
        return bool(left < right)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _DescNullsLast) and self.value == other.value


def sort_items_for_view(items: Sequence[Mapping[str, Any]], view: str) -> list[Mapping[str, Any]]:
    """按视图口径排序（DESC，None 最后）。多字段用稳定排序从末位字段往前做。"""
    order = VIEW_ORDER.get(view)
    if order is None:
        raise ValueError(f"未知视图: {view}")
    result = list(items)
    for field in reversed(order):
        result.sort(key=lambda item, f=field: _DescNullsLast(item.get(f)), reverse=True)
    return result


def order_by_for_view(view: str) -> list[Any]:
    """生成 SQL ORDER BY 子句（与 :func:`sort_items_for_view` 口径一致）。"""
    order = VIEW_ORDER.get(view)
    if order is None:
        raise ValueError(f"未知视图: {view}")
    return [_ORDER_COLUMNS[field].desc().nulls_last() for field in order]


# --------------------------------------------------------------------------------------
# 响应构造
# --------------------------------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _breakdown_or_template(value: Any, template: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        return {key: dict(item) for key, item in template.items()}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[str(key)] = {
                "value": _as_float(item.get("value")),
                "source": item.get("source"),
                "confidence": _as_float(item.get("confidence")),
            }
        else:
            result[str(key)] = {"value": _as_float(item), "source": None, "confidence": None}
    return result


def _novelty_tag(paper: Paper) -> dict[str, Any]:
    """把库里的 llm_novelty 标签字段还原成带不确定性区间的展示标签（仅展示，不入分数）。"""
    if paper.llm_novelty is None and paper.llm_novelty_low is None:
        tag: dict[str, Any] = {"value": None, "low": None, "high": None, "stable": None, "note": None, "display": "unavailable"}
        return tag | {"model": None, "prompt_version": influence.NOVELTY_PROMPT_VERSION}
    stable = paper.llm_novelty_stable
    return {
        "value": None if stable is False else _as_float(paper.llm_novelty),
        "low": _as_float(paper.llm_novelty_low),
        "high": _as_float(paper.llm_novelty_high),
        "stable": stable,
        "note": paper.llm_novelty_note,
        "display": "range" if stable is False else "point",
        "model": None,
        "prompt_version": influence.NOVELTY_PROMPT_VERSION,
    }


def _fulltext_block(paper: Paper, document: PaperDocument | None) -> dict[str, Any]:
    """全文信息：优先 WP05 的 paper_documents，未就绪时按 papers.is_parsed 兜底并标注来源。"""
    if document is not None:
        return {
            "parse_status": document.parse_status,
            "coverage": _as_float(document.coverage),
            "document_version": document.document_version,
            "source": "paper_documents",
        }
    return {
        "parse_status": "ok" if paper.is_parsed else "unavailable",
        "coverage": None,
        "document_version": None,
        "source": "papers.is_parsed",
    }


def _affiliations(paper: Paper) -> list[str]:
    """展示型元数据：作者机构（**不参与任何分数计算**）。"""
    authors = paper.authors if isinstance(paper.authors, list) else []
    seen: list[str] = []
    for author in authors:
        if not isinstance(author, Mapping):
            continue
        affiliation = author.get("affiliation") or author.get("institution")
        if isinstance(affiliation, str) and affiliation.strip() and affiliation not in seen:
            seen.append(affiliation.strip())
    return seen


def build_item(paper: Paper, document: PaperDocument | None = None) -> dict[str, Any]:
    """构造单条 feed 返回项。"""
    rank_breakdown = _breakdown_or_template(paper.rank_breakdown, _RANK_DIM_TEMPLATE)
    score_breakdown = _breakdown_or_template(paper.score_breakdown, _INFLUENCE_DIM_TEMPLATE)
    try:
        influence_coverage = influence._weighted_influence(  # noqa: SLF001 - 与本模块同口径
            score_breakdown, influence.parse_influence_weights()
        )[1]
    except (ValueError, TypeError):  # pragma: no cover - 数据结构异常时如实归零
        influence_coverage = 0.0
    return {
        "id": paper.id,
        "source": paper.source,
        "external_id": paper.external_id,
        "doi": paper.doi,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": paper.authors,
        "published_at": paper.published_at.isoformat() if paper.published_at else None,
        "venue": paper.venue,
        "venue_source": paper.venue_source,
        "venue_level": paper.venue_level,
        "citation_count": paper.citation_count,
        "citation_velocity": _as_float(paper.citation_velocity),
        "code_url": paper.code_url,
        "rank_score": _as_float(paper.rank_score),
        "influence_score": _as_float(paper.influence_score),
        "score_coverage": _as_float(paper.score_coverage),
        "rank_breakdown": rank_breakdown,
        "score_breakdown": score_breakdown,
        "influence_coverage": influence_coverage,
        "llm_novelty_tag": _novelty_tag(paper),
        "fulltext": _fulltext_block(paper, document),
        "is_parsed": bool(paper.is_parsed),
        "display_metadata": {
            # 机构分已退出所有分数计算，仅作展示型元数据
            "institution_score": _as_float(paper.institution_score),
            "affiliations": _affiliations(paper),
            "note": "机构分与 LLM 新颖性分不参与任何排序或评分",
        },
    }


def _coverage_notes(query_provided: bool, view: str, view_includes_relevance: bool) -> list[str]:
    notes = [
        "排序分项缺失一律置 null 并按剩余权重归一，前端以 score_coverage 提示数据完整度",
        "机构分(institution_score)与 LLM 新颖性分(llm_novelty)不参与任何排序或评分",
    ]
    if not query_provided and view_includes_relevance:
        notes.append("未提供查询词 q：relevance 为 null，相关性不参与本次排序")
    if view == "influence":
        notes.append("本视图按 influence_score 排序；该分是辅助展示分，不用于默认（推荐）排序")
    return notes


# --------------------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------------------
def _field_condition(field: str) -> Any:
    """领域筛选：兼容 WP03 落库的多种 raw 形状，最后退回整段 raw 文本匹配。"""
    token = field.strip()
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return or_(
        Paper.raw["arxiv"]["primary_category"].astext == token,
        Paper.raw["primary_category"].astext == token,
        Paper.raw["fields"].contains([token]),
        Paper.raw["categories"].contains([token]),
        cast(Paper.raw, Text).ilike(f"%{escaped}%"),
    )


def _base_conditions(
    field: str | None, date_from: date | None, date_to: date | None, venue_only: bool
) -> list[Any]:
    conditions: list[Any] = []
    if field and field.strip():
        conditions.append(_field_condition(field))
    if date_from is not None:
        conditions.append(Paper.published_at >= date_from)
    if date_to is not None:
        conditions.append(Paper.published_at <= date_to)
    if venue_only:
        conditions.append(Paper.venue_level.is_not(None))
    return conditions


def _load_papers(db: Session, paper_ids: Sequence[int]) -> dict[int, Paper]:
    if not paper_ids:
        return {}
    rows = db.execute(select(Paper).where(Paper.id.in_(list(paper_ids)))).scalars().all()
    return {paper.id: paper for paper in rows}


def _load_documents(db: Session, paper_ids: Sequence[int]) -> dict[int, PaperDocument]:
    """取每篇论文最新的一条解析记录（按 parsed_at 降序，首个即最新）。"""
    if not paper_ids:
        return {}
    rows = (
        db.execute(
            select(PaperDocument)
            .where(PaperDocument.paper_id.in_(list(paper_ids)))
            .order_by(PaperDocument.paper_id, PaperDocument.parsed_at.desc())
        )
        .scalars()
        .all()
    )
    latest: dict[int, PaperDocument] = {}
    for document in rows:
        latest.setdefault(document.paper_id, document)
    return latest


def _request_filters(
    field: str | None, date_from: date | None, date_to: date | None, venue_only: bool
) -> dict[str, Any]:
    return {
        "field": field or None,
        "from": date_from.isoformat() if date_from else None,
        "to": date_to.isoformat() if date_to else None,
        "venue_only": bool(venue_only),
    }


def _pick_snapshot(
    rows: Sequence[PaperFeedSnapshot], request_filters: Mapping[str, Any]
) -> PaperFeedSnapshot | None:
    """优先匹配 filters 一致的快照，其次取最新快照。"""
    for row in rows:
        stored = row.filters if isinstance(row.filters, Mapping) else {}
        if not stored:
            continue
        if all(stored.get(key) == value for key, value in request_filters.items() if key in stored):
            return row
    return rows[0] if rows else None


def _snapshot_enabled() -> bool:
    try:
        from app.core.config import settings

        return bool(settings.demo_snapshot_enabled)
    except Exception:  # noqa: BLE001
        return str(os.getenv("DEMO_SNAPSHOT_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _replay_mode() -> bool:
    try:
        from app.core.config import settings

        return bool(settings.llm_replay)
    except Exception:  # noqa: BLE001
        return str(os.getenv("LLM_REPLAY", "0")).strip().lower() not in {"0", "false", "no", "off", ""}


@router.get("/papers/feed")
def get_papers_feed(
    view: FeedView = Query("recommended", description="recommended|influence|latest"),
    field: str | None = Query(None, description="领域，如 cs.AI"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    venue_only: bool = Query(False, description="仅看已识别到顶会/期刊等级的论文"),
    snapshot: str | None = Query(None, description="demo=读取 is_demo 快照；replay=回放数据"),
    q: str | None = Query(None, description="可选检索词；不传则 relevance 不参与排序"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """首页论文库：三视图口径分离 + 快照兜底。"""
    filters = _request_filters(field, date_from, date_to, venue_only)
    conditions = _base_conditions(field, date_from, date_to, venue_only)

    use_snapshot = bool(snapshot) and snapshot.strip().lower() not in {"", "live"}
    data_source = "snapshot"
    data_source_note: str | None = None
    if use_snapshot:
        if not _snapshot_enabled():
            use_snapshot = False
            data_source_note = "DEMO_SNAPSHOT_ENABLED=false，快照已禁用，本次返回实时数据"
        elif snapshot and snapshot.strip().lower() == "replay":
            data_source = "replay"

    items: list[dict[str, Any]] = []
    total = 0

    if use_snapshot:
        snapshot_rows = (
            db.execute(
                select(PaperFeedSnapshot)
                .where(PaperFeedSnapshot.is_demo.is_(True), PaperFeedSnapshot.view_type == view)
                .order_by(PaperFeedSnapshot.created_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )
        chosen = _pick_snapshot(snapshot_rows, filters)
        if chosen is None:
            use_snapshot = False
            data_source = "live"
            data_source_note = "未找到匹配的 demo 快照，已回退实时数据（如实标记 data_source=live）"
        else:
            raw_ids = chosen.paper_ids if isinstance(chosen.paper_ids, list) else []
            paper_ids = [int(pid) for pid in raw_ids if isinstance(pid, (int, str)) and str(pid).isdigit()]
            total = len(paper_ids)
            offset = (page - 1) * page_size
            page_ids = paper_ids[offset : offset + page_size]
            paper_map = _load_papers(db, page_ids)
            document_map = _load_documents(db, page_ids)
            ordered = [paper_map[pid] for pid in page_ids if pid in paper_map]
            items = [build_item(paper, document_map.get(paper.id)) for paper in ordered]

    if not use_snapshot:
        data_source = "replay" if _replay_mode() else "live"
        if data_source == "replay":
            data_source_note = "LLM_REPLAY=1：本页包含回放数据，禁止当作实时结果"
        total = int(
            db.execute(select(func.count()).select_from(Paper).where(*conditions)).scalar_one()
        )
        statement = (
            select(Paper)
            .where(*conditions)
            .order_by(*order_by_for_view(view))
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        papers = db.execute(statement).scalars().all()
        document_map = _load_documents(db, [paper.id for paper in papers])
        items = [build_item(paper, document_map.get(paper.id)) for paper in papers]

    reranked = False
    if q and items:
        # 提供检索词时在返回页内用含 relevance 的四维重排（跨页分页仍按库内 rank_score）
        batch = ranking.compute_rank_batch(
            [
                {
                    "title": item["title"],
                    "abstract": item["abstract"],
                    "published_at": item["published_at"],
                    "venue": item["venue"],
                    "venue_level": item["venue_level"],
                    "venue_source": item["venue_source"],
                    "doi": item["doi"],
                    "citation_count": item["citation_count"],
                    "code_url": item["code_url"],
                    "is_parsed": item["is_parsed"],
                    "raw": None,
                }
                for item in items
            ],
            query=q,
            documents=[item["fulltext"] for item in items],
        )
        # ``compute_rank_batch`` 逐条返回，长度必须与 items 相等（strict 兜底防止静默截断）
        for item, computed in zip(items, batch, strict=True):
            item["rank_breakdown"] = computed["rank_breakdown"]
            item["rank_score"] = computed["rank_score"]
            item["score_coverage"] = computed["score_coverage"]
        items = list(sort_items_for_view(items, view))
        reranked = True

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "data_source": data_source,
        "data_source_note": data_source_note,
        "view": view,
        "sort_by": VIEW_SORT_LABEL[view],
        "reranked_by_query": reranked,
        "filters": filters,
        "ranking": {
            "rank_weights": ranking.parse_rank_weights(),
            "influence_weights": influence.parse_influence_weights(),
            "query_provided": bool(q),
            "notes": _coverage_notes(bool(q), view, view == "recommended"),
        },
    }
