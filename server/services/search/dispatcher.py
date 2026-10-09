"""Concurrent multi-engine dispatcher with cooldown and failure taxonomy."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

import httpx

from .categories import engines_for_categories, normalize_categories
from .config import SearchSettings, get_search_settings
from .models import EngineFailure, SearchRequest, SearchResponse
from .ranking import deduplicate, rank
from .registry import EngineDefinition, engine_registry

_COOLDOWN: dict[str, float] = {}
_FAILURES: dict[str, int] = defaultdict(int)


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return "blocked" if code in {401, 403, 406, 409, 429} else "bad_response"
    if isinstance(exc, httpx.HTTPError):
        return "no_egress"
    return "bad_response"


async def _run_engine(definition: EngineDefinition, request: SearchRequest, settings: SearchSettings, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, *, respect_cooldown: bool) -> tuple[str, list[Any], EngineFailure | None]:
    async with semaphore:
        now = time.monotonic()
        if respect_cooldown and now < _COOLDOWN.get(definition.name, 0.0):
            return definition.name, [], EngineFailure(definition.name, ",".join(definition.categories), "cooling_down", "temporarily skipped after a recent failure")
        last: BaseException | None = None
        for attempt in range(settings.retries + 1):
            try:
                hits = await asyncio.wait_for(definition.fetcher(request, settings, client), timeout=definition.timeout or settings.timeout_seconds)
                _FAILURES.pop(definition.name, None)
                return definition.name, hits or [], None
            except BaseException as exc:  # engine failures are isolated by design
                last = exc
                if attempt < settings.retries:
                    await asyncio.sleep(0.15 * (attempt + 1))
        _FAILURES[definition.name] += 1
        if respect_cooldown:
            _COOLDOWN[definition.name] = time.monotonic() + min(600.0, 30.0 * _FAILURES[definition.name])
        reason = _failure_reason(last or RuntimeError("unknown"))
        status = last.response.status_code if isinstance(last, httpx.HTTPStatusError) else None
        return definition.name, [], EngineFailure(definition.name, ",".join(definition.categories), reason, str(last or "unknown"), status)


async def search(request: SearchRequest, *, settings: SearchSettings | None = None, client: httpx.AsyncClient | None = None) -> SearchResponse:
    settings = settings or get_search_settings()
    text = request.query.strip()
    started = time.perf_counter()
    if not text:
        return SearchResponse(request, [], [], [EngineFailure("dispatcher", "general", "bad_request", "没有给搜索词")], 0)
    categories = normalize_categories(request.categories)
    selected = list(request.engines) if request.engines else list(engines_for_categories(categories))
    enabled = set(settings.enabled_engines)
    disabled = set(settings.disabled_engines)
    definitions = [engine_registry.get(name) for name in selected]
    definitions = [item for item in definitions if item and item.name not in disabled and (not enabled or item.name in enabled)]
    if not definitions:
        return SearchResponse(request, [], [], [EngineFailure("dispatcher", ",".join(categories), "no_engines", "没有启用的搜索引擎")], 0)
    owns = client is None
    if client is None:
        kwargs: dict[str, Any] = {"timeout": settings.timeout_seconds, "follow_redirects": True}
        if settings.proxy:
            kwargs["proxy"] = settings.proxy
        client = httpx.AsyncClient(**kwargs)
    try:
        normalized = SearchRequest(text, categories, tuple(selected), request.language or settings.default_language, request.time_range, request.safesearch, max(1, request.page), max(1, request.limit))
        semaphore = asyncio.Semaphore(settings.concurrency)
        outputs = await asyncio.gather(*(_run_engine(item, normalized, settings, client, semaphore, respect_cooldown=owns) for item in definitions))
    finally:
        if owns:
            await client.aclose()
    rows = []
    failures = []
    used = []
    for name, hits, failure in outputs:
        if failure:
            failures.append(failure)
        elif hits:
            used.append(name)
            rows.extend(hits)
    rows = rank(text, deduplicate(rows), request.limit)
    return SearchResponse(normalized, rows, used, failures, int((time.perf_counter() - started) * 1000))


async def status(*, settings: SearchSettings | None = None) -> dict[str, Any]:
    settings = settings or get_search_settings()
    enabled = set(settings.enabled_engines)
    disabled = set(settings.disabled_engines)
    return {
        "ok": True,
        "engine": "sciloop-search",
        "version": "searxng-compatible-1",
        "detail": f"{sum(1 for item in engine_registry.all() if item.name not in disabled and (not enabled or item.name in enabled))} 个内置来源已注册",
        "proxy": bool(settings.proxy),
        "categories": list(CATEGORIES),
        "engines": engine_registry.config(enabled, disabled),
        "plugins": [],
        "limiter": {"enabled": False},
    }


# Registration is explicit and deterministic; no network call occurs at import.
from . import engines as _builtin_engines  # noqa: E402,F401
from .categories import CATEGORIES  # noqa: E402
