"""Shared HTTP and parsing helpers for engine adapters."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from ..config import SearchSettings
from ..models import SearchRequest, SearchResult


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def tag_text(fragment: str) -> str:
    return clean(re.sub(r"<[^>]+>", " ", fragment))


def absolute_url(value: str, base: str) -> str:
    return urljoin(base, html.unescape(value or "").strip())


async def get_json(request: SearchRequest, url: str, params: dict[str, Any], settings: SearchSettings, client: httpx.AsyncClient) -> Any:
    response = await client.get(url, params=params, headers={"User-Agent": settings.user_agent})
    response.raise_for_status()
    return response.json()


async def get_text(request: SearchRequest, url: str, params: dict[str, Any], settings: SearchSettings, client: httpx.AsyncClient) -> str:
    response = await client.get(url, params=params, headers={"User-Agent": settings.user_agent})
    response.raise_for_status()
    return response.text


def html_results(text: str, pattern: str, base: str, *, source: str, engine: str, category: str) -> list[SearchResult]:
    hits: list[SearchResult] = []
    for match in re.finditer(pattern, text, re.I | re.S):
        groups = match.groupdict()
        url = absolute_url(groups.get("url", ""), base)
        title = tag_text(groups.get("title", ""))
        snippet = tag_text(groups.get("snippet", ""))
        if url and title:
            hits.append(SearchResult(title=title, url=url, snippet=snippet[:600], source=source, engine=engine, category=category))
    return hits
