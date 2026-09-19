# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""CSL-JSON 导出（Zotero / Mendeley 兼容，EasyPaper §5.6）。

内部元数据 → CSL 标准字段映射：

====================  ====================================================
CSL 字段              SciLoop 来源
====================  ====================================================
``id``                ``sciloop-<paper_id>``（稳定；无原生 CSL id）
``type``              venue 有值 → ``article-journal``；arXiv → ``posted-content``；否则 ``article``
``title``             ``papers.title``
``author[].family/given``  ``papers.authors[].name`` 拆分；拆不出退回 ``literal``
``issued.date-parts`` ``papers.published_at``（仅在可解析出年份时输出）
``DOI``               ``papers.doi``
``container-title``   ``papers.venue``
``URL``               ``papers.pdf_url``，回退 DOI / arXiv 规范链接
``abstract``          ``papers.abstract``
``custom.sciloop``    paper_id / source / external_id / arxiv_id / citation_count / 映射声明
====================  ====================================================

取不到的字段**不输出该键**（CSL-JSON 允许缺省），绝不填占位字符串。
输出为**顶层数组**（Zotero/Mendeley 直接导入的形态），每个元素自带
``custom.sciloop`` 溯源信息。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.services.export.mapping import (
    SCHEMA_VERSION,
    SCOPE_NOTE,
    csl_authors,
    mapping_notes,
    real_value,
    to_int,
)

__all__ = ["csl_item", "render_csl_items"]

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")

_TYPE_BY_VENUE = "article-journal"
_TYPE_PREPRINT = "posted-content"
_TYPE_DEFAULT = "article"


def _issue_date(published_at: Any) -> dict[str, Any] | None:
    value = real_value(published_at)
    if value is None:
        return None
    match = _DATE_RE.match(str(value))
    if not match:
        return None
    parts: list[int] = [int(match.group(1))]
    if match.group(2):
        parts.append(int(match.group(2)))
        if match.group(3):
            parts.append(int(match.group(3)))
    return {"date-parts": [parts]}


def _csl_type(meta: Mapping[str, Any]) -> str:
    if real_value(meta.get("venue")):
        return _TYPE_BY_VENUE
    if str(meta.get("source") or "").lower() == "arxiv":
        return _TYPE_PREPRINT
    return _TYPE_DEFAULT


def csl_item(meta: Mapping[str, Any]) -> dict[str, Any]:
    """单篇论文 → CSL-JSON 条目（缺省字段直接省略）。"""
    item: dict[str, Any] = {
        "id": f"sciloop-{meta.get('paper_id')}",
        "type": _csl_type(meta),
    }
    title = real_value(meta.get("title"))
    if title:
        item["title"] = str(title)
    authors = csl_authors(meta.get("authors"))
    if authors:
        item["author"] = authors
    issued = _issue_date(meta.get("published_at"))
    if issued:
        item["issued"] = issued
    doi = real_value(meta.get("doi"))
    if doi:
        item["DOI"] = str(doi)
    venue = real_value(meta.get("venue"))
    if venue:
        item["container-title"] = str(venue)
    abstract = real_value(meta.get("abstract"))
    if abstract:
        item["abstract"] = str(abstract)
    url = real_value(meta.get("url")) or real_value(meta.get("canonical_url"))
    if url:
        item["URL"] = str(url)
    item["custom"] = {
        "sciloop": {
            "schema_version": SCHEMA_VERSION,
            "paper_id": to_int(meta.get("paper_id")),
            "source": real_value(meta.get("source")),
            "external_id": real_value(meta.get("external_id")),
            "arxiv_id": real_value(meta.get("arxiv_id")),
            "citation_count": to_int(meta.get("citation_count")),
            "citation_velocity": meta.get("citation_velocity"),
            "venue_level": to_int(meta.get("venue_level")),
            "score_coverage": meta.get("score_coverage"),
            "scope_note": SCOPE_NOTE,
            "type_rule": (
                "venue 非空 → article-journal；source=arxiv → posted-content；其余 → article"
            ),
            "issued_rule": "仅当 published_at 可解析出年份时输出 issued，否则省略该键",
            "mapping_notes": mapping_notes(),
        }
    }
    return item


def render_csl_items(metas: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """渲染 CSL-JSON 顶层数组。"""
    return [csl_item(meta) for meta in metas]
