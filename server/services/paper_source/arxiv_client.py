# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""arXiv 取数客户端（WP03-T1）。

职责
----
1. 按 ``ARXIV_FIELDS``（默认 ``cs.AI,cs.CL,cs.CV,cs.LG``）构造分类检索式，
   分页拉取 Atom XML 并解析出 ``title / abstract / authors / published / updated /
   pdf_url / comment / doi / primary_category``。
2. 对 ``comment`` 与 ``abstract`` 做**正则抽取**：
   - ``venue_hint``：如 ``Accepted to SLT 2026`` → ``SLT 2026``；
   - ``code_url``：如 ``Website: ..., Code: https://github.com/owner/repo`` → 仓库地址。
   本模块**只抽原始线索**，等级映射与白名单归 WP04 的 ``venue.py``。
3. 支持按 ``submittedDate`` 的增量抓取（``date_from`` 起）与分页。

取不到就置 ``None``：``journal_ref`` 为空的预印本不会编造 venue。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from core.config import get_settings
from services.paper_source.cache import HttpResult, clean_env_value, get_source_http

logger = logging.getLogger("sciloop.wp03.arxiv")

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
SOURCE_NAME = "arxiv"

# arXiv 官方礼貌抓取建议：单次 ≤ 2000 条、请求间隔 ≥ 3s
DEFAULT_PAGE_SIZE = 100
ARXIV_QPS = 1.0
VENUE_MAX_LEN = 128

_ATOM = f"{{{ATOM_NS}}}"
_ARXIV = f"{{{ARXIV_NS}}}"
_OPENSEARCH = f"{{{OPENSEARCH_NS}}}"

_ARXIV_ID_RE = re.compile(r"(?P<id>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?P<version>v\d+)?")
_GITHUB_RE = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
# GitHub 的非仓库路径/保留字，命中即跳过（避免抽出 https://github.com/features 之类）
_GITHUB_RESERVED = frozenset(
    {
        "about",
        "account",
        "apps",
        "blog",
        "careers",
        "collections",
        "contact",
        "dashboard",
        "docs",
        "enterprise",
        "events",
        "explore",
        "features",
        "issues",
        "join",
        "login",
        "marketplace",
        "new",
        "notifications",
        "organizations",
        "orgs",
        "pricing",
        "pulls",
        "readme",
        "search",
        "security",
        "settings",
        "signup",
        "site",
        "sponsors",
        "stars",
        "support",
        "topics",
        "trending",
        "watching",
        "wiki",
    }
)
_GITHUB_REPO_RESERVED = frozenset(
    {
        "actions",
        "blob",
        "branches",
        "commits",
        "compare",
        "discussions",
        "issues",
        "network",
        "projects",
        "pulse",
        "raw",
        "releases",
        "security",
        "stargazers",
        "tags",
        "tree",
        "watchers",
        "wiki",
    }
)

# venue 线索：按"越具体越优先"的顺序匹配
_VENUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"accepted\s+(?:to|at|in|for)\s+(?P<v>[^.;\n]{2,80})", re.IGNORECASE),
    re.compile(r"to\s+appear\s+(?:in|at)\s+(?P<v>[^.;\n]{2,80})", re.IGNORECASE),
    re.compile(r"published\s+(?:in|at)\s+(?P<v>[^.;\n]{2,80})", re.IGNORECASE),
    re.compile(r"camera[- ]ready\s+(?:for|version\s+for)\s+(?P<v>[^.;\n]{2,80})", re.IGNORECASE),
    re.compile(r"in\s+proceedings\s+of\s+(?:the\s+)?(?P<v>[^.;\n]{2,80})", re.IGNORECASE),
    re.compile(
        r"(?P<v>[A-Z][A-Za-z0-9&'\- ]{1,60}\s(?:19|20)\d{2})\s*(?:camera[- ]ready|proceedings)",
        re.IGNORECASE,
    ),
)
# 明显不是 venue 的短语（避免把 "Accepted to appear" 之类的残句当作 venue）
_VENUE_REJECTS = frozenset(
    {"appear", "be published", "the", "a", "an", "to", "in", "at", "appear in", "camera ready"}
)


# --------------------------------------------------------------------------------------
# 数据形状
# --------------------------------------------------------------------------------------
@dataclass
class ArxivPaper:
    """一篇 arXiv 论文的结构化表示（全部字段来自真实响应，缺项为 None）。"""

    arxiv_id: str
    title: str
    abstract: str | None = None
    authors: list[dict[str, Any]] = field(default_factory=list)
    published: date | None = None
    updated: date | None = None
    pdf_url: str | None = None
    abs_url: str | None = None
    version: str | None = None
    comment: str | None = None
    journal_ref: str | None = None
    doi: str | None = None
    primary_category: str | None = None
    categories: list[str] = field(default_factory=list)
    venue_hint: str | None = None
    code_url: str | None = None
    request_url: str | None = None
    http_status: int | None = None

    @property
    def external_id(self) -> str:
        return self.arxiv_id

    def identifiers(self) -> dict[str, str]:
        """可用于身份映射的标识集合（不含 title_hash，由 identity.py 计算）。"""
        ids = {"arxiv": self.arxiv_id}
        if self.doi:
            ids["doi"] = self.doi
        return ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": SOURCE_NAME,
            "external_id": self.external_id,
            "arxiv_id": self.arxiv_id,
            "version": self.version,
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors,
            "published_at": self.published.isoformat() if self.published else None,
            "updated_at_src": self.updated.isoformat() if self.updated else None,
            "pdf_url": self.pdf_url,
            "abs_url": self.abs_url,
            "comment": self.comment,
            "journal_ref": self.journal_ref,
            "doi": self.doi,
            "primary_category": self.primary_category,
            "categories": self.categories,
            "venue_hint": self.venue_hint,
            "code_url": self.code_url,
        }

    def raw_payload(self) -> dict[str, Any]:
        """写入 ``papers.raw['arxiv']`` 的形状。

        ``/papers/search?field=cs.CL`` 与 WP04 的领域筛选都按
        ``raw['arxiv']['primary_category']`` / ``['categories']`` 取值，
        因此这里必须把分类一并落库。
        """
        return {
            "arxiv_id": self.arxiv_id,
            "version": self.version,
            "title": self.title,
            "abstract": self.abstract,
            "comment": self.comment,
            "journal_ref": self.journal_ref,
            "doi": self.doi,
            "primary_category": self.primary_category,
            "categories": self.categories,
            "published": self.published.isoformat() if self.published else None,
            "updated": self.updated.isoformat() if self.updated else None,
            "pdf_url": self.pdf_url,
            "abs_url": self.abs_url,
            "venue_hint": self.venue_hint,
            "code_url": self.code_url,
        }

    def to_meta(self) -> dict[str, Any]:
        """转换为 ``identity.fetch_and_upsert_paper`` 期望的归一 meta。"""
        return {
            "source": SOURCE_NAME,
            "external_id": self.external_id,
            "identifiers": self.identifiers(),
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors,
            "published_at": self.published,
            "updated_at_src": self.updated,
            "pdf_url": self.pdf_url,
            "code_url": self.code_url,
            "venue": self.venue_hint,
            "venue_source": "arxiv_comment" if self.venue_hint else None,
            "raw": self.raw_payload(),
        }


@dataclass
class ArxivFeedMeta:
    """Atom feed 的 ``opensearch`` 元信息（分页用）。"""

    total_results: int | None = None
    start_index: int | None = None
    items_per_page: int | None = None
    updated: datetime | None = None


# --------------------------------------------------------------------------------------
# 文本与正则工具（供 A5 验收）
# --------------------------------------------------------------------------------------
def normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_arxiv_id(raw: Any) -> tuple[str | None, str | None]:
    """从 ``http://arxiv.org/abs/2409.12345v2`` 之类的串里取出 ``(id, version)``。"""
    if not raw:
        return None, None
    match = _ARXIV_ID_RE.search(str(raw))
    if match is None:
        return None, None
    return match.group("id"), match.group("version")


def extract_github_url(text: Any) -> str | None:
    """从任意文本抽取规范化后的 GitHub 仓库地址；抽不到返回 None。"""
    if not text:
        return None
    for match in _GITHUB_RE.finditer(str(text)):
        owner = match.group("owner")
        repo = re.sub(r"\.git$", "", match.group("repo")).strip(".,;:)]}\"'`")
        if not owner or not repo:
            continue
        if owner.lower() in _GITHUB_RESERVED or repo.lower() in _GITHUB_REPO_RESERVED:
            continue
        if owner.lower() in _GITHUB_REPO_RESERVED:
            continue
        return f"https://github.com/{owner}/{repo}"
    return None


def extract_venue_hint(text: Any) -> str | None:
    """从 comment / abstract 抽取 venue **原始线索串**（不做等级映射，不猜测）。"""
    if not text:
        return None
    raw = normalize_whitespace(text)
    for pattern in _VENUE_PATTERNS:
        match = pattern.search(raw)
        if match is None:
            continue
        candidate = normalize_whitespace(match.group("v")).strip(" ,.;:-")
        if not candidate:
            continue
        if candidate.lower() in _VENUE_REJECTS:
            continue
        # 去掉尾部的 "camera-ready" / "8 pages" 之类噪音
        candidate = re.sub(
            r"\s*(?:camera[- ]ready|preprint|paper)\s*$", "", candidate, flags=re.IGNORECASE
        )
        candidate = candidate.strip(" ,.;:-")
        if len(candidate) < 2:
            continue
        return candidate[:VENUE_MAX_LEN]
    return None


def parse_comment(comment: Any) -> dict[str, str | None]:
    """解析 ``arXiv:comment``：返回 ``{venue_hint, code_url}``（缺项为 None）。

    实测样本（2026-09-17）：

    - ``Website: ..., Code: https://github.com/owner/repo`` → ``code_url`` 命中；
    - ``Accepted to SLT 2026. 8 pages`` → ``venue_hint='SLT 2026'``。
    """
    text = normalize_whitespace(comment) or None
    return {
        "venue_hint": extract_venue_hint(text),
        "code_url": extract_github_url(text),
    }


# --------------------------------------------------------------------------------------
# 检索式与 Atom 解析
# --------------------------------------------------------------------------------------
def parse_fields(raw: str | Sequence[str] | None = None) -> list[str]:
    """解析 ``ARXIV_FIELDS``（``cs.AI,cs.CL``）为列表。"""
    if raw is None:
        raw = get_settings().arxiv_fields
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    else:
        items = [str(part).strip() for part in raw]
    return [item for item in items if item]


def build_search_query(
    fields: Sequence[str] | None = None,
    *,
    query: str | None = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
) -> str:
    """构造 ``search_query``：分类 OR 组合 + 可选关键词 + 可选 ``submittedDate`` 区间。"""
    categories = list(fields or parse_fields())
    parts: list[str] = []
    if categories:
        cat_expr = " OR ".join(f"cat:{cat}" for cat in categories)
        parts.append(f"({cat_expr})" if len(categories) > 1 else cat_expr)
    if query:
        parts.append(f'all:"{normalize_whitespace(query)}"')
    window = _submitted_date_window(date_from, date_to)
    if window:
        parts.append(f"submittedDate:[{window}]")
    return " AND ".join(parts) if parts else "all:*"


def _submitted_date_window(
    date_from: date | datetime | str | None, date_to: date | datetime | str | None
) -> str | None:
    if not date_from and not date_to:
        return None
    start = _to_arxiv_timestamp(date_from) or "199107010000"
    end = _to_arxiv_timestamp(date_to) or _to_arxiv_timestamp(datetime.now(UTC)) or "203012310000"
    return f"{start} TO {end}"


def _to_arxiv_timestamp(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime("%Y%m%d%H%M")
    if isinstance(value, date):
        return value.strftime("%Y%m%d") + "0000"
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{12}", text):
        return text
    if re.fullmatch(r"\d{8}", text):
        return text + "0000"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC).strftime("%Y%m%d%H%M")
    return None


def _parse_atom_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def parse_feed(
    xml_text: str, *, request_url: str | None = None, http_status: int | None = None
) -> tuple[list[ArxivPaper], ArxivFeedMeta]:
    """解析 arXiv Atom feed；XML 非法时抛 ``ValueError``（由上层转为失败留痕）。"""
    if not xml_text or not xml_text.strip():
        raise ValueError("empty arXiv response")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:  # pragma: no cover - 极端情况
        raise ValueError(f"invalid atom xml: {exc}") from exc

    meta = ArxivFeedMeta(
        # opensearch 命名空间（http://a9.com/-/spec/opensearch/1.1/），不是 Atom 命名空间
        total_results=_to_int(root.findtext(f"{_OPENSEARCH}totalResults")),
        start_index=_to_int(root.findtext(f"{_OPENSEARCH}startIndex")),
        items_per_page=_to_int(root.findtext(f"{_OPENSEARCH}itemsPerPage")),
        updated=_parse_atom_datetime(root.findtext(f"{_ATOM}updated")),
    )

    papers: list[ArxivPaper] = []
    for entry in root.findall(f"{_ATOM}entry"):
        paper = _parse_entry(entry, request_url=request_url, http_status=http_status)
        if paper is not None:
            papers.append(paper)
    logger.info(
        "arxiv_feed_parsed entries=%d total=%s start=%s url=%s",
        len(papers),
        meta.total_results,
        meta.start_index,
        request_url,
    )
    return papers, meta


def _parse_entry(
    entry: ET.Element, *, request_url: str | None, http_status: int | None
) -> ArxivPaper | None:
    raw_id = entry.findtext(f"{_ATOM}id")
    arxiv_id, version = parse_arxiv_id(raw_id)
    title = normalize_whitespace(entry.findtext(f"{_ATOM}title"))
    if arxiv_id is None or not title:
        logger.debug("arxiv_entry_skipped id=%s title=%r", raw_id, title)
        return None

    abstract = normalize_whitespace(entry.findtext(f"{_ATOM}summary")) or None
    comment = normalize_whitespace(entry.findtext(f"{_ARXIV}comment")) or None
    journal_ref = normalize_whitespace(entry.findtext(f"{_ARXIV}journal_ref")) or None
    doi = normalize_whitespace(entry.findtext(f"{_ARXIV}doi")) or None

    authors: list[dict[str, Any]] = []
    for author in entry.findall(f"{_ATOM}author"):
        name = normalize_whitespace(author.findtext(f"{_ATOM}name"))
        if not name:
            continue
        affiliation = normalize_whitespace(author.findtext(f"{_ARXIV}affiliation")) or None
        authors.append({"name": name, "affiliation": affiliation, "institution_id": None})

    categories = [c.get("term") for c in entry.findall(f"{_ATOM}category") if c.get("term")]
    primary = entry.find(f"{_ARXIV}primary_category")
    primary_category = (
        primary.get("term") if primary is not None else (categories[0] if categories else None)
    )

    abs_url = None
    pdf_url = None
    for link in entry.findall(f"{_ATOM}link"):
        rel = link.get("rel")
        href = link.get("href")
        if not href:
            continue
        title_attr = (link.get("title") or "").lower()
        if rel == "alternate":
            abs_url = href
        elif title_attr == "pdf" or link.get("type") == "application/pdf":
            pdf_url = href
    if pdf_url is None and arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    published = _parse_atom_datetime(entry.findtext(f"{_ATOM}published"))
    updated = _parse_atom_datetime(entry.findtext(f"{_ATOM}updated"))
    extracted = parse_comment(comment)
    # comment 里没有代码链接时，再退回摘要里找（同样只抽原文，不猜测）
    code_url = extracted["code_url"] or extract_github_url(abstract)

    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        authors=authors,
        published=published.date() if published else None,
        updated=updated.date() if updated else None,
        pdf_url=pdf_url,
        abs_url=abs_url,
        version=version,
        comment=comment,
        journal_ref=journal_ref,
        doi=doi,
        primary_category=primary_category,
        categories=list(categories),
        venue_hint=extracted["venue_hint"],
        code_url=code_url,
        request_url=request_url,
        http_status=http_status,
    )


def _to_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# 网络调用
# --------------------------------------------------------------------------------------
async def search(
    *,
    fields: Sequence[str] | None = None,
    query: str | None = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    start: int = 0,
    max_results: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "submittedDate",
    sort_order: str = "descending",
    use_cache: bool = True,
) -> tuple[list[ArxivPaper], ArxivFeedMeta, HttpResult]:
    """单次检索；返回 ``(papers, feed_meta, http_result)``，失败时 ``papers == []``。"""
    settings = get_settings()
    base = clean_env_value(settings.arxiv_api_base) or "https://export.arxiv.org/api/query"
    params = {
        "search_query": build_search_query(
            fields, query=query, date_from=date_from, date_to=date_to
        ),
        "start": max(0, int(start)),
        "max_results": max(1, int(max_results)),
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    client = get_source_http(
        SOURCE_NAME,
        qps=ARXIV_QPS,
        # arXiv 在限流/风控时会回 **406 Not Acceptable**（不是 429），默认重试集合不含它，
        # 一次失败就整轮零产出。这里补齐 406 并把重试提到 3 次、退避放宽，
        # 全失败时仍如实返回最后一次的 http_status（不假装成功）。
        max_retries=3,
        retry_statuses=(406, 408, 425, 429, 500, 502, 503, 504),
        backoff_base=1.0,
        backoff_max=8.0,
    )
    # arXiv 返回 Atom XML 而非 JSON：expect_json=False，2xx 即成功
    result = await client.get_json(base, params, use_cache=use_cache, expect_json=False)
    if not result.ok:
        return [], ArxivFeedMeta(), result
    try:
        papers, meta = parse_feed(
            result.text, request_url=result.request_url, http_status=result.status_code
        )
    except ValueError as exc:
        logger.warning("arxiv_parse_failed url=%s error=%s", result.request_url, exc)
        result.error = str(exc)
        result.error_kind = "parse"
        return [], ArxivFeedMeta(), result
    # 统一回填请求留痕（缓存命中时 status 来自缓存条目）
    for paper in papers:
        paper.request_url = result.request_url
        paper.http_status = result.status_code
    return papers, meta, result


async def iter_papers(
    *,
    fields: Sequence[str] | None = None,
    query: str | None = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort_by: str = "submittedDate",
) -> tuple[list[ArxivPaper], list[HttpResult]]:
    """分页抓取直到满足 ``limit`` 或 feed 耗尽；返回 ``(papers, http_results)``。"""
    page_size = max(1, min(int(page_size), 2000))
    collected: list[ArxivPaper] = []
    results: list[HttpResult] = []
    start = 0
    seen: set[str] = set()
    while len(collected) < limit:
        page_limit = min(page_size, limit - len(collected))
        papers, meta, result = await search(
            fields=fields,
            query=query,
            date_from=date_from,
            date_to=date_to,
            start=start,
            max_results=page_limit,
            sort_by=sort_by,
        )
        results.append(result)
        if not result.ok:
            break
        fresh = [p for p in papers if p.arxiv_id not in seen]
        for paper in fresh:
            seen.add(paper.arxiv_id)
        collected.extend(fresh)
        if not papers:
            break
        total = meta.total_results or 0
        start += len(papers)
        if start >= total:
            break
        if len(papers) < page_limit:
            break
    logger.info(
        "arxiv_iter_done collected=%d limit=%d requests=%d", len(collected), limit, len(results)
    )
    return collected[:limit], results


async def fetch_recent(
    *,
    fields: Sequence[str] | None = None,
    date_from: date | datetime | str | None = None,
    limit: int = 100,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[ArxivPaper], list[HttpResult]]:
    """按 ``submittedDate`` 倒序增量抓取（``date_from`` 之后提交的论文）。"""
    return await iter_papers(
        fields=fields,
        date_from=date_from,
        limit=limit,
        page_size=page_size,
        sort_by="submittedDate",
    )


__all__ = [
    "ATOM_NS",
    "ARXIV_NS",
    "OPENSEARCH_NS",
    "ArxivFeedMeta",
    "ArxivPaper",
    "DEFAULT_PAGE_SIZE",
    "EXTRACT",
    "SOURCE_NAME",
    "build_search_query",
    "extract_github_url",
    "extract_venue_hint",
    "fetch_recent",
    "iter_papers",
    "normalize_whitespace",
    "parse_arxiv_id",
    "parse_comment",
    "parse_feed",
    "parse_fields",
    "search",
]

# 便于调用方一次性拿到两个抽取器（A5 验收常用）
EXTRACT = {"venue_hint": extract_venue_hint, "code_url": extract_github_url}
