# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""联网搜索：走项目自建的 SearXNG（免费开源、不需要任何商业 API Key）。

为什么自建而不是接商业搜索 API（研究者 2026-09-22 选定）
------------------------------------------------------
- 免费、开源、可离线部署（别人把自己电脑当服务器时也能有搜索）；
- 不经过任何第三方账号，查询不出售给谁；
- 代价：**它自己不爬网**，是把查询转给上游搜索引擎再汇总 → 容器需要能出网。

两条纪律
--------
1. **搜不到就说搜不到**：连不上、被限流、结果为空，都如实回报给模型与研究者，
   绝不假装"我查过了、没有相关资料"。
2. **这里不做取舍**：哪些结果值得存进论文库，由模型自己决定（研究者 2026-09-22）。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

__all__ = ["DEFAULT_URL", "UNAVAILABLE_HINT", "base_url", "search"]

#: 容器内按服务名访问（与 compose 同网）；本机直跑时可覆盖成 http://localhost:8888
DEFAULT_URL = "http://searxng:8080"
URL_ENV = "SCILOOP_SEARXNG_URL"
DEFAULT_TIMEOUT_S = 20.0

#: 面向研究者的话（**人话**）：怎么把它弄起来
UNAVAILABLE_HINT = (
    "联网搜索这个能力现在没连上（它是项目自带的一个搜索容器）。"
    "如果你想让它能上网找资料，在项目目录里执行一次 `docker compose up -d searxng` 就好了；"
    "不启动也不影响其它能力，只是我不能上网。"
)


def base_url() -> str:
    return (os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    """把 SearXNG 的一条结果收成我们自己的形状（字段少而稳，不把上游结构透给模型）。"""

    engines = item.get("engines")
    return {
        "title": str(item.get("title") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "snippet": str(item.get("content") or "").strip()[:600],
        "engines": [str(name) for name in engines][:5] if isinstance(engines, list) else [],
        "score": float(item.get("score") or 0.0),
    }


async def search(
    query: str,
    *,
    limit: int = 8,
    language: str = "auto",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """搜一次。**永不抛异常**：连不上也是一种正常结果，要如实回给模型。"""

    text = (query or "").strip()
    if not text:
        return {"ok": False, "error": "没有给搜索词"}

    params = {"q": text, "format": "json", "language": language}
    owns = client is None
    client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    try:
        response = await client.get(f"{base_url()}/search", params=params)
        if response.status_code == 429:
            return {
                "ok": False,
                "unavailable": True,
                "error": "搜索服务把这次请求当成机器人挡掉了（触发限流）",
                "hint": UNAVAILABLE_HINT,
            }
        if response.status_code != 200:
            return {
                "ok": False,
                "unavailable": True,
                "error": f"搜索服务返回了 {response.status_code}",
                "hint": UNAVAILABLE_HINT,
            }
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "ok": False,
            "unavailable": True,
            "error": f"连不上搜索服务：{str(exc)[:160]}",
            "hint": UNAVAILABLE_HINT,
        }
    finally:
        if owns:
            await client.aclose()

    raw = payload.get("results") if isinstance(payload, dict) else None
    results = [_normalize(item) for item in (raw or []) if isinstance(item, dict)]
    results = [item for item in results if item["url"]]
    # 上游给的相关度排序不保证稳定，这里只按它的 score 取前 N 条，**不替模型筛内容**
    results.sort(key=lambda item: item["score"], reverse=True)
    return {
        "ok": True,
        "query": text,
        "count": len(results[:limit]),
        "results": results[:limit],
        "note": "这些是搜索结果的摘要，不是论文全文；引用前请打开原始链接核对。",
    }
