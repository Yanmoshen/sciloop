"""Academic adapters kept as normal search engines as in SearXNG."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import httpx

from ..models import SearchRequest, SearchResult
from ..registry import register
from .base import clean, get_json


@register("arxiv", label="arXiv", categories=("science",), shortcut="ax")
async def arxiv(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    url = "https://export.arxiv.org/api/query?search_query=" + quote(f"all:{request.query}", safe=":") + f"&start={(request.page - 1) * request.limit}&max_results={request.limit}"
    response = await client.get(url, headers={"User-Agent": settings.user_agent})
    response.raise_for_status()
    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    hits = []
    for entry in root.findall("atom:entry", ns):
        link = (entry.findtext("atom:id", "", ns) or "").strip()
        if link:
            hits.append(SearchResult(title=clean(entry.findtext("atom:title", "", ns)), url=link, snippet=clean(entry.findtext("atom:summary", "", ns))[:600], source="arXiv", engine="arxiv", category="science", published=(entry.findtext("atom:published", "", ns) or "")[:10]))
    return hits


@register("crossref", label="Crossref", categories=("science",), shortcut="cr")
async def crossref(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://api.crossref.org/works", {"query": request.query, "rows": request.limit, "filter": "type:journal-article", "select": "title,DOI,URL,container-title,issued"}, settings, client)
    hits = []
    for item in data.get("message", {}).get("items", []):
        doi = str(item.get("DOI") or "")
        url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
        title = clean((item.get("title") or [""])[0])
        if url and title:
            year = str((((item.get("issued") or {}).get("date-parts") or [[""]])[0][0]) or "")
            hits.append(SearchResult(title=title, url=url, snippet=clean((item.get("container-title") or [""])[0]), source="Crossref", engine="crossref", category="science", published=year))
    return hits


@register("europepmc", label="Europe PMC", categories=("science",), shortcut="ep")
async def europepmc(request: SearchRequest, settings: Any, client: httpx.AsyncClient) -> list[SearchResult]:
    data = await get_json(request, "https://www.ebi.ac.uk/europepmc/webservices/rest/search", {"query": request.query, "format": "json", "pageSize": request.limit, "page": request.page}, settings, client)
    return [SearchResult(title=clean(item.get("title", "")), url=f"https://europepmc.org/article/{item.get('source', 'MED')}/{item.get('id', '')}", snippet=clean(item.get("authorString", "")), source="Europe PMC", engine="europepmc", category="science", published=str(item.get("firstPublicationDate", ""))) for item in data.get("resultList", {}).get("result", []) if item.get("title") and item.get("id")]
