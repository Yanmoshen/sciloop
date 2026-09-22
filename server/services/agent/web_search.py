# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""联网检索：**两个能力** + **可诊断的失败**。

两个能力（研究者 2026-09-22 定的，模型自己选用哪个）
--------------------------------------------------
- ``search_academic()``：查学术。**直连官方接口**（arXiv / Crossref / GitHub）——
  不抓页面、不受反爬影响、结果结构化，是科研场景的主力。
- ``search_web()``：搜网页。走项目自建的 SearXNG（默认带 ``general,science,it`` 分类）。

失败必须能分清三件事（这三种的修法完全不同）
--------------------------------------------
- ``service_down``：搜索服务**没起来** → 告诉他怎么把它起来；
- ``no_egress``：服务起来了但**连不上外网** → 检查网络 / 配代理；
- ``blocked``：服务能上网，但**被上游限流或反爬**（429 / 验证码）→ 换词、稍后再试，或改用查学术。

⚠️ 教训（2026-09-22）：我一度把"搜到 0 条"解释成"话题太偏"，其实那天有两层原因 ——
**默认只搜 general 分类**（arxiv/github 在 science/it 里没被调用）+ 上游反爬。
所以这里**每次搜索都把"用了哪些来源、哪些没响应"如实带回来**，谁都能一眼看出是哪一层的问题。

其余纪律不变：**搜不到就说搜不到**；结果要不要用、要不要入库，由模型决定。
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import httpx

__all__ = [
    "CAPABILITY_ACADEMIC",
    "CAPABILITY_WEB",
    "DEFAULT_CATEGORIES",
    "HINTS",
    "REASON_BLOCKED",
    "REASON_BAD_RESPONSE",
    "REASON_NO_EGRESS",
    "REASON_SERVICE_DOWN",
    "SEARXNG_URL_ENV",
    "SOURCE_LABELS",
    "SUPPORTED_SOURCES",
    "base_url",
    "proxy_url",
    "search",
    "search_academic",
    "search_status",
    "search_web",
]

# --------------------------------------------------------------------------- #
# 搜索服务（SearXNG）
# --------------------------------------------------------------------------- #
DEFAULT_URL = "http://searxng:8080"
SEARXNG_URL_ENV = "SCILOOP_SEARXNG_URL"
#: 可选代理。**默认为空 = 直连**（研究者明确要"直连为主"，代理只在需要时配）。
PROXY_ENV = "SCILOOP_SEARXNG_PROXY"
DEFAULT_TIMEOUT_S = 30.0

#: 默认要搜的分类。**这一项是 2026-09-22 那次"0 条"的一半原因**：
#: arxiv / crossref 在 `science`、github 在 `it`，只搜 `general` 等于把它们全跳过。
DEFAULT_CATEGORIES = "general,science,it"

CAPABILITY_ACADEMIC = "academic"
CAPABILITY_WEB = "web"

# 三种（加一种兜底）失败原因 + 面向研究者的人话
REASON_SERVICE_DOWN = "service_down"
REASON_NO_EGRESS = "no_egress"
REASON_BLOCKED = "blocked"
REASON_BAD_RESPONSE = "bad_response"

HINTS: dict[str, str] = {
    REASON_SERVICE_DOWN: (
        "联网搜索服务没启动。在项目目录里执行一次 `docker compose up -d searxng` 就好"
        "（不启动也不影响其它能力）。"
    ),
    REASON_NO_EGRESS: (
        "搜索服务起来了，但它连不上外网。先看这台机器能不能上外网；"
        "如果只能通过代理上网，把代理地址填到环境变量 SCILOOP_SEARXNG_PROXY 里。"
    ),
    REASON_BLOCKED: (
        "搜索服务能上网，但这一次被上游搜索引擎限流或反爬挡了（429 / 验证码）。"
        "可以稍后再试、换一组关键词，或改用学术检索 —— 它走的是官方接口，不受这个影响。"
    ),
    REASON_BAD_RESPONSE: "搜索服务返回了看不懂的内容，这一条先当没有结果处理。",
}

def _env_value(name: str) -> str:
    """读环境变量并**剥掉行内注释**。

    ⚠️ 这个仓库的 `.env` 里习惯写行内中文注释（`GITHUB_TOKEN=xxx   # 可选：提升限额`），
    `os.environ` 拿到的是**带注释的整串** —— 中文塞进 HTTP 头会直接抛
    `'ascii' codec can't encode characters`（2026-09-22 实测踩到）。
    """

    raw = (os.environ.get(name) or "").strip()
    if "#" in raw:
        raw = raw.split("#", 1)[0]
    return raw.strip().strip('"').strip("'").strip()


#: 对外请求统一带的 UA：arXiv 会拒掉不带 UA 的请求（实测 406）
def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    mail = _env_value("OPENALEX_MAILTO") or "sciloop@example.com"
    base = {"User-Agent": f"SciLoop/0.1 (research assistant; mailto:{mail})"}
    if extra:
        base.update(extra)
    return base


#: 引擎名 → 给人看的来源名（面板里要显示"来源 · 搜索词"）
SOURCE_LABELS: dict[str, str] = {
    "bing": "必应",
    "brave": "Brave",
    "duckduckgo": "DuckDuckGo",
    "google": "Google",
    "google cse": "Google",
    "google scholar": "Google Scholar",
    "arxiv": "arXiv",
    "crossref": "Crossref",
    "github": "GitHub",
    "stackoverflow": "Stack Overflow",
    "mdn": "MDN",
    "docker hub": "Docker Hub",
    "europepmc": "Europe PMC",
    "pubmed": "PubMed",
    "semantic scholar": "Semantic Scholar",
    "wikipedia": "Wikipedia",
    "wikidata": "Wikidata",
    "superuser": "Super User",
    "askubuntu": "Ask Ubuntu",
}

#: 学术检索支持的来源（都用官方接口，不抓页面）
SUPPORTED_SOURCES: tuple[str, ...] = ("arxiv", "crossref", "github")


def base_url() -> str:
    return (_env_value(SEARXNG_URL_ENV) or DEFAULT_URL).rstrip("/")


def proxy_url() -> str | None:
    """可选代理；**没配就是直连**。"""

    value = _env_value(PROXY_ENV)
    return value or None


def _client(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    if client is not None:
        return client, False
    kwargs: dict[str, Any] = {"timeout": DEFAULT_TIMEOUT_S}
    proxy = proxy_url()
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs), True


def _source_label(engines: Any) -> str:
    names = [str(item) for item in engines] if isinstance(engines, list) else []
    for name in names:
        if name.lower() in SOURCE_LABELS:
            return SOURCE_LABELS[name.lower()]
    return names[0] if names else "网页"


def _failure(reason: str, error: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "error": error[:200], "hint": HINTS.get(reason, "")}


# --------------------------------------------------------------------------- #
# 能力一：搜网页（SearXNG）
# --------------------------------------------------------------------------- #
def _classify_unresponsive(items: Any) -> tuple[str, list[dict[str, str]]]:
    """把 SearXNG 报的"哪些引擎没响应"分类成人能看懂的原因。

    返回 ``(总原因, 明细)``；总原因用于给研究者一句话，明细用于过程行。
    """

    details: list[dict[str, str]] = []
    reasons: set[str] = set()
    for item in items or []:
        if not (isinstance(item, (list, tuple)) and len(item) >= 2):
            continue
        name, message = str(item[0]), str(item[1])
        low = message.lower()
        if "captcha" in low:
            reason = REASON_BLOCKED
        elif "too many requests" in low or "access denied" in low or "suspended" in low:
            # suspended 后面常跟 timeout —— 限流会被记成这个，仍按"被挡"处理更准确
            reason = REASON_NO_EGRESS if "timeout" in low else REASON_BLOCKED
        elif "timeout" in low or "unreachable" in low or "connection" in low:
            reason = REASON_NO_EGRESS
        else:
            reason = REASON_BAD_RESPONSE
        reasons.add(reason)
        details.append({"engine": SOURCE_LABELS.get(name.lower(), name), "reason": reason, "detail": message[:80]})

    if not reasons:
        return "", details
    # 只有"连不上"就算连不上；只要有一个是被挡，就算被挡（更具体的归因优先）
    if REASON_BLOCKED in reasons:
        return REASON_BLOCKED, details
    if REASON_NO_EGRESS in reasons:
        return REASON_NO_EGRESS, details
    return REASON_BAD_RESPONSE, details


async def search_web(
    query: str,
    *,
    limit: int = 8,
    language: str = "auto",
    categories: str = DEFAULT_CATEGORIES,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """搜网页。**永不抛异常**：连不上、被限流、没结果，都如实回报。"""

    text = (query or "").strip()
    if not text:
        return {"ok": False, "capability": CAPABILITY_WEB, "reason": REASON_BAD_RESPONSE, "error": "没有给搜索词"}

    http, owns = _client(client)
    params = {"q": text, "format": "json", "language": language, "categories": categories}
    try:
        response = await http.get(f"{base_url()}/search", params=params)
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "capability": CAPABILITY_WEB,
            "query": text,
            **_failure(REASON_SERVICE_DOWN, f"连不上搜索服务：{type(exc).__name__}"),
        }
    finally:
        if owns:
            await http.aclose()

    if response.status_code != 200:
        return {
            "ok": False,
            "capability": CAPABILITY_WEB,
            "query": text,
            **_failure(REASON_BAD_RESPONSE, f"搜索服务返回 {response.status_code}"),
        }

    try:
        payload = response.json()
    except ValueError as exc:
        return {
            "ok": False,
            "capability": CAPABILITY_WEB,
            "query": text,
            **_failure(REASON_BAD_RESPONSE, f"搜索服务返回的不是 JSON：{exc}"),
        }

    raw = payload.get("results") if isinstance(payload, dict) else None
    results: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": str(item.get("content") or "").strip()[:600],
                "source": _source_label(item.get("engines")),
                "score": float(item.get("score") or 0.0),
            }
        )
    results.sort(key=lambda row: row["score"], reverse=True)

    overall_reason, unresponsive = _classify_unresponsive(payload.get("unresponsive_engines"))
    used = sorted({row["source"] for row in results})
    data: dict[str, Any] = {
        "ok": True,
        "capability": CAPABILITY_WEB,
        "query": text,
        "count": len(results[:limit]),
        "results": results[:limit],
        "sources_used": used,
        "sources_unresponsive": unresponsive,
        "note": "这些是网页结果的摘要，不是论文全文；引用前请打开原始链接核对。",
    }
    if not results and overall_reason:
        # **一条都没有，而且有引擎报错**：如实说清是哪一层，别让模型以为"这题没资料"
        data["ok"] = False
        data["reason"] = overall_reason
        data["error"] = "这次搜索没有拿到任何结果：" + "；".join(
            f"{item['engine']}={item['detail'][:40]}" for item in unresponsive[:4]
        )
        data["hint"] = HINTS.get(overall_reason, "")
    return data


# --------------------------------------------------------------------------- #
# 能力二：查学术（直连官方接口，不抓页面）
# --------------------------------------------------------------------------- #
async def _arxiv(query: str, limit: int, http: httpx.AsyncClient) -> list[dict[str, Any]]:
    # ⚠️ URL 必须**手拼**，不能走 httpx 的 params：
    # httpx 会把 `search_query=all:tokenizer` 的冒号编码成 `%3A`，arXiv 直接回 **406**（实测）。
    # 同一个 UA、同一个地址，手拼 URL 就是 200 —— curl 也不编码，所以"curl 行、代码不行"卡了很久。
    url = (
        "https://export.arxiv.org/api/query?search_query="
        + quote(f"all:{query}", safe=":")
        + f"&start=0&max_results={limit}"
    )
    response = await http.get(url, headers=_headers())
    response.raise_for_status()
    root = ET.fromstring(response.text)
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    hits: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", namespace):
        title = " ".join((entry.findtext("atom:title", "", namespace) or "").split())
        link = (entry.findtext("atom:id", "", namespace) or "").strip()
        summary = " ".join((entry.findtext("atom:summary", "", namespace) or "").split())[:600]
        published = (entry.findtext("atom:published", "", namespace) or "")[:10]
        if not link:
            continue
        hits.append(
            {
                "title": title,
                "url": link,
                "snippet": summary,
                "source": SOURCE_LABELS["arxiv"],
                "published": published,
            }
        )
    return hits


async def _crossref(query: str, limit: int, http: httpx.AsyncClient) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "query": query,
        "rows": limit,
        # 不限定类型时，Crossref 会把论文里的**图注**也当条目返回
        # （实测拿到过 "Figure 7: Tokenization process of the BERT tokenizer."）
        "filter": "type:journal-article",
        "select": "title,DOI,URL,container-title,issued",
    }
    mailto = _env_value("CROSSREF_MAILTO") or _env_value("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto  # Crossref 的"礼貌池"：公开邮箱能让请求更稳
    response = await http.get("https://api.crossref.org/works", params=params, headers=_headers())
    response.raise_for_status()
    items = ((response.json().get("message") or {}).get("items")) or []
    hits: list[dict[str, Any]] = []
    for item in items:
        titles = item.get("title") or []
        title = " ".join(str(titles[0]).split()) if titles else ""
        doi = str(item.get("DOI") or "")
        url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
        if not url:
            continue
        container = (item.get("container-title") or [""])[0]
        year = ((item.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        hits.append(
            {
                "title": title or doi,
                "url": url,
                "snippet": f"{container} {year or ''}".strip(),
                "source": SOURCE_LABELS["crossref"],
                "published": str(year or ""),
            }
        )
    return hits


async def _github(query: str, limit: int, http: httpx.AsyncClient) -> list[dict[str, Any]]:
    headers = _headers({"Accept": "application/vnd.github+json"})
    token = _env_value("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await http.get(
        "https://api.github.com/search/repositories",
        params={"q": query, "per_page": limit},
        headers=headers,
    )
    response.raise_for_status()
    items = (response.json().get("items")) or []
    return [
        {
            "title": str(item.get("full_name") or ""),
            "url": str(item.get("html_url") or ""),
            "snippet": (str(item.get("description") or "") or "")[:600],
            "source": SOURCE_LABELS["github"],
            "stars": int(item.get("stargazers_count") or 0),
        }
        for item in items
        if item.get("html_url")
    ]


_SOURCE_FETCHERS = {"arxiv": _arxiv, "crossref": _crossref, "github": _github}


async def search_academic(
    query: str,
    *,
    limit: int = 5,
    sources: tuple[str, ...] = SUPPORTED_SOURCES,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """查学术：直连 arXiv / Crossref / GitHub 的官方接口，**不抓页面**（所以不受反爬影响）。

    同一组关键词会在每个来源上各查一遍，结果合并（按链接去重）。
    某个来源挂了**不影响其它来源**，但要如实记进 ``sources_failed``。
    """

    text = (query or "").strip()
    if not text:
        return {"ok": False, "capability": CAPABILITY_ACADEMIC, "reason": REASON_BAD_RESPONSE, "error": "没有给检索词"}

    http, owns = _client(client)
    results: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    used: list[str] = []
    seen: set[str] = set()
    try:
        for name in sources:
            fetcher = _SOURCE_FETCHERS.get(name)
            if fetcher is None:
                continue
            try:
                hits = await fetcher(text, limit, http)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # 403/429 是被挡；**406 也要算被挡** —— arXiv 按 IP 限流时就是回 406
                # （实测：同一请求几分钟前 200，连续探测后一律 406）
                reason = REASON_BLOCKED if status in (403, 406, 429) else REASON_BAD_RESPONSE
                failed.append({"source": SOURCE_LABELS.get(name, name), "reason": reason, "detail": f"HTTP {status}"})
                continue
            except httpx.HTTPError as exc:
                failed.append(
                    {
                        "source": SOURCE_LABELS.get(name, name),
                        "reason": REASON_NO_EGRESS,
                        "detail": type(exc).__name__,
                    }
                )
                continue
            except (ET.ParseError, ValueError, KeyError) as exc:
                failed.append(
                    {
                        "source": SOURCE_LABELS.get(name, name),
                        "reason": REASON_BAD_RESPONSE,
                        "detail": str(exc)[:60],
                    }
                )
                continue
            used.append(SOURCE_LABELS.get(name, name))
            for hit in hits:
                key = hit["url"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                results.append(hit)
    finally:
        if owns:
            await http.aclose()

    data: dict[str, Any] = {
        "ok": bool(results),
        "capability": CAPABILITY_ACADEMIC,
        "query": text,
        "count": len(results),
        "results": results[: limit * len(sources)],
        "sources_used": used,
        "sources_failed": failed,
        "note": "来自官方学术接口的题录信息；引用前请打开链接核对原文。",
    }
    if not results:
        # 一个来源都没成 → 说清是哪一层（全被挡 / 全连不上 / 各说各的）
        reasons = {item["reason"] for item in failed}
        data["reason"] = (
            REASON_BLOCKED if REASON_BLOCKED in reasons else REASON_NO_EGRESS if reasons else REASON_BAD_RESPONSE
        )
        data["error"] = "；".join(f"{item['source']}={item['detail']}" for item in failed) or "没有找到结果"
        data["hint"] = HINTS.get(data["reason"], "")
    return data


async def search_status() -> dict[str, Any]:
    """搜索服务当前状态（给界面的常驻标识用）。**只看服务本身能不能工作，不判断上游。**"""

    http, owns = _client(None)
    try:
        response = await http.get(f"{base_url()}/config")
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": REASON_SERVICE_DOWN, "hint": HINTS[REASON_SERVICE_DOWN], "detail": type(exc).__name__}
    finally:
        if owns:
            await http.aclose()

    if response.status_code != 200:
        return {
            "ok": False,
            "reason": REASON_BAD_RESPONSE,
            "hint": HINTS[REASON_BAD_RESPONSE],
            "detail": f"HTTP {response.status_code}",
        }
    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "reason": REASON_BAD_RESPONSE, "hint": HINTS[REASON_BAD_RESPONSE], "detail": "非 JSON"}
    enabled = [str(item.get("name")) for item in (payload.get("engines") or []) if item.get("enabled")]
    return {
        "ok": True,
        "detail": f"{len(enabled)} 个来源可用",
        "proxy": bool(proxy_url()),
        "academic_sources": list(SUPPORTED_SOURCES),
    }


#: 兼容旧调用名（对话工具与节点都还在用 `search`）
search = search_web


def parse_categories(value: str | None) -> str:
    """把分类串收敛成合法形态（给调用方用，避免手抖传空串）。"""

    text = (value or "").strip() or DEFAULT_CATEGORIES
    keep = [part.strip() for part in text.split(",") if part.strip()]
    return ",".join(keep) or DEFAULT_CATEGORIES


# 便于单独跑：python -m services.agent.web_search "关键词"
if __name__ == "__main__":  # pragma: no cover
    import asyncio
    import sys

    async def _main() -> None:
        query = " ".join(sys.argv[1:]) or "tokenizer fairness"
        print(json.dumps(await search_web(query, limit=3), ensure_ascii=False, indent=2)[:1500])
        print(json.dumps(await search_academic(query, limit=2), ensure_ascii=False, indent=2)[:1500])

    asyncio.run(_main())
