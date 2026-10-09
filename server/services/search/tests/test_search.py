from __future__ import annotations

import asyncio

import httpx

from services.search import SearchRequest
from services.search import search as run_search
from services.search.config import SearchSettings


def test_dispatcher_uses_categories_and_deduplicates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("wikipedia.org"):
            return httpx.Response(200, json={"query": {"search": [{"title": "Python", "snippet": "language"}]}})
        if request.url.host.endswith("github.com"):
            return httpx.Response(200, json={"items": [{"full_name": "python", "html_url": "https://python.org", "description": "language"}]})
        return httpx.Response(200, json={"items": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(run_search(SearchRequest("python", categories=("general", "it"), engines=("wikipedia", "github"), limit=5), settings=SearchSettings(retries=0, concurrency=2), client=client))
    asyncio.run(client.aclose())
    assert result.ok
    assert result.engines_used == ["wikipedia", "github"]
    assert len(result.results) == 2
    assert {row.source for row in result.results} == {"Wikipedia", "GitHub"}


def test_one_engine_failure_does_not_cancel_others() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("wikipedia.org"):
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"items": [{"full_name": "python", "html_url": "https://python.org"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = asyncio.run(run_search(SearchRequest("python", engines=("wikipedia", "github"), limit=5), settings=SearchSettings(retries=0, concurrency=2), client=client))
    asyncio.run(client.aclose())
    assert result.ok
    assert result.engines_used == ["github"]
    assert result.failures[0].reason == "no_egress"


def test_status_exposes_categories_and_engine_configuration() -> None:
    from services.search import status

    result = asyncio.run(status(settings=SearchSettings(enabled_engines=("wikipedia",))))
    assert result["ok"] is True
    assert "science" in result["categories"]
    assert next(row for row in result["engines"] if row["name"] == "wikipedia")["enabled"] is True
    assert next(row for row in result["engines"] if row["name"] == "github")["enabled"] is False
