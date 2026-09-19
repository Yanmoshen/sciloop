# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Semantic Scholar 客户端（WP03-T2）。

硬约束（计划书 §2.7.6 取数纪律）
--------------------------------
1. **必须带 key**：``SEMANTIC_SCHOLAR_API_KEY`` 经 ``x-api-key`` 头发送；
   实测无 key 时官方接口返回 **HTTP 429**，本客户端据此把原因标为
   ``missing_api_key``（异常里带真实 ``http_status``，留痕不说谎）。
2. **限流**：``SEMANTIC_SCHOLAR_QPS``（默认 1）经进程内 ``RateLimiter`` 串行节流。
3. **退避**：429 / 5xx / 传输层异常 → 指数退避重试，**最多 3 次尝试**；
   仍然失败则抛 :class:`S2Unavailable` 供上层**降级到 OpenAlex**（引用数仍要拿得到）。
4. ``fields`` 固定包含 ``citationCount / publicationDate / venue / fieldsOfStudy /
   externalIds``（外加 ``publicationVenue / authors / openAccessPdf`` 供交叉核对）。

本模块只负责"取"，不落库；留痕由 ``source_records.py`` 完成。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.config import get_settings
from app.services.paper_source.cache import (
    HttpResult,
    clean_env_value,
    get_source_http,
    safe_float,
)

logger = logging.getLogger("sciloop.wp03.s2")

SOURCE_NAME = "semantic_scholar"
S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
S2_MAX_ATTEMPTS = 3
# 位置参数化的 fields 列表（附录要求的最小集合 + 交叉核对字段）
S2_FIELDS = (
    "paperId",
    "title",
    "abstract",
    "venue",
    "publicationVenue",
    "publicationDate",
    "year",
    "citationCount",
    "influentialCitationCount",
    "referenceCount",
    "fieldsOfStudy",
    "externalIds",
    "authors",
    "openAccessPdf",
)


class S2Unavailable(RuntimeError):
    """S2 不可用（缺 key / 限流 / 5xx / 网络故障）——上层据此降级 OpenAlex。"""

    # 真正的"源级故障"：只有这些原因才值得打开断路器（本批次不再调用该源）
    OUTAGE_REASONS: frozenset[str] = frozenset(
        {
            "missing_api_key",
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
            f"S2Unavailable(reason={reason}, http_status={status_code}, attempts={attempts})"
            + (f" detail={detail}" if detail else "")
        )

    @property
    def is_source_outage(self) -> bool:
        """单条请求的 4xx（如 400 参数问题）**不算**源级故障，不应熔断整个源。"""
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
class S2Paper:
    """S2 论文条目（归一后）。"""

    s2_id: str | None
    title: str | None = None
    abstract: str | None = None
    venue: str | None = None
    publication_date: date | None = None
    year: int | None = None
    citation_count: int | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    fields_of_study: list[str] = field(default_factory=list)
    external_ids: dict[str, Any] = field(default_factory=dict)
    authors: list[dict[str, Any]] = field(default_factory=list)
    open_access_pdf: str | None = None
    request_url: str | None = None
    http_status: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def identifiers(self) -> dict[str, str]:
        ids: dict[str, str] = {}
        if self.s2_id:
            ids["semantic_scholar"] = self.s2_id
        doi = self.external_ids.get("DOI")
        if doi:
            ids["doi"] = str(doi)
        arxiv = self.external_ids.get("ArXiv")
        if arxiv:
            ids["arxiv"] = str(arxiv)
        return ids

    def to_meta(self) -> dict[str, Any]:
        """转换为 ``identity`` 期望的归一 meta（``citation_count`` 为真实值或 None）。"""
        return {
            "source": SOURCE_NAME,
            "external_id": self.s2_id or "",
            "identifiers": self.identifiers(),
            "title": self.title,
            "abstract": self.abstract,
            "authors": self.authors or None,
            "published_at": self.publication_date,
            "venue": self.venue,
            "venue_source": "s2" if self.venue else None,
            "citation_count": self.citation_count,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "s2_id": self.s2_id,
            "title": self.title,
            "venue": self.venue,
            "publication_date": (
                self.publication_date.isoformat() if self.publication_date else None
            ),
            "year": self.year,
            "citation_count": self.citation_count,
            "influential_citation_count": self.influential_citation_count,
            "reference_count": self.reference_count,
            "fields_of_study": self.fields_of_study,
            "external_ids": self.external_ids,
            "open_access_pdf": self.open_access_pdf,
            "authors": self.authors,
        }


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class S2Client:
    """Semantic Scholar Graph API 客户端。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = S2_BASE_URL,
        qps: float | None = None,
        max_retries: int = S2_MAX_ATTEMPTS - 1,
        timeout_seconds: float | None = None,
        http: Any | None = None,
    ) -> None:
        settings = get_settings()
        # clean_env_value：防御 .env 行尾注释被 compose 当成值（实测会污染成中文注释）
        self.api_key = clean_env_value(
            api_key if api_key is not None else settings.semantic_scholar_api_key
        )
        self.base_url = base_url.rstrip("/")
        self.qps = safe_float(qps if qps is not None else settings.semantic_scholar_qps, 1.0)
        self.max_retries = max(0, int(max_retries))
        # 缺 key 时**仍然发一次真实请求**：官方此时回 429，我们据此留下真实的
        # http_status 与可识别原因（比不请求、只在日志里猜更可审计）。
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        self._http = http or get_source_http(
            SOURCE_NAME,
            headers={"Accept": "application/json"},
            qps=self.qps,
            timeout_seconds=timeout_seconds,
        )
        if not self.api_key:
            logger.warning(
                "S2 key 缺失（SEMANTIC_SCHOLAR_API_KEY 为空）：请求预计返回 429，"
                "本批次将自动降级 OpenAlex 取引用数"
            )
        # 最近一次请求的留痕（含"查不到/被拒"的情况）：供上层写 source_records 用
        self.last_lookup: dict[str, Any] = {}

    # ---------------------------------------------------------------- 属性
    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    def _describe_failure(self, result: HttpResult) -> tuple[str, str | None]:
        """把 HTTP 结果映射为 ``(reason, detail)``。"""
        if result.status_code == 429:
            if not self.api_key:
                return "missing_api_key", (
                    "SEMANTIC_SCHOLAR_API_KEY 未配置：官方接口对匿名调用返回 429"
                )
            return "rate_limited", "429 Too Many Requests（已指数退避重试后仍失败）"
        if result.status_code is not None and 500 <= result.status_code < 600:
            return "server_error", f"{result.status_code} server error"
        if result.error_kind in {"timeout", "transport", "http"}:
            return "network_error", result.error or result.error_kind
        if result.status_code is not None:
            return f"http_{result.status_code}", result.error
        return "unavailable", result.error

    # ---------------------------------------------------------------- 取数
    async def get_paper(
        self,
        *,
        paper_id: str | None = None,
        arxiv_id: str | None = None,
        doi: str | None = None,
        fields: tuple[str, ...] | list[str] | str | None = None,
        use_cache: bool = True,
    ) -> S2Paper | None:
        """按 S2 ID / arXiv ID / DOI 取单篇；**不可用时抛** :class:`S2Unavailable`。

        返回 ``None`` 表示请求成功但 S2 无此论文（此时不写任何数值）。
        """
        lookup = self._build_lookup_id(paper_id=paper_id, arxiv_id=arxiv_id, doi=doi)
        url = f"{self.base_url}/paper/{lookup}"
        field_list = fields if fields is not None else ",".join(S2_FIELDS)
        params: Mapping[str, Any] = {"fields": field_list}
        result = await self._http.get_json(
            url,
            params,
            headers=self._headers(),
            use_cache=use_cache,
            max_retries=self.max_retries,
            # 404（S2 无此论文）是**有效结论**，不应重试，也不应被当成"源不可用"
            accept_statuses=frozenset({404}),
        )
        self.last_lookup = {
            "lookup": lookup,
            "request_url": result.request_url,
            "http_status": result.status_code,
            "attempts": result.attempts,
            "from_cache": result.from_cache,
            "headers": dict(result.headers),
        }
        if result.status_code == 404:
            logger.info("s2_not_found lookup=%s url=%s", lookup, result.request_url)
            return None
        if not result.ok:
            reason, detail = self._describe_failure(result)
            raise S2Unavailable(
                reason,
                status_code=result.status_code,
                attempts=result.attempts,
                request_url=result.request_url,
                detail=detail,
            )
        if not isinstance(result.payload, dict):
            raise S2Unavailable(
                "invalid_payload",
                status_code=result.status_code,
                attempts=result.attempts,
                request_url=result.request_url,
                detail="响应不是 JSON 对象",
            )
        return self._normalize(result.payload, result=result)

    @staticmethod
    def _build_lookup_id(*, paper_id: str | None, arxiv_id: str | None, doi: str | None) -> str:
        if paper_id:
            return str(paper_id).strip()
        if arxiv_id:
            return f"ARXIV:{str(arxiv_id).strip()}"
        if doi:
            normalized = str(doi).strip()
            for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
                if normalized.lower().startswith(prefix):
                    normalized = normalized[len(prefix) :]
            return f"DOI:{normalized}"
        raise ValueError("S2Client.get_paper 需要 paper_id / arxiv_id / doi 之一")

    @staticmethod
    def _normalize(payload: Mapping[str, Any], *, result: HttpResult) -> S2Paper:
        external_ids = payload.get("externalIds") or {}
        if not isinstance(external_ids, Mapping):
            external_ids = {}
        authors = payload.get("authors") or []
        normalized_authors = [
            {
                "name": (a or {}).get("name"),
                "author_id": (a or {}).get("authorId"),
                "affiliation": None,
                "institution_id": None,
            }
            for a in authors
            if isinstance(a, Mapping) and (a or {}).get("name")
        ]
        pdf = (
            (payload.get("openAccessPdf") or {})
            if isinstance(payload.get("openAccessPdf"), Mapping)
            else {}
        )
        fields_of_study = payload.get("fieldsOfStudy") or []
        return S2Paper(
            s2_id=payload.get("paperId"),
            title=(payload.get("title") or None),
            abstract=(payload.get("abstract") or None),
            venue=(payload.get("venue") or None),
            publication_date=_parse_date(payload.get("publicationDate")),
            year=_to_int(payload.get("year")),
            citation_count=_to_int(payload.get("citationCount")),
            influential_citation_count=_to_int(payload.get("influentialCitationCount")),
            reference_count=_to_int(payload.get("referenceCount")),
            fields_of_study=[str(item) for item in fields_of_study if item],
            external_ids=dict(external_ids),
            authors=normalized_authors,
            open_access_pdf=(pdf.get("url") or None),
            request_url=result.request_url,
            http_status=result.status_code,
            raw=dict(payload),
        )


__all__ = [
    "S2_BASE_URL",
    "S2_FIELDS",
    "S2_MAX_ATTEMPTS",
    "S2Client",
    "S2Paper",
    "S2Unavailable",
    "SOURCE_NAME",
]
