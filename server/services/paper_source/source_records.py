# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""取数留痕（WP03-T6）：每一次外部取数都写 ``paper_source_records``。

每条记录回答四个问题：**哪个源**（source）、**取了哪个字段**（field_name）、
**取回什么**（raw_value，取不到就是 NULL）、**怎么取的**（request_url + http_status +
confidence + fetched_at）。

confidence 分级（WP03-T6 硬约束）
--------------------------------
=====================  ==========
源                      confidence
=====================  ==========
semantic_scholar       1.0
openalex               0.8
arxiv_comment          0.6（正则派生的 venue 线索 / 代码链接）
github                 1.0
arxiv                  1.0（arXiv 自身元数据是权威源）
=====================  ==========

规则：**只有真的发出了 HTTP 请求才会写记录**（因此 ``http_status`` 永不为 NULL，
验收 WP03-A4 才成立）；没发请求的降级（如缺 mailto）写进 ``papers.raw`` 的
``error`` 字段，不伪造留痕。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models.paper import PaperSourceRecord

logger = logging.getLogger("sciloop.wp03.source_records")

SOURCE_LABELS: dict[str, str] = {
    "arxiv": "arXiv API",
    "arxiv_comment": "arXiv comment 正则抽取",
    "semantic_scholar": "Semantic Scholar",
    "openalex": "OpenAlex",
    "github": "GitHub API",
    "llm": "LLM",
}

CONFIDENCE_BY_SOURCE: dict[str, Decimal] = {
    "semantic_scholar": Decimal("1.000"),
    "openalex": Decimal("0.800"),
    "arxiv_comment": Decimal("0.600"),
    "github": Decimal("1.000"),
    "arxiv": Decimal("1.000"),
}

# 字段名口径（前端与验收脚本按此查询）
FIELD_VENUE = "venue"
FIELD_CITATION_COUNT = "citation_count"
FIELD_CODE_URL = "code_url"
FIELD_STARS = "stargazers_count"
FIELD_COUNTS_BY_YEAR = "counts_by_year"
FIELD_INSTITUTIONS = "institutions"
FIELD_PUBLICATION_DATE = "publication_date"

SUPPORTED_SOURCES: tuple[str, ...] = tuple(SOURCE_LABELS)


def confidence_for(source: str, field_name: str | None = None) -> Decimal | None:
    """按来源给 confidence；未知来源返回 None（不猜）。"""
    del field_name  # 目前同一来源的所有字段同权，保留参数以便未来细分
    return CONFIDENCE_BY_SOURCE.get(str(source))


def format_raw_value(value: Any) -> str | None:
    """把任意值序列化为留痕文本；``None`` 原样返回（表示确实没取到）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 兜底
        return str(value)


def build_record(
    *,
    paper_id: int,
    source: str,
    field_name: str,
    raw_value: Any = None,
    request_url: str | None = None,
    http_status: int | None = None,
    confidence: float | Decimal | None = None,
    fetched_at: datetime | None = None,
) -> PaperSourceRecord:
    """构造一条留痕记录（不落库）。"""
    return PaperSourceRecord(
        paper_id=int(paper_id),
        source=str(source),
        field_name=str(field_name)[:64],
        raw_value=format_raw_value(raw_value),
        confidence=(
            Decimal(str(confidence))
            if confidence is not None
            else confidence_for(source, field_name)
        ),
        request_url=request_url,
        http_status=int(http_status) if http_status is not None else None,
        fetched_at=fetched_at or datetime.now(UTC),
    )


def record(
    session: Session,
    *,
    paper_id: int,
    source: str,
    field_name: str,
    raw_value: Any = None,
    request_url: str | None = None,
    http_status: int | None = None,
    confidence: float | Decimal | None = None,
    fetched_at: datetime | None = None,
) -> PaperSourceRecord:
    """写一条留痕并 flush（不 commit，交由上层事务/批次提交）。"""
    item = build_record(
        paper_id=paper_id,
        source=source,
        field_name=field_name,
        raw_value=raw_value,
        request_url=request_url,
        http_status=http_status,
        confidence=confidence,
        fetched_at=fetched_at,
    )
    session.add(item)
    return item


def record_many(session: Session, items: Iterable[PaperSourceRecord]) -> int:
    """批量写入留痕，返回写入条数。"""
    rows = list(items)
    if not rows:
        return 0
    session.add_all(rows)
    return len(rows)


def record_fields_for_fetch(
    session: Session,
    *,
    paper_id: int,
    source: str,
    fields: Mapping[str, Any],
    request_url: str | None,
    http_status: int | None,
    confidence: float | Decimal | None = None,
    fetched_at: datetime | None = None,
) -> int:
    """一次请求取回多个字段 → 每个字段一条留痕（``raw_value=None`` 表示该字段没取到）。"""
    rows = [
        build_record(
            paper_id=paper_id,
            source=source,
            field_name=field_name,
            raw_value=value,
            request_url=request_url,
            http_status=http_status,
            confidence=confidence,
            fetched_at=fetched_at,
        )
        for field_name, value in fields.items()
    ]
    return record_many(session, rows)


def list_records(
    session: Session,
    paper_id: int,
    *,
    source: str | None = None,
    field_name: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> tuple[list[PaperSourceRecord], int]:
    """分页查询某篇论文的留痕（``GET /papers/{id}/sources`` 用）。"""
    conditions = [PaperSourceRecord.paper_id == int(paper_id)]
    if source:
        conditions.append(PaperSourceRecord.source == source)
    if field_name:
        conditions.append(PaperSourceRecord.field_name == field_name)

    total = session.execute(
        select(func.count()).select_from(PaperSourceRecord).where(*conditions)
    ).scalar_one()
    rows = (
        session.execute(
            select(PaperSourceRecord)
            .where(*conditions)
            .order_by(PaperSourceRecord.fetched_at.desc(), PaperSourceRecord.id.desc())
            .offset(max(0, (page - 1) * page_size))
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total or 0)


def record_to_dict(item: PaperSourceRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "paper_id": item.paper_id,
        "source": item.source,
        "source_label": SOURCE_LABELS.get(item.source, item.source),
        "field_name": item.field_name,
        "raw_value": item.raw_value,
        "confidence": float(item.confidence) if item.confidence is not None else None,
        "request_url": item.request_url,
        "http_status": item.http_status,
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
    }


# --------------------------------------------------------------------------------------
# 源健康（GET /sources/health 的数据来源）
# --------------------------------------------------------------------------------------
def source_stats(session: Session) -> dict[str, dict[str, Any]]:
    """按来源聚合留痕：最近成功时间、最近 HTTP 状态、成功/失败计数。"""
    rows = session.execute(
        select(
            PaperSourceRecord.source,
            func.count().label("total"),
            func.max(PaperSourceRecord.fetched_at).label("last_fetched_at"),
        ).group_by(PaperSourceRecord.source)
    ).all()
    stats: dict[str, dict[str, Any]] = {
        source: {
            "total_records": int(row.total or 0),
            "last_fetched_at": row.last_fetched_at.isoformat() if row.last_fetched_at else None,
            "last_success_at": None,
            "last_http_status": None,
            "success_records": 0,
            "empty_records": 0,
        }
        for source, row in ((row.source, row) for row in rows)
    }

    # 最近一次成功（2xx 且取到值）的时间与状态
    success_rows = session.execute(
        select(
            PaperSourceRecord.source,
            func.count().label("success"),
            func.max(PaperSourceRecord.fetched_at).label("last_success_at"),
        )
        .where(
            PaperSourceRecord.http_status >= 200,
            PaperSourceRecord.http_status < 300,
            PaperSourceRecord.raw_value.is_not(None),
        )
        .group_by(PaperSourceRecord.source)
    ).all()
    for row in success_rows:
        node = stats.setdefault(
            row.source,
            {
                "total_records": 0,
                "last_fetched_at": None,
                "last_success_at": None,
                "last_http_status": None,
                "success_records": 0,
                "empty_records": 0,
            },
        )
        node["success_records"] = int(row.success or 0)
        node["last_success_at"] = row.last_success_at.isoformat() if row.last_success_at else None

    # 每个来源最近一次记录的 http_status
    last_rows = session.execute(
        select(
            PaperSourceRecord.source, PaperSourceRecord.http_status, PaperSourceRecord.fetched_at
        ).order_by(PaperSourceRecord.fetched_at.desc(), PaperSourceRecord.id.desc())
    ).all()
    for row in last_rows:
        node = stats.get(row.source)
        if node is not None and node["last_http_status"] is None:
            node["last_http_status"] = row.http_status

    empty_rows = session.execute(
        select(PaperSourceRecord.source, func.count().label("empty"))
        .where(PaperSourceRecord.raw_value.is_(None))
        .group_by(PaperSourceRecord.source)
    ).all()
    for row in empty_rows:
        node = stats.get(row.source)
        if node is not None:
            node["empty_records"] = int(row.empty or 0)
    return stats


def distinct_papers_by_source(session: Session, source: str) -> int:
    return int(
        session.execute(
            select(func.count(func.distinct(PaperSourceRecord.paper_id))).where(
                PaperSourceRecord.source == source
            )
        ).scalar_one()
        or 0
    )


__all__ = [
    "CONFIDENCE_BY_SOURCE",
    "FIELD_CITATION_COUNT",
    "FIELD_CODE_URL",
    "FIELD_COUNTS_BY_YEAR",
    "FIELD_INSTITUTIONS",
    "FIELD_PUBLICATION_DATE",
    "FIELD_STARS",
    "FIELD_VENUE",
    "SOURCE_LABELS",
    "SUPPORTED_SOURCES",
    "build_record",
    "confidence_for",
    "distinct_papers_by_source",
    "format_raw_value",
    "list_records",
    "record",
    "record_fields_for_fetch",
    "record_many",
    "record_to_dict",
    "source_stats",
]

# 供验收脚本 import 的常量（保持与 WP03 JSON 一致）
CONFIDENCE_EXPECTED: dict[str, float] = {
    "semantic_scholar": 1.0,
    "openalex": 0.8,
    "arxiv_comment": 0.6,
    "github": 1.0,
    "arxiv": 1.0,
}
