# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""联网检索单测：两个能力 + **可诊断的失败**。假传输层，不碰真网络。

要钉住三条：
1. **搜不到就说搜不到**（不假装查过）；
2. 失败要能分清 **服务没起 / 连不上外网 / 被上游限流** —— 这三种修法不同；
3. 某个学术来源挂了**不影响**其它来源，但要如实记进 `sources_failed`。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from services.agent import web_search


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


# --------------------------------------------------------------------------- #
# 环境变量与代理
# --------------------------------------------------------------------------- #
def test_env_value_strips_inline_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` 里的行内中文注释不能带进请求头（实测抛过 ascii 编码错）。"""

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc123   # 可选：提升限额")
    assert web_search._env_value("GITHUB_TOKEN") == "ghp_abc123"


def test_proxy_is_optional_and_direct_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认直连（研究者要的）；配了才走代理。"""

    monkeypatch.delenv(web_search.PROXY_ENV, raising=False)
    assert web_search.proxy_url() is None
    monkeypatch.setenv(web_search.PROXY_ENV, "http://127.0.0.1:7897   # 本机代理")
    assert web_search.proxy_url() == "http://127.0.0.1:7897"


def test_default_categories_include_science_and_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认分类必须带上 science / it —— 只搜 general 会把 arxiv / github 全跳过。"""

    assert "science" in web_search.DEFAULT_CATEGORIES
    assert "it" in web_search.DEFAULT_CATEGORIES


def test_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(web_search.SEARXNG_URL_ENV, raising=False)
    assert web_search.base_url() == web_search.DEFAULT_URL
    monkeypatch.setenv(web_search.SEARXNG_URL_ENV, "http://localhost:8888/")
    assert web_search.base_url() == "http://localhost:8888"


# --------------------------------------------------------------------------- #
# 搜网页：正常 / 服务没起 / 被限流
# --------------------------------------------------------------------------- #
def test_search_web_returns_normalized_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        assert "science" in request.url.params["categories"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "A",
                        "url": "https://a",
                        "content": "摘要 A",
                        "engines": ["arxiv"],
                        "score": 1.5,
                    },
                    {
                        "title": "B",
                        "url": "https://b",
                        "content": "摘要 B",
                        "engines": ["bing"],
                        "score": 3.0,
                    },
                    {"title": "没有链接的", "url": ""},
                ]
            },
        )

    result = asyncio.run(web_search.search_web("tokenizer 公平性", client=_client(handler)))
    assert result["ok"] is True
    assert result["capability"] == "web"
    assert result["count"] == 2, "没有链接的不算结果"
    assert [row["url"] for row in result["results"]] == ["https://b", "https://a"], "按相关度排"
    assert result["results"][1]["source"] == "arXiv", "来源要翻成人看的名字"
    assert result["results"][0]["source"] == "必应"
    assert "不是论文全文" in result["note"]


def test_web_service_down_is_diagnosed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = asyncio.run(web_search.search_web("任意词", client=_client(handler)))
    assert result["ok"] is False
    assert result["reason"] == web_search.REASON_SERVICE_DOWN
    assert "searxng" in result["hint"], "要告诉他怎么把服务起来"


def test_web_blocked_by_upstream_is_diagnosed() -> None:
    """服务是活的，但引擎全被反爬挡了 → 归到"被限流"，不是"服务没起"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [
                    ["duckduckgo", "CAPTCHA (wt-wt)"],
                    ["google cse", "Suspended: too many requests"],
                ],
            },
        )

    result = asyncio.run(web_search.search_web("任意词", client=_client(handler)))
    assert result["ok"] is False
    assert result["reason"] == web_search.REASON_BLOCKED
    assert "学术" in result["hint"], "要给出「改用查学术」这条出路"
    assert {row["engine"] for row in result["sources_unresponsive"]} == {"DuckDuckGo", "Google"}


def test_web_no_egress_is_diagnosed() -> None:
    """服务活着但连不上外网（引擎全 timeout）→ 归到"没外网"，提示代理那条路。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [["brave", "timeout"], ["wikipedia", "timeout"]],
            },
        )

    result = asyncio.run(web_search.search_web("任意词", client=_client(handler)))
    assert result["ok"] is False
    assert result["reason"] == web_search.REASON_NO_EGRESS
    assert "代理" in result["hint"]


def test_web_empty_but_no_engine_errors_is_still_ok() -> None:
    """一条没有、但引擎都没报错 → 是"真没搜到"，不该算失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    result = asyncio.run(web_search.search_web("非常冷门的词", client=_client(handler)))
    assert result["ok"] is True and result["count"] == 0


def test_web_empty_query_short_circuits() -> None:
    result = asyncio.run(web_search.search_web("   "))
    assert result["ok"] is False
    assert "搜索词" in result["error"]


# --------------------------------------------------------------------------- #
# 查学术：多来源聚合 / 单源失败不影响整体 / 被挡的诊断
# --------------------------------------------------------------------------- #
def _academic_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host.endswith("arxiv.org"):
        return httpx.Response(
            200,
            text=(
                "<feed xmlns='http://www.w3.org/2005/Atom'><entry>"
                "<id>http://arxiv.org/abs/1</id><title>Arxiv 标题</title>"
                "<summary>摘要</summary><published>2024-01-01T00:00:00Z</published>"
                "</entry></feed>"
            ),
            headers={"content-type": "application/atom+xml"},
        )
    if host.endswith("crossref.org"):
        return httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "title": ["Crossref 标题"],
                            "DOI": "10.1/x",
                            "URL": "https://doi.org/10.1/x",
                            "container-title": ["某刊"],
                            "issued": {"date-parts": [[2024]]},
                        }
                    ]
                }
            },
        )
    if host.endswith("github.com"):
        return httpx.Response(
            200,
            json={"items": [{"full_name": "org/repo", "html_url": "https://github.com/org/repo", "description": "d", "stargazers_count": 7}]},
        )
    return httpx.Response(404)


def test_academic_aggregates_three_sources() -> None:
    result = asyncio.run(web_search.search_academic("tokenizer", client=_client(_academic_handler)))
    assert result["ok"] is True
    assert result["capability"] == "academic"
    assert result["sources_used"] == ["arXiv", "Crossref", "GitHub"]
    assert {row["source"] for row in result["results"]} == {"arXiv", "Crossref", "GitHub"}
    assert result["sources_failed"] == []


def test_academic_survives_one_source_failing() -> None:
    """一个来源挂了不影响其它来源，但要如实记下来。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("arxiv.org"):
            raise httpx.ConnectError("boom", request=request)
        return _academic_handler(request)

    result = asyncio.run(web_search.search_academic("tokenizer", client=_client(handler)))
    assert result["ok"] is True
    assert "Crossref" in result["sources_used"] and "GitHub" in result["sources_used"]
    assert result["sources_failed"][0]["source"] == "arXiv"
    assert result["sources_failed"][0]["reason"] == web_search.REASON_NO_EGRESS


def test_academic_all_blocked_is_diagnosed() -> None:
    """全被挡（比如 arXiv 按 IP 限流返回 406）→ 说清是"被挡"，并给出可用的出路。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(406, request=request)

    result = asyncio.run(web_search.search_academic("tokenizer fairness", client=_client(handler)))
    assert result["ok"] is False
    assert result["reason"] == web_search.REASON_BLOCKED
    assert "稍后" in result["hint"] or "换" in result["hint"]


def test_academic_crossref_excludes_non_articles() -> None:
    """Crossref 必须限定条目类型 —— 否则会把论文里的图注当条目返回（实测见过）。"""

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("crossref.org"):
            seen.update(dict(request.url.params))
        return _academic_handler(request)

    asyncio.run(web_search.search_academic("tokenizer", sources=("crossref",), client=_client(handler)))
    assert "journal-article" in seen.get("filter", "")


# --------------------------------------------------------------------------- #
# 过程行：搜索结果挂上去给界面画面板
# --------------------------------------------------------------------------- #
def test_search_row_payload_keeps_only_what_the_panel_needs() -> None:
    """面板只要搜索词/来源/条数/标题/链接；**摘要不往里灌**（过程行会落盘）。"""

    from services.agent import mcp_tools

    payload = mcp_tools.search_row_payload(
        "search_academic",
        {"query": "tokenizer fairness"},
        {
            "ok": True,
            "count": 1,
            "sources_used": ["arXiv"],
            "results": [
                {
                    "title": "A Survey",
                    "url": "https://a",
                    "snippet": "很长的摘要" * 50,
                    "source": "arXiv",
                }
            ],
        },
    )
    assert payload is not None
    assert payload["capability"] == "academic"
    assert payload["label"] == "查学术"
    assert payload["query"] == "tokenizer fairness"
    assert payload["sources_used"] == ["arXiv"]
    assert list(payload["results"][0]) == ["title", "url", "source"], "只留面板要用的三个字段"
    assert "snippet" not in str(payload)


def test_search_row_payload_is_none_for_other_tools() -> None:
    from services.agent import mcp_tools

    assert mcp_tools.search_row_payload("query_library", {}, {}) is None


def test_tool_row_carries_search_payload() -> None:
    from services.agent import mcp_tools

    call = {"function": {"name": "search_web", "arguments": "{}"}}
    row = mcp_tools.tool_row(call, "ok", "搜到 3 条结果", search={"count": 3})
    assert row["text"] == "「搜网页」完成：搜到 3 条结果"
    assert row["search"] == {"count": 3}
    # 没有搜索结果时不该凭空多出字段
    assert "search" not in mcp_tools.tool_row(call, "ok", "搜到 0 条结果")
