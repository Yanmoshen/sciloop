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
"""BibTeX 导出（EasyPaper §5.6）。

来源：``papers`` 元数据（+ ``paper_identities`` 的 arXiv id）。

硬口径
------
- **必须转义** BibTeX 特殊字符 ``& % $ # _ { } ~ ^ \\``（:func:`escape_bibtex`），
  否则标题/作者/venue 会破坏语法。
- **空字段跳过**（``year`` / ``venue`` / ``doi`` / ``url`` / ``eprint`` 取不到就不输出该行），
  **绝不输出空串字段**。
- cite key 稳定且唯一：``姓+年`` + arXiv id（无 arXiv 用 ``external_id``），
  冲突时追加 ``-<paper_id>``（paper_id 固定 → 仍然稳定）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.export.mapping import (
    DISCLAIMER,
    SCHEMA_VERSION,
    SCOPE_NOTE,
    author_rows,
    bibtex_cite_key,
    escape_bibtex,
    real_value,
)

__all__ = ["bibtex_entry", "render_bibtex"]


def _entry_type(meta: Mapping[str, Any]) -> str:
    """有 venue → ``@article``；否则（预印本/无 venue）→ ``@misc``。"""
    if real_value(meta.get("venue")):
        return "article"
    return "misc"


def bibtex_entry(meta: Mapping[str, Any], *, used_keys: set[str]) -> str:
    """单条 BibTeX 记录：字段按固定顺序输出，空字段跳过。"""
    entry_type = _entry_type(meta)
    key = bibtex_cite_key(meta, used=used_keys)
    lines: list[str] = []
    authors = [row["name"] for row in author_rows(meta.get("authors")) if row.get("name")]
    if authors:
        lines.append(f"  author        = {{{' and '.join(escape_bibtex(name) for name in authors)}}}")
    title = real_value(meta.get("title"))
    if title:
        lines.append(f"  title         = {{{escape_bibtex(title)}}}")
    if entry_type == "article":
        lines.append(f"  journal       = {{{escape_bibtex(meta.get('venue'))}}}")
    year = meta.get("year")
    if year is not None:
        lines.append(f"  year          = {{{escape_bibtex(year)}}}")
    doi = real_value(meta.get("doi"))
    if doi:
        lines.append(f"  doi           = {{{escape_bibtex(doi)}}}")
    arxiv_id = real_value(meta.get("arxiv_id"))
    if arxiv_id:
        lines.append(f"  eprint        = {{{escape_bibtex(arxiv_id)}}}")
        lines.append("  archivePrefix = {arXiv}")
    url = real_value(meta.get("url")) or real_value(meta.get("canonical_url"))
    if url:
        lines.append(f"  url           = {{{escape_bibtex(url)}}}")
    # 可追溯信息不作为书目字段（避免污染 BibTeX 语义），统一放在 % 注释里
    body = ",\n".join(lines)
    footer = f"% sciloop_paper_id: {meta.get('paper_id')} | source: {meta.get('source') or 'null'}"
    return f"@{entry_type}{{{key},\n{body}\n}}\n{footer}\n"


def render_bibtex(
    metas: Sequence[Mapping[str, Any]], *, exported_at: str, schema_version: str = SCHEMA_VERSION
) -> str:
    """渲染整份 ``.bib``（含头部口径说明，便于审计来源）。"""
    used_keys: set[str] = set()
    header = [
        "% SciLoop BibTeX 导出（EasyPaper §5.6 同形）",
        f"% schema_version: {schema_version}",
        f"% exported_at: {exported_at}",
        "% access_mode: public_demo",
        f"% {SCOPE_NOTE}",
        f"% {DISCLAIMER}",
        f"% entries: {len(metas)}",
        "% 空字段已跳过（不输出空串）；特殊字符 & % $ # _ { } ~ ^ \\ 已转义",
        "",
    ]
    entries = [bibtex_entry(meta, used_keys=used_keys) for meta in metas]
    return "\n".join(header) + "\n".join(entries) + "\n"
