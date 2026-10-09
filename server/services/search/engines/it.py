"""Developer and software catalog adapters."""

from __future__ import annotations

from typing import Any

import httpx

from ..models import SearchRequest, SearchResult
from ..registry import register
from .base import clean, get_json


@register("github", label="GitHub", categories=("it",), shortcut="gh")
async def github(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    headers = {"User-Agent": settings.user_agent, "Accept": "application/vnd.github+json"}
    token = (getattr(settings, "github_token", "") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.get("https://api.github.com/search/repositories", params={"q": request.query, "per_page": request.limit, "page": request.page}, headers=headers)
    response.raise_for_status()
    return [SearchResult(title=clean(item.get("full_name", "")), url=str(item.get("html_url", "")), snippet=clean(item.get("description", ""))[:600], source="GitHub", engine="github", category="it", score=min(1.0, float(item.get("stargazers_count", 0)) / 10000)) for item in response.json().get("items", []) if item.get("html_url")]


@register("stackexchange", label="Stack Exchange", categories=("it",), shortcut="se")
async def stackexchange(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://api.stackexchange.com/2.3/search/advanced", {"q": request.query, "site": "stackoverflow", "pagesize": request.limit, "page": request.page, "order": "desc", "sort": "relevance", "filter": "default"}, settings, client)
    return [SearchResult(title=clean(item.get("title", "")), url=str(item.get("link", "")), snippet="; ".join(item.get("tags", [])[:8]), source="Stack Overflow", engine="stackexchange", category="it", score=float(item.get("score", 0))) for item in data.get("items", []) if item.get("link")]


@register("mdn", label="MDN", categories=("it",), shortcut="mdn")
async def mdn(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://developer.mozilla.org/api/v1/search", {"q": request.query, "size": request.limit}, settings, client)
    return [SearchResult(title=clean(item.get("title", "")), url="https://developer.mozilla.org" + str(item.get("mdn_url", "")), snippet=clean(item.get("summary", ""))[:600], source="MDN", engine="mdn", category="it") for item in data.get("documents", []) if item.get("title") and item.get("mdn_url")]


@register("dockerhub", label="Docker Hub", categories=("it",), shortcut="dh")
async def dockerhub(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://hub.docker.com/v2/search/repositories", {"query": request.query, "page_size": request.limit, "page": request.page}, settings, client)
    return [SearchResult(title=clean(item.get("repo_name", "")), url=f"https://hub.docker.com/r/{item.get('repo_name', '')}", snippet=clean(item.get("short_description", ""))[:600], source="Docker Hub", engine="dockerhub", category="it", score=float(item.get("star_count", 0))) for item in data.get("results", []) if item.get("repo_name")]
