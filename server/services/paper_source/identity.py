# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""统一身份映射（WP03-T5）：同一篇论文从多源进来只允许一条 ``papers`` 记录。

身份类型（``paper_identities.id_type``，UNIQUE(id_type, id_value)）
-----------------------------------------------------------------
doi / arxiv / semantic_scholar / openalex / title_hash

``title_hash`` = 归一化标题（小写、去标点、压空白）的 **SHA-256 前 16 位**。

入库流程
--------
1. 收集本次 meta 的全部标识（含 title_hash）→ 按优先级
   ``doi > arxiv > semantic_scholar > openalex > title_hash`` 查 ``paper_identities``；
2. **命中**：复用该 ``paper_id``，补全缺失标识与缺失字段（已有非空值不被覆盖）；
3. **未命中**：新建 ``papers`` 并写入全部标识。

公开接口（供 WP04/WP05 复用）
----------------------------
- :func:`fetch_and_upsert_paper` → ``paper_id``
- :func:`resolve_paper_by_identifiers` → ``paper_id | None``
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models.paper import Paper, PaperIdentity
from services.paper_source.openalex_client import normalize_doi as _normalize_doi_base

logger = logging.getLogger("sciloop.wp03.identity")

ID_DOI = "doi"
ID_ARXIV = "arxiv"
ID_S2 = "semantic_scholar"
ID_OPENALEX = "openalex"
ID_TITLE_HASH = "title_hash"

ID_TYPES: tuple[str, ...] = (ID_DOI, ID_ARXIV, ID_S2, ID_OPENALEX, ID_TITLE_HASH)
# 强标识优先：先用最可信的标识定位既有论文，title_hash 只作最后兜底
ID_PRIORITY: tuple[str, ...] = (ID_DOI, ID_ARXIV, ID_S2, ID_OPENALEX, ID_TITLE_HASH)

# venue 来源优先级（数字越大越可信）——升级策略用，与 source_records 的 confidence 同源
VENUE_SOURCE_RANK: dict[str, int] = {
    "arxiv_comment": 1,
    "openalex": 2,
    "s2": 3,
    "semantic_scholar": 3,
    "whitelist": 4,
}

# meta 中可直接落库的标量/JSON 字段
_SCALAR_FIELDS: tuple[str, ...] = (
    "title",
    "abstract",
    "authors",
    "published_at",
    "updated_at_src",
    "venue",
    "venue_source",
    "pdf_url",
    "code_url",
    "doi",
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


# --------------------------------------------------------------------------------------
# 归一化工具
# --------------------------------------------------------------------------------------
def normalize_title(title: Any) -> str:
    """标题归一：Unicode NFKC → 小写 → 去标点 → 压空白。"""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", str(title)).lower()
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_hash(title: Any) -> str:
    """归一化标题的 SHA-256 前 16 位（空标题返回空串，由调用方决定是否使用）。"""
    normalized = normalize_title(title)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def normalize_doi(value: Any) -> str | None:
    normalized = _normalize_doi_base(value)
    return normalized.lower() if normalized else None


def normalize_arxiv_id(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for prefix in ("arxiv:", "https://arxiv.org/abs/", "http://arxiv.org/abs/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
    text = re.sub(r"v\d+$", "", text)
    return text.strip() or None


def normalize_identifier(id_type: str, value: Any) -> str | None:
    """按类型归一标识；无法归一的返回 None（不会写入 identities）。"""
    if value is None:
        return None
    if id_type == ID_DOI:
        return normalize_doi(value)
    if id_type == ID_ARXIV:
        return normalize_arxiv_id(value)
    if id_type == ID_TITLE_HASH:
        text = str(value).strip().lower()
        return text if re.fullmatch(r"[0-9a-f]{16}", text) else (title_hash(value) or None)
    text = str(value).strip()
    if not text:
        return None
    if id_type == ID_OPENALEX:
        return text.rstrip("/").split("/")[-1].lower()
    return text[:256]


def build_identifier_map(
    identifiers: Mapping[str, Any] | None = None,
    *,
    title: Any = None,
    doi: Any = None,
    arxiv_id: Any = None,
    s2_id: Any = None,
    openalex_id: Any = None,
) -> dict[str, str]:
    """汇总所有已知标识（含 title_hash），去空、去重、按优先级排序。"""
    raw: dict[str, Any] = dict(identifiers or {})
    for id_type, value in (
        (ID_DOI, doi),
        (ID_ARXIV, arxiv_id),
        (ID_S2, s2_id),
        (ID_OPENALEX, openalex_id),
    ):
        if value and not raw.get(id_type):
            raw[id_type] = value
    if title:
        raw.setdefault(ID_TITLE_HASH, title_hash(title))

    resolved: dict[str, str] = {}
    for id_type in ID_PRIORITY:
        if id_type not in raw:
            continue
        normalized = normalize_identifier(id_type, raw.get(id_type))
        if normalized:
            resolved[id_type] = normalized
    return resolved


# --------------------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------------------
@dataclass
class UpsertResult:
    """一次 upsert 的结果（用于日志与"新增/复用"计数）。"""

    paper_id: int
    created: bool
    matched_by: str | None = None
    identities_added: list[str] = field(default_factory=list)
    identities_conflicted: list[str] = field(default_factory=list)
    fields_filled: list[str] = field(default_factory=list)

    def as_log(self) -> dict[str, Any]:
        return {
            "paper_id": self.paper_id,
            "created": self.created,
            "matched_by": self.matched_by,
            "identities_added": self.identities_added,
            "identities_conflicted": self.identities_conflicted,
            "fields_filled": self.fields_filled,
        }


# --------------------------------------------------------------------------------------
# 标识查询
# --------------------------------------------------------------------------------------
def resolve_paper_by_identifiers(
    session: Session, identifiers: Mapping[str, Any] | Iterable[tuple[str, Any]]
) -> int | None:
    """按任意已知标识查 ``paper_id``；命中多个时按 ``ID_PRIORITY`` 取最可信的。"""
    pairs: list[tuple[str, str]] = []
    if isinstance(identifiers, Mapping):
        for id_type, value in identifiers.items():
            normalized = normalize_identifier(str(id_type), value)
            if normalized:
                pairs.append((str(id_type), normalized))
    else:
        for id_type, value in identifiers:
            normalized = normalize_identifier(str(id_type), value)
            if normalized:
                pairs.append((str(id_type), normalized))
    if not pairs:
        return None

    hits: dict[str, int] = {}
    for id_type, id_value in pairs:
        row = session.execute(
            select(PaperIdentity.paper_id).where(
                PaperIdentity.id_type == id_type, PaperIdentity.id_value == id_value
            )
        ).scalar_one_or_none()
        if row is not None:
            hits[id_type] = int(row)
    if not hits:
        return None
    for id_type in ID_PRIORITY:
        if id_type in hits:
            return hits[id_type]
    return next(iter(hits.values()))


def resolve_paper_by_title(session: Session, title: Any) -> int | None:
    """按 ``title_hash`` 兜底查重（弱标识，仅在强标识都未命中时使用）。"""
    hashed = title_hash(title)
    if not hashed:
        return None
    return resolve_paper_by_identifiers(session, {ID_TITLE_HASH: hashed})


def attach_identities(
    session: Session,
    paper_id: int,
    identifiers: Mapping[str, Any],
    *,
    primary_type: str | None = None,
) -> tuple[list[str], list[str]]:
    """把标识写入 ``paper_identities``；返回 ``(已添加, 冲突)``。

    冲突 = 该 ``(id_type, id_value)`` 已被**另一篇**论文占用（说明上游数据本身有歧义），
    此时跳过写入并记日志，避免把两篇论文错误合并。
    """
    added: list[str] = []
    conflicted: list[str] = []
    existing = {
        (row.id_type, row.id_value)
        for row in session.execute(
            select(PaperIdentity).where(PaperIdentity.paper_id == int(paper_id))
        )
        .scalars()
        .all()
    }
    for id_type in ID_PRIORITY:
        value = identifiers.get(id_type)
        if not value:
            continue
        key = (id_type, value)
        if key in existing:
            continue
        holder = session.execute(
            select(PaperIdentity.paper_id).where(
                PaperIdentity.id_type == id_type, PaperIdentity.id_value == value
            )
        ).scalar_one_or_none()
        if holder is not None and int(holder) != int(paper_id):
            conflicted.append(f"{id_type}={value}->paper:{holder}")
            logger.warning(
                "identity_conflict id=%s value=%s holder_paper=%s target_paper=%s",
                id_type,
                value,
                holder,
                paper_id,
            )
            continue
        session.add(
            PaperIdentity(
                paper_id=int(paper_id),
                id_type=id_type,
                id_value=value,
                is_primary=bool(primary_type and id_type == primary_type),
            )
        )
        added.append(f"{id_type}={value}")
        existing.add(key)
    return added, conflicted


# --------------------------------------------------------------------------------------
# upsert 主流程
# --------------------------------------------------------------------------------------
def _scalar(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date() if value.tzinfo is None else value.date()
    return value


def _merge_raw(
    paper: Paper, source: str, payload: Any, *, extra: Mapping[str, Any] | None = None
) -> None:
    """把本次原始响应合并进 ``papers.raw``（按来源分键，保留历史来源）。

    防御：若调用方误把 ``{source: payload}`` 当成 payload 传进来（历史 bug），
    这里会自动解包一层，保证落库后的路径恒为 ``raw[source][...]``——
    WP04 的 ``ranking.counts_by_year_from_raw`` 与 ``influence_score.stars_from_raw``
    就是按这个路径读的，双层嵌套会让它们全部读不到。
    """
    if payload is None and not extra:
        return
    if isinstance(payload, Mapping) and len(payload) == 1 and source in payload:
        payload = payload[source]
    current = paper.raw if isinstance(paper.raw, Mapping) else {}
    merged: dict[str, Any] = dict(current)
    if payload is not None:
        merged[source] = payload
    if extra:
        for key, value in extra.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    sources = merged.get("_sources")
    merged["_sources"] = sorted({*(sources if isinstance(sources, list) else []), source})
    paper.raw = merged


def _fill_scalars(paper: Paper, meta: Mapping[str, Any]) -> list[str]:
    """只补空缺字段；``citation_count`` 与 ``raw`` 另行处理（前者允许刷新，后者总是合并）。"""
    filled: list[str] = []
    for name in _SCALAR_FIELDS:
        if name not in meta:
            continue
        value = _scalar(meta.get(name))
        if value is None or value == "":
            continue
        if name in {"venue", "venue_source"}:
            continue  # venue 走优先级升级策略
        current = getattr(paper, name)
        if current is None or current == "" or (name == "authors" and not current):
            setattr(paper, name, value)
            filled.append(name)
    return filled


def _apply_venue(paper: Paper, meta: Mapping[str, Any]) -> list[str]:
    """venue 升级策略：新来源更可信则替换；否则只补空缺。"""
    new_venue = meta.get("venue")
    if not new_venue:
        return []
    new_source = str(meta.get("venue_source") or "openalex")
    current_venue = paper.venue
    current_source = str(paper.venue_source or "")
    if not current_venue:
        paper.venue = str(new_venue)[:128]
        paper.venue_source = new_source[:32]
        return ["venue"]
    if VENUE_SOURCE_RANK.get(new_source, 0) > VENUE_SOURCE_RANK.get(current_source, 0) and str(
        new_venue
    ) != str(current_venue):
        old = paper.venue
        paper.venue = str(new_venue)[:128]
        paper.venue_source = new_source[:32]
        logger.info(
            "venue_upgraded paper_id=%s from=%r(%s) to=%r(%s)",
            paper.id,
            old,
            current_source,
            paper.venue,
            new_source,
        )
        return ["venue"]
    return []


def upsert_paper(
    session: Session, meta: Mapping[str, Any], *, force_paper_id: int | None = None
) -> UpsertResult:
    """把一份归一 meta 合并进库：命中则复用，未命中则新建。

    ``meta`` 必填 ``source`` 与 ``title``；``external_id`` 缺省时用标识或标题哈希兜底。

    ``force_paper_id``：调用方已经从别处拿到 ``paper_id``（例如同一批抓取里 arXiv 那一步
    已解析出的主记录）时使用，跳过标识查询直接合并，**保证富化步骤永远不会新建论文**。
    """
    source = str(meta.get("source") or "").strip()
    title = str(meta.get("title") or "").strip()
    if not source or not title:
        raise ValueError("upsert_paper 需要 source 与 title")

    # 本模块被 sync session 调用，而 WP01 的 SessionLocal 设了 autoflush=False：
    # 必须先 flush，否则上一次 upsert 刚 add 的 paper_identities 还不可见，
    # 会导致同一篇论文被重复插一行（实测踩过）。
    if session.new or session.dirty:
        session.flush()

    identifiers = build_identifier_map(
        meta.get("identifiers"),
        title=title,
        doi=meta.get("doi"),
        arxiv_id=meta.get("arxiv_id"),
        s2_id=meta.get("s2_id"),
        openalex_id=meta.get("openalex_id"),
    )
    matched_by: str | None = None
    paper: Paper | None = None

    if force_paper_id is not None:
        paper = session.get(Paper, int(force_paper_id))
        matched_by = "force_paper_id" if paper is not None else None

    hits: dict[str, int] = {}
    if paper is None:
        for id_type in ID_PRIORITY:
            value = identifiers.get(id_type)
            if not value:
                continue
            holder = session.execute(
                select(PaperIdentity.paper_id).where(
                    PaperIdentity.id_type == id_type, PaperIdentity.id_value == value
                )
            ).scalar_one_or_none()
            if holder is not None:
                hits[id_type] = int(holder)
        for id_type in ID_PRIORITY:
            if id_type in hits:
                matched_by = id_type
                paper = session.get(Paper, hits[id_type])
                break

    external_id = str(
        meta.get("external_id")
        or identifiers.get(ID_ARXIV)
        or identifiers.get(ID_DOI)
        or title_hash(title)
    )[:128]

    if paper is None:
        # 同名同源的记录（UNIQUE(source, external_id)）也要复用，避免唯一键冲突
        paper = session.execute(
            select(Paper).where(Paper.source == source, Paper.external_id == external_id)
        ).scalar_one_or_none()
        if paper is not None:
            matched_by = "source_external_id"

    created = False
    if paper is None:
        paper = Paper(
            source=source,
            external_id=external_id,
            title=title,
            citation_count=meta.get("citation_count"),
        )
        session.add(paper)
        session.flush()
        created = True
        logger.info("paper_created id=%s source=%s external_id=%s", paper.id, source, external_id)

    filled = _fill_scalars(paper, meta)
    filled += _apply_venue(paper, meta)
    citation = meta.get("citation_count")
    if citation is not None:
        paper.citation_count = int(citation)
        if "citation_count" not in filled:
            filled.append("citation_count")
    if meta.get("is_parsed") is not None:
        paper.is_parsed = bool(meta.get("is_parsed"))
    _merge_raw(paper, source, meta.get("raw"), extra=meta.get("raw_extra"))
    paper.updated_at = datetime.now(UTC)

    primary_type = {
        "arxiv": ID_ARXIV,
        "semantic_scholar": ID_S2,
        "openalex": ID_OPENALEX,
    }.get(source)
    added, conflicted = attach_identities(session, paper.id, identifiers, primary_type=primary_type)

    result = UpsertResult(
        paper_id=int(paper.id),
        created=created,
        matched_by=None if created else (matched_by or "unknown"),
        identities_added=added,
        identities_conflicted=conflicted,
        fields_filled=filled,
    )
    logger.info("paper_upsert %s", result.as_log())
    return result


def fetch_and_upsert_paper(raw_meta: Mapping[str, Any], session: Session | None = None) -> int:
    """统一入库入口：返回 ``paper_id``（供 WP04/WP05 复用）。

    传入 ``session`` 时由调用方负责事务；不传时自动开事务并提交。
    """
    if session is not None:
        return upsert_paper(session, raw_meta).paper_id
    from db.session import session_scope

    with session_scope() as own_session:
        return upsert_paper(own_session, raw_meta).paper_id


def merge_source_payload(
    session: Session,
    paper_id: int,
    source: str,
    payload: Any,
    *,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """把某来源的原始响应合并进**已存在**的论文（不新建、不改标识）。

    用于 GitHub 这类"不是论文身份"的富化来源：仓库信息只并入 ``papers.raw``，
    绝不允许因为一个代码链接就多出一行论文。
    """
    paper = session.get(Paper, int(paper_id))
    if paper is None:
        logger.warning(
            "merge_source_payload paper_not_found paper_id=%s source=%s", paper_id, source
        )
        return False
    _merge_raw(paper, source, payload, extra=extra)
    paper.updated_at = datetime.now(UTC)
    return True


def force_null(session: Session, paper_id: int, column: str) -> None:
    """把某列显式置 NULL（用于"确实取不到"的场景，避免 DB DEFAULT 0 被误读为真实值）。

    仅允许 ``paper_identities`` 之外的 ``papers`` 可空数值列，且只用于 citation_count
    这类"缺失必须显式表达"的字段。
    """
    if column not in {"citation_count", "citation_velocity", "influence_score", "rank_score"}:
        raise ValueError(f"force_null 不支持的列: {column}")
    from sqlalchemy import update

    session.execute(update(Paper).where(Paper.id == int(paper_id)).values({column: None}))


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def coerce_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def paper_identity_rows(session: Session, paper_id: int) -> list[dict[str, Any]]:
    """列出某篇论文的全部标识（详情端点与验收脚本用）。"""
    rows: Sequence[PaperIdentity] = (
        session.execute(
            select(PaperIdentity)
            .where(PaperIdentity.paper_id == int(paper_id))
            .order_by(PaperIdentity.id)
        )
        .scalars()
        .all()
    )
    return [
        {
            "id_type": row.id_type,
            "id_value": row.id_value,
            "is_primary": bool(row.is_primary),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


__all__ = [
    "ID_ARXIV",
    "ID_DOI",
    "ID_OPENALEX",
    "ID_PRIORITY",
    "ID_S2",
    "ID_TITLE_HASH",
    "ID_TYPES",
    "UpsertResult",
    "VENUE_SOURCE_RANK",
    "attach_identities",
    "build_identifier_map",
    "coerce_date",
    "decimal_or_none",
    "fetch_and_upsert_paper",
    "force_null",
    "merge_source_payload",
    "normalize_arxiv_id",
    "normalize_doi",
    "normalize_identifier",
    "normalize_title",
    "paper_identity_rows",
    "resolve_paper_by_identifiers",
    "resolve_paper_by_title",
    "title_hash",
    "upsert_paper",
]
