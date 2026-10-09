"""Stable models exchanged by the internal search engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True, frozen=True)
class SearchRequest:
    query: str
    categories: tuple[str, ...] = ("general", "science", "it")
    engines: tuple[str, ...] = ()
    language: str = "auto"
    time_range: str | None = None
    safesearch: int = 0
    page: int = 1
    limit: int = 8


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    engine: str = ""
    category: str = "general"
    score: float = 0.0
    published: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "engine": self.engine,
            "category": self.category,
            "score": round(float(self.score), 6),
            **({"published": self.published} if self.published else {}),
            **self.metadata,
        }


@dataclass(slots=True)
class EngineFailure:
    engine: str
    category: str
    reason: str
    detail: str
    status_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "engine": self.engine,
            "category": self.category,
            "reason": self.reason,
            "detail": self.detail[:240],
        }
        if self.status_code is not None:
            row["status_code"] = self.status_code
        return row


@dataclass(slots=True)
class SearchResponse:
    request: SearchRequest
    results: list[SearchResult]
    engines_used: list[str]
    failures: list[EngineFailure]
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return bool(self.results) or not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "query": self.request.query,
            "count": len(self.results),
            "results": [item.as_dict() for item in self.results],
            "sources_used": self.engines_used,
            "sources_failed": [item.as_dict() for item in self.failures],
            "categories": list(self.request.categories),
            "language": self.request.language,
            "page": self.request.page,
            "elapsed_ms": self.elapsed_ms,
        }
