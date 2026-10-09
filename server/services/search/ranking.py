"""Deterministic result normalization and ranking."""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urlsplit, urlunsplit

from .models import SearchResult


def canonical_url(url: str) -> str:
    parts = urlsplit(urldefrag(url)[0])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def rank(query: str, rows: list[SearchResult], limit: int) -> list[SearchResult]:
    terms = {term.lower() for term in re.findall(r"\w+", query) if len(term) > 2}
    for row in rows:
        text = f"{row.title} {row.snippet}".lower()
        lexical = sum(1 for term in terms if term in text) / max(1, len(terms))
        source_bonus = 0.05 if row.engine in {"arxiv", "crossref", "europepmc", "github"} else 0.0
        upstream = max(0.0, float(row.score or 0.0))
        normalized_upstream = upstream / (upstream + 100.0) if upstream else 0.0
        row.score = lexical * 0.65 + source_bonus + normalized_upstream * 0.3
    rows.sort(key=lambda item: (-item.score, item.title.lower(), item.url))
    return rows[:limit]


def deduplicate(rows: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = canonical_url(row.url)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique
