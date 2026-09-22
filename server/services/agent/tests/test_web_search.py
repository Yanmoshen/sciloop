# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""联网搜索（自建 SearXNG）单测：假传输层，不启真容器。

要钉住一条纪律：**搜不到就说搜不到**。
连不上 / 被限流 / 返回非 200，都必须如实回报，绝不能让模型以为"查过了、没有相关资料"。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from services.agent import web_search


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(web_search.URL_ENV, raising=False)
    assert web_search.base_url() == web_search.DEFAULT_URL
    monkeypatch.setenv(web_search.URL_ENV, "http://localhost:8888/")
    assert web_search.base_url() == "http://localhost:8888"


def test_search_returns_normalized_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "A", "url": "https://a", "content": "摘要 A", "engines": ["brave"], "score": 1.5},
                    {"title": "B", "url": "https://b", "content": "摘要 B", "score": 3.0},
                    {"title": "没有链接的", "url": "", "content": "x"},
                ]
            },
        )

    result = asyncio.run(web_search.search("图神经网络 推荐系统", client=_client(handler)))
    assert result["ok"] is True
    assert result["count"] == 2, "没有链接的结果不算结果"
    assert [item["url"] for item in result["results"]] == ["https://b", "https://a"], "按上游相关度排"
    assert result["results"][0]["snippet"] == "摘要 B"
    assert "不是论文全文" in result["note"], "要把'这只是摘要'如实告诉模型"


def test_search_says_so_when_service_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = asyncio.run(web_search.search("任意词", client=_client(handler)))
    assert result["ok"] is False and result["unavailable"] is True
    assert "搜" not in result["error"] or "连不上" in result["error"]
    # 给研究者的话里必须说清"怎么把它打开"，而不是只报一个错
    assert "searxng" in result["hint"]


def test_rate_limited_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    result = asyncio.run(web_search.search("任意词", client=_client(handler)))
    assert result["ok"] is False
    assert "限流" in result["error"]


def test_bad_status_and_bad_json_are_reported() -> None:
    def bad_status(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="nope")

    assert asyncio.run(web_search.search("x", client=_client(bad_status)))["ok"] is False

    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    assert asyncio.run(web_search.search("x", client=_client(bad_json)))["ok"] is False


def test_empty_query_short_circuits() -> None:
    result = asyncio.run(web_search.search("   "))
    assert result["ok"] is False
    assert "搜索词" in result["error"]


def test_empty_result_is_ok_but_says_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    result = asyncio.run(web_search.search("非常冷门的词", client=_client(handler)))
    assert result["ok"] is True and result["count"] == 0
