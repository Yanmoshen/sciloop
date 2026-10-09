"""General web engines. They use public endpoints and need no API key."""

from __future__ import annotations

from typing import Any
import xml.etree.ElementTree as ET

import httpx

from ..models import SearchRequest, SearchResult
from ..registry import register
from .base import clean, get_json, get_text, html_results


@register("bing", label="Bing", categories=("general",), shortcut="bi", supports_time_range=True)
async def bing(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    params = {"q": request.query, "count": request.limit, "first": (request.page - 1) * request.limit + 1}
    if request.language not in {"", "auto"}:
        params["setlang"] = request.language.split("-")[0]
    text = await get_text(request, "https://www.bing.com/search", params, settings, client)
    return html_results(
        text,
        r'<li[^>]+class=["\']b_algo["\'][^>]*>.*?<h2[^>]*><a[^>]+href=["\'](?P<url>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>.*?(?:<p[^>]*>(?P<snippet>.*?)</p>)?',
        "https://www.bing.com",
        source="Bing", engine="bing", category="general",
    )[: request.limit]


@register("duckduckgo", label="DuckDuckGo", categories=("general",), shortcut="ddg", supports_time_range=True)
async def duckduckgo(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    text = await get_text(request, "https://html.duckduckgo.com/html/", {"q": request.query}, settings, client)
    return html_results(
        text,
        r'<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]+href=["\'](?P<url>[^"\']+)["\'][^>]*>(?P<title>.*?)</a>.*?(?:<a[^>]+class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(?P<snippet>.*?)</a>)?',
        "https://html.duckduckgo.com",
        source="DuckDuckGo", engine="duckduckgo", category="general",
    )[: request.limit]


@register("brave", label="Brave", categories=("general",), shortcut="br", supports_time_range=True)
async def brave(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    text = await get_text(request, "https://search.brave.com/search", {"q": request.query, "source": "web"}, settings, client)
    return html_results(
        text,
        r'<a[^>]+href=["\'](?P<url>https?://[^"\']+)["\'][^>]*class=["\'][^"\']*(?:heading|result-header)[^"\']*["\'][^>]*>(?P<title>.*?)</a>.*?(?:<p[^>]*>(?P<snippet>.*?)</p>)?',
        "https://search.brave.com",
        source="Brave", engine="brave", category="general",
    )[: request.limit]


@register("wikipedia", label="Wikipedia", categories=("general",), shortcut="wp")
async def wikipedia(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    lang = "en" if request.language in {"", "auto"} else request.language.split("-")[0]
    data = await get_json(request, f"https://{lang}.wikipedia.org/w/api.php", {"action": "query", "list": "search", "srsearch": request.query, "srlimit": request.limit, "format": "json", "utf8": 1}, settings, client)
    return [
        SearchResult(title=clean(item.get("title", "")), url=f"https://{lang}.wikipedia.org/wiki/{item.get('title', '').replace(' ', '_')}", snippet=clean(item.get("snippet", "")), source="Wikipedia", engine="wikipedia", category="general")
        for item in data.get("query", {}).get("search", []) if item.get("title")
    ]


@register("wikidata", label="Wikidata", categories=("general",), shortcut="wd")
async def wikidata(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://www.wikidata.org/w/api.php", {"action": "wbsearchentities", "search": request.query, "language": "en", "limit": request.limit, "format": "json", "origin": "*"}, settings, client)
    return [
        SearchResult(title=clean(item.get("label", "")), url=f"https://www.wikidata.org/wiki/{item.get('id', '')}", snippet=clean(item.get("description", "")), source="Wikidata", engine="wikidata", category="general")
        for item in data.get("search", []) if item.get("id") and item.get("label")
    ]


@register("bing_news", label="Bing News", categories=("news",), shortcut="bn", supports_time_range=True)
async def bing_news(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    response = await client.get("https://www.bing.com/news/search", params={"q": request.query, "format": "rss"}, headers={"User-Agent": settings.user_agent})
    response.raise_for_status()
    root = ET.fromstring(response.text)
    return [
        SearchResult(title=clean(item.findtext("title", "")), url=clean(item.findtext("link", "")), snippet=clean(item.findtext("description", ""))[:600], source="Bing News", engine="bing_news", category="news", published=clean(item.findtext("pubDate", "")))
        for item in root.findall(".//item") if item.findtext("title") and item.findtext("link")
    ][: request.limit]


@register("wikimedia_images", label="Wikimedia Commons", categories=("images",), shortcut="wm")
async def wikimedia_images(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://commons.wikimedia.org/w/api.php", {"action": "query", "generator": "search", "gsrsearch": request.query, "gsrnamespace": 6, "gsrlimit": request.limit, "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 800, "format": "json", "origin": "*"}, settings, client)
    pages = data.get("query", {}).get("pages", {}).values()
    return [
        SearchResult(title=clean(item.get("title", "")).removeprefix("File:"), url=str((item.get("imageinfo") or [{}])[0].get("descriptionurl") or "https://commons.wikimedia.org/wiki/" + str(item.get("title", "")).replace(" ", "_")), snippet="Wikimedia Commons image", source="Wikimedia Commons", engine="wikimedia_images", category="images", metadata={"image_url": str((item.get("imageinfo") or [{}])[0].get("thumburl") or "")})
        for item in pages if item.get("title") and item.get("imageinfo")
    ]
