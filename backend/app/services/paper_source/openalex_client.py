# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""OpenAlex 兜底客户端（WP03-T3）。

为什么需要它
------------
实测（2026-09-17）：Semantic Scholar **无 key 返回 429**，而 OpenAlex **免 key 可用**，
因此 OpenAlex 是"引用数 / venue / 机构 / 年度引用曲线"的免费主力备份源。
S2 不可用时上层自动降级到本客户端，**降级后引用数仍必须取得到**（验收 WP03-A2）。

硬约束
------
1. 每个请求**必须带** ``mailto``（``OPENALEX_MAILTO``，友好通道）；未配置时直接抛
   :class:`OpenAlexUnavailable`，不发请求、不编造数据。
2. 抽取 ``cited_by_count / publication_year / primary_location.source.display_name /
   authorships.institutions / counts_by_year``。
3. ``counts_by_year`` 只**原样取回**并写入 ``papers.raw['openalex']``，趋势计算归 WP04。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from typing import Any

from app.core.config import get_settings
from app.services.paper_source.cache import (
    HttpResult,
    clean_env_value,
    get_source_http,
    safe_float,
)

logger = logging.getLogger("sciloop.wp03.openalex")

SOURCE_NAME = "openalex"
OPENALEX_BASE_URL = "https://api.openalex.org"
# 实测（2026-09-17）：带占位 mailto 时进的是"公共池"，突发 >1 req/s 即 429
# （响应头 X-RateLimit-Limit=1000/天、Remaining 充足，说明是速率而非配额问题），
# 因此这里取 1 QPS；429 由上层断路器 + 冷却重试兜住。
OPENALEX_QPS = 1.0
TITLE_MATCH_THRESHOLD = 0.90
ARXIV_DOI_PREFIX = "10.48550/arxiv."

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
# OpenAlex 的 search 参数对部分标点会直接返回 400（实测 title 里的 "?" 会 400），
# 因此检索前先做减法：只保留字母/数字/空格/连字符/撇号，其余丢弃。
_SEARCH_KEEP_RE = re.compile(r"[^\w\s'\-]", re.UNICODE)
SEARCH_TERM_MAX_LEN = 200


def sanitize_search_term(value: Any) -> str:
    """清洗用于 OpenAlex ``search`` 的标题串（去标点、压空白、限长）。"""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = _SEARCH_KEEP_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SEARCH_TERM_MAX_LEN]


class OpenAlexUnavailable(RuntimeError):
    """OpenAlex 不可用（缺 mailto / 限流 / 5xx / 网络故障 / 请求被拒）。"""

    # 真正的"源级故障"：只有这些原因才值得打开断路器（本批次不再调用该源）
    OUTAGE_REASONS: frozenset[str] = frozenset(
        {
            "missing_mailto",
            "rate_limited",
            "server_error",
            "network_error",
            "unavailable",
            "invalid_payload",
        }
    )

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        attempts: int = 0,
        request_url: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.reason = reason
        self.status_code = status_code
        self.attempts = attempts
        self.request_url = request_url
        self.detail = detail
        super().__init__(
            f"OpenAlexUnavailable(reason={reason}, http_status={status_code}, attempts={attempts})"
            + (f" detail={detail}" if detail else "")
        )

    @property
    def is_source_outage(self) -> bool:
        """单条请求被拒（如 400 参数问题）**不算**源级故障，不应熔断整个源。"""
        return self.reason in self.OUTAGE_REASONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "http_status": self.status_code,
            "attempts": self.attempts,
            "request_url": self.request_url,
            "detail": self.detail,
            "is_source_outage": self.is_source_outage,
        }


@dataclass
class OpenAlexWork:
    """OpenAlex work（归一后，缺项一律 None）。"""

    openalex_id: str | None
    doi: str | None = None
    title: str | None = None
    publication_year: int | None = None
    publication_date: date | None = None
    cited_by_count: int | None = None
    venue: str | None = None
    venue_type: str | None = None
    institutions: list[dict[str, Any]] = field(default_factory=list)
    authors: list[dict[str, Any]] = field(default_factory=list)
    counts_by_year: list[dict[str, int]] = field(default_factory=list)
    work_type: str | None = None
    is_open_access: bool | None = None
    open_access_url: str | None = None
    referenced_works_count: int | None = None
    matched_by: str | None = None
    title_similarity: float | None = None
    request_url: str | None = None
    http_status: int | None = None

    def identifiers(self) -> dict[str, str]:
        ids: dict[str, str] = {}
        if self.openalex_id:
            ids["openalex"] = self.openalex_id
        if self.doi:
            ids["doi"] = self.doi
        if self.doi and self.doi.lower().startswith(ARXIV_DOI_PREFIX):
            ids["arxiv"] = self.doi[len(ARXIV_DOI_PREFIX) :]
        return ids

    def to_meta(self) -> dict[str, Any]:
        """转换为 ``identity`` 期望的归一 meta。"""
        return {
            "source": SOURCE_NAME,
            "external_id": self.openalex_id or self.doi or "",
            "identifiers": self.identifiers(),
            "title": self.title,
            "authors": self.authors or None,
            "published_at": self.publication_date,
            "venue": self.venue,
            "venue_source": "openalex" if self.venue else None,
            "citation_count": self.cited_by_count,
        }

    def raw_payload(self) -> dict[str, Any]:
        """写入 ``papers.raw['openalex']`` 的形状（WP04 的 ranking 直接按此读）。"""
        return {
            "id": self.openalex_id,
            "doi": self.doi,
            "title": self.title,
            "publication_year": self.publication_year,
            "publication_date": (
                self.publication_date.isoformat() if self.publication_date else None
            ),
            "cited_by_count": self.cited_by_count,
            "venue": self.venue,
            "venue_type": self.venue_type,
            "institutions": self.institutions,
            "counts_by_year": self.counts_by_year,
            "type": self.work_type,
            "is_open_access": self.is_open_access,
            "open_access_url": self.open_access_url,
            "referenced_works_count": self.referenced_works_count,
            "matched_by": self.matched_by,
            "title_similarity": self.title_similarity,
            "request_url": self.request_url,
            "http_status": self.http_status,
        }


# --------------------------------------------------------------------------------------
# 解析工具
# --------------------------------------------------------------------------------------
def normalize_title_for_match(value: Any) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", str(value).lower())).strip()


def title_similarity(left: Any, right: Any) -> float:
    """标题相似度（用于确认按标题检索到的结果确实是同一篇论文，避免张冠李戴）。"""
    a, b = normalize_title_for_match(left), normalize_title_for_match(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
    return text.strip() or None


def arxiv_doi(arxiv_id: str) -> str:
    """arXiv 自 2022 起为每篇论文注册 DataCite DOI：``10.48550/arXiv.<id>``。"""
    clean = re.sub(r"v\d+$", "", str(arxiv_id).strip())
    return f"10.48550/arxiv.{clean}"


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:7], "%Y-%m").date().replace(day=1)
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:4], "%Y").date().replace(month=1, day=1)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_institutions(authorships: Any) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    if not isinstance(authorships, Sequence):
        return []
    for authorship in authorships:
        if not isinstance(authorship, Mapping):
            continue
        for inst in authorship.get("institutions") or []:
            if not isinstance(inst, Mapping):
                continue
            name = inst.get("display_name") or inst.get("name")
            if not name:
                continue
            key = str(inst.get("id") or name)
            seen.setdefault(
                key,
                {
                    "name": str(name),
                    "ror": inst.get("ror"),
                    "country_code": inst.get("country_code"),
                    "type": inst.get("type"),
                    "openalex_id": inst.get("id"),
                },
            )
    return list(seen.values())


def _extract_authors(authorships: Any) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = []
    if not isinstance(authorships, Sequence):
        return authors
    for authorship in authorships:
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author") or {}
        name = (
            author.get("display_name") or author.get("name")
            if isinstance(author, Mapping)
            else None
        )
        if not name:
            continue
        institutions = _extract_institutions([authorship])
        authors.append(
            {
                "name": str(name),
                "affiliation": institutions[0]["name"] if institutions else None,
                "institution_id": institutions[0]["openalex_id"] if institutions else None,
                "openalex_author_id": author.get("id") if isinstance(author, Mapping) else None,
            }
        )
    return authors


def normalize_work(payload: Mapping[str, Any]) -> OpenAlexWork:
    """把 OpenAlex work JSON 归一为 :class:`OpenAlexWork`。"""
    primary_location = payload.get("primary_location") or {}
    if not isinstance(primary_location, Mapping):
        primary_location = {}
    source = primary_location.get("source") or {}
    if not isinstance(source, Mapping):
        source = {}
    open_access = payload.get("open_access") or {}
    if not isinstance(open_access, Mapping):
        open_access = {}
    authorships = payload.get("authorships") or []

    counts: list[dict[str, int]] = []
    for item in payload.get("counts_by_year") or []:
        if not isinstance(item, Mapping):
            continue
        year, cited = _to_int(item.get("year")), _to_int(item.get("cited_by_count"))
        if year is None or cited is None:
            continue
        counts.append({"year": year, "cited_by_count": cited})
    counts.sort(key=lambda row: row["year"])

    return OpenAlexWork(
        openalex_id=payload.get("id"),
        doi=normalize_doi(payload.get("doi")),
        title=payload.get("title") or payload.get("display_name"),
        publication_year=_to_int(payload.get("publication_year")),
        publication_date=_parse_date(
            payload.get("publication_date") or payload.get("publication_year")
        ),
        cited_by_count=_to_int(payload.get("cited_by_count")),
        venue=(source.get("display_name") or None),
        venue_type=(source.get("type") or None),
        institutions=_extract_institutions(authorships),
        authors=_extract_authors(authorships),
        counts_by_year=counts,
        work_type=payload.get("type"),
        is_open_access=open_access.get("is_oa") if "is_oa" in open_access else None,
        open_access_url=open_access.get("oa_url"),
        referenced_works_count=_to_int(payload.get("referenced_works_count")),
    )


class OpenAlexClient:
    """OpenAlex Works API 客户端（必须带 mailto）。"""

    def __init__(
        self,
        *,
        mailto: str | None = None,
        base_url: str | None = None,
        qps: float | None = None,
        max_retries: int = 2,
        timeout_seconds: float | None = None,
        http: Any | None = None,
    ) -> None:
        settings = get_settings()
        # clean_env_value：防御 .env 行尾注释被 compose 当成值
        self.mailto = clean_env_value(mailto if mailto is not None else settings.openalex_mailto)
        self.base_url = (
            clean_env_value(base_url)
            or clean_env_value(settings.openalex_api_base)
            or OPENALEX_BASE_URL
        ).rstrip("/")
        self.max_retries = max(0, int(max_retries))
        self._http = http or get_source_http(
            SOURCE_NAME,
            headers={"Accept": "application/json"},
            qps=safe_float(qps, OPENALEX_QPS),
            timeout_seconds=timeout_seconds,
        )
        # 最近一次请求的留痕（含"查不到/被拒"的情况）：供上层写 source_records 用
        self.last_lookup: dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        return bool(self.mailto)

    def _record_lookup(self, *, kind: str, result: HttpResult, matched_by: str | None) -> None:
        self.last_lookup = {
            "kind": kind,
            "request_url": result.request_url,
            "http_status": result.status_code,
            "matched_by": matched_by,
            "headers": dict(result.headers),
            "at": datetime.now(UTC).isoformat(),
        }

    def _params(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self.mailto:
            raise OpenAlexUnavailable(
                "missing_mailto",
                detail="OPENALEX_MAILTO 未配置：OpenAlex 请求必须带 mailto（友好通道）",
            )
        params: dict[str, Any] = {"mailto": self.mailto}
        params.update({k: v for k, v in (extra or {}).items() if v is not None})
        return params

    # ---------------------------------------------------------------- 取数入口
    async def get_work(
        self,
        *,
        openalex_id: str | None = None,
        doi: str | None = None,
        arxiv_id: str | None = None,
        title: str | None = None,
        use_cache: bool = True,
    ) -> OpenAlexWork | None:
        """按 OpenAlex ID / DOI / arXiv ID / 标题取单篇；不可用时抛异常。

        标题检索会做相似度校验（≥0.90），命中不了就返回 ``None``（宁缺勿错）。
        无论成功失败，``self.last_lookup`` 都记录最后一次真实请求，便于留痕。
        """
        if openalex_id:
            work = await self._get_by_openalex_id(openalex_id, use_cache=use_cache)
            if work is not None:
                work.matched_by = "openalex_id"
                return work
        if doi:
            work = await self._get_by_doi(normalize_doi(doi) or doi, use_cache=use_cache)
            if work is not None:
                work.matched_by = "doi"
                return work
        if arxiv_id:
            work = await self._get_by_doi(arxiv_doi(arxiv_id), use_cache=use_cache)
            if work is not None:
                work.matched_by = "arxiv_doi"
                return work
        if title:
            return await self.search_by_title(title, use_cache=use_cache)
        return None

    async def _get_by_openalex_id(
        self, openalex_id: str, *, use_cache: bool
    ) -> OpenAlexWork | None:
        work_id = str(openalex_id).rstrip("/").split("/")[-1]
        url = f"{self.base_url}/works/{work_id}"
        payload, result = await self._request(url, self._params(), use_cache=use_cache)
        self._record_lookup(
            kind="openalex_id",
            result=result,
            matched_by="openalex_id" if result.status_code != 404 else None,
        )
        if result.status_code == 404:
            return None
        return self._wrap(payload, result, matched_by="openalex_id")

    async def _get_by_doi(self, doi: str, *, use_cache: bool) -> OpenAlexWork | None:
        url = f"{self.base_url}/works/doi:{doi}"
        payload, result = await self._request(url, self._params(), use_cache=use_cache)
        self._record_lookup(
            kind="doi", result=result, matched_by="doi" if result.status_code != 404 else None
        )
        if result.status_code == 404:
            return None
        return self._wrap(payload, result, matched_by="doi")

    async def search_by_title(self, title: str, *, use_cache: bool = True) -> OpenAlexWork | None:
        """按标题检索并做相似度校验，命中返回 work，否则 ``None``。"""
        term = sanitize_search_term(title)
        if not term:
            logger.info("openalex_title_skip reason=empty_after_sanitize title=%r", title)
            return None
        url = f"{self.base_url}/works"
        params = self._params({"search": term, "per_page": 5})
        payload, result = await self._request(url, params, use_cache=use_cache)
        if not isinstance(payload, Mapping):
            self._record_lookup(kind="title", result=result, matched_by=None)
            return None
        candidates = payload.get("results") or []
        best: tuple[float, Mapping[str, Any]] | None = None
        for item in candidates:
            if not isinstance(item, Mapping):
                continue
            score = title_similarity(title, item.get("title") or item.get("display_name"))
            if best is None or score > best[0]:
                best = (score, item)
        if best is None or best[0] < TITLE_MATCH_THRESHOLD:
            logger.info(
                "openalex_title_miss query=%r best=%s threshold=%.2f",
                term,
                None if best is None else round(best[0], 3),
                TITLE_MATCH_THRESHOLD,
            )
            self._record_lookup(kind="title", result=result, matched_by=None)
            return None
        work = normalize_work(best[1])
        work.matched_by = "title"
        work.title_similarity = round(best[0], 4)
        work.request_url = result.request_url
        work.http_status = result.status_code
        self._record_lookup(kind="title", result=result, matched_by="title")
        return work

    # ---------------------------------------------------------------- 底层请求
    async def _request(
        self, url: str, params: Mapping[str, Any], *, use_cache: bool
    ) -> tuple[Any | None, HttpResult]:
        result = await self._http.get_json(
            url,
            params,
            use_cache=use_cache,
            max_retries=self.max_retries,
            accept_statuses=frozenset({404}),
        )
        self.last_lookup = {
            "kind": "request",
            "request_url": result.request_url,
            "http_status": result.status_code,
            "matched_by": None,
            "headers": dict(result.headers),
            "at": datetime.now(UTC).isoformat(),
        }
        if result.status_code == 404:
            return None, result
        if not result.ok:
            raise OpenAlexUnavailable(
                self._reason(result),
                status_code=result.status_code,
                attempts=result.attempts,
                request_url=result.request_url,
                detail=result.error,
            )
        return result.payload, result

    @staticmethod
    def _reason(result: HttpResult) -> str:
        if result.status_code == 429:
            return "rate_limited"
        if result.status_code is not None and 500 <= result.status_code < 600:
            return "server_error"
        if result.error_kind in {"timeout", "transport", "http"}:
            return "network_error"
        if result.status_code is not None:
            return f"http_{result.status_code}"
        return "unavailable"

    def _wrap(self, payload: Any, result: HttpResult, *, matched_by: str) -> OpenAlexWork | None:
        if not isinstance(payload, Mapping):
            return None
        work = normalize_work(payload)
        work.matched_by = matched_by
        work.request_url = result.request_url
        work.http_status = result.status_code
        return work


__all__ = [
    "ARXIV_DOI_PREFIX",
    "OPENALEX_BASE_URL",
    "OPENALEX_QPS",
    "SOURCE_NAME",
    "TITLE_MATCH_THRESHOLD",
    "OpenAlexClient",
    "OpenAlexUnavailable",
    "OpenAlexWork",
    "arxiv_doi",
    "normalize_doi",
    "normalize_title_for_match",
    "normalize_work",
    "sanitize_search_term",
    "title_similarity",
]
