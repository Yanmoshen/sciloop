"""SearXNG-compatible HTTP surface backed by SciLoop's embedded engine."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from services.search import SearchRequest
from services.search import search as run_search
from services.search import status as search_status

router = APIRouter(tags=["search"])


def _request(
    q: str,
    categories: str = "general,science,it",
    engines: str = "",
    language: str = "auto",
    time_range: str | None = None,
    safesearch: int = 0,
    page: int = 1,
    limit: int = 8,
) -> SearchRequest:
    selected = tuple(item.strip().lower() for item in engines.split(",") if item.strip())
    selected_categories = tuple(item.strip().lower() for item in categories.split(",") if item.strip())
    return SearchRequest(
        query=q,
        categories=selected_categories,
        engines=selected,
        language=language,
        time_range=time_range,
        safesearch=max(0, min(2, safesearch)),
        page=max(1, page),
        limit=max(1, min(50, limit)),
    )


@router.get("/search", summary="内置元搜索（SearXNG 兼容）")
async def search(
    q: str = Query(..., min_length=1),
    categories: str = Query("general,science,it"),
    engines: str = Query(""),
    language: str = Query("auto"),
    time_range: str | None = Query(None),
    safesearch: int = Query(0),
    page: int = Query(1),
    limit: int = Query(8),
) -> dict[str, Any]:
    response = await run_search(_request(q, categories, engines, language, time_range, safesearch, page, limit))
    return response.as_dict()


@router.post("/search", summary="内置元搜索（JSON）")
async def search_post(payload: dict[str, Any]) -> dict[str, Any]:
    response = await run_search(
        _request(
            str(payload.get("q") or payload.get("query") or ""),
            str(payload.get("categories") or "general,science,it"),
            str(payload.get("engines") or ""),
            str(payload.get("language") or "auto"),
            payload.get("time_range"),
            int(payload.get("safesearch") or 0),
            int(payload.get("page") or 1),
            int(payload.get("limit") or 8),
        )
    )
    return response.as_dict()


@router.get("/config", summary="搜索引擎配置（SearXNG 兼容）")
async def config() -> dict[str, Any]:
    return await search_status()
