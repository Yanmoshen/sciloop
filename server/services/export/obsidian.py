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
"""Obsidian ZIP 导出（Markdown 笔记 + wiki 链接，EasyPaper §5.5）。

产物结构::

    README.md                    # 生成方式与字段含义说明
    papers/<安全标题>.md          # 论文笔记（YAML front-matter + 正文 + wiki 链接）
    entities/<安全实体名>.md       # 实体笔记（含反向链接）

安全化（硬约束）
----------------
- 文件名去除路径分隔符 / 控制字符 / ``..``，压平空白并截断长度（:func:`safe_filename`）。
- 重名加短哈希后缀（:func:`unique_name`），保证一一对应。
- zip 条目名经 :func:`safe_zip_entry` 二次清洗：**不含 ``..``、不含绝对路径**。
- wiki 链接只指向本 ZIP 内真实存在的实体笔记（索引缺失时退化为纯文本，不产生死链）。
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

from services.export.mapping import (
    DISCLAIMER,
    SCHEMA_VERSION,
    SCOPE_NOTE,
    locator_note,
    mapping_notes,
    real_value,
    safe_filename,
    safe_zip_entry,
    to_int,
    unique_name,
)

__all__ = ["build_obsidian_zip", "paper_note", "entity_note"]

_STATUS_LABELS = {
    "supported": "supported（有证据支撑）",
    "contradicted": "contradicted（证据冲突）",
    "insufficient": "insufficient（证据不足）",
}


def _yaml_scalar(value: Any) -> str:
    """YAML 标量：``None`` → ``null``；字符串统一用 JSON 双引号转义。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _front_matter(fields: Mapping[str, Any]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {_yaml_scalar(value)}" for key, value in fields.items())
    lines.append("---")
    return "\n".join(lines)


def _card_section(card: Mapping[str, Any] | None) -> list[str]:
    if not card:
        return ["## 解析卡片", "", "> 该论文暂无 `paper_cards` 记录：不做推断。", ""]
    lines = ["## 解析卡片（最新版本）", ""]
    lines.append(f"- **卡片版本**: {card.get('version')}（`paper_cards.id={card.get('card_id')}`）")
    lines.append(f"- **研究问题**: {card.get('research_problem') or 'null'}")
    lines.append(f"- **核心方法**: {card.get('core_method') or 'null'}")
    for label, key in (
        ("关键创新", "key_innovation"),
        ("主要结论", "main_conclusions"),
        ("局限", "limitations"),
    ):
        lines.append(f"- **{label}**:")
        entries = card.get(key) or []
        if not entries:
            lines.append("  - null")
        for entry in entries:
            if isinstance(entry, Mapping):
                text = (
                    entry.get("point")
                    or entry.get("conclusion")
                    or entry.get("limitation")
                    or "null"
                )
                lines.append(f"  - {text}")
            else:
                lines.append(f"  - {entry}")
    lines.append("- **技术路线**:")
    route = card.get("technical_route") or []
    if not route:
        lines.append("  - null")
    for entry in route:
        if isinstance(entry, Mapping):
            lines.append(f"  - **{entry.get('step') or 'null'}**: {entry.get('description') or ''}")
    setup = card.get("experimental_setup")
    if isinstance(setup, Mapping):
        datasets = setup.get("datasets") or []
        baselines = setup.get("baselines") or []
        metrics = setup.get("metrics") or []
        lines.append(f"- **数据集**: {', '.join(str(v) for v in datasets) if datasets else 'null'}")
        lines.append(f"- **基线**: {', '.join(str(v) for v in baselines) if baselines else 'null'}")
        lines.append(f"- **指标**: {', '.join(str(v) for v in metrics) if metrics else 'null'}")
        lines.append(
            f"- **证据范围**: {setup.get('evidence_meta', {}).get('available_scope') if isinstance(setup.get('evidence_meta'), Mapping) else 'null'}"
        )
    lines.append("")
    return lines


def _findings_section(paper: Mapping[str, Any]) -> list[str]:
    findings = paper.get("findings") or []
    lines = ["## 发现（Claim 三态）", ""]
    if not findings:
        lines.append("> 无关联 Claim（`draft_claims` 经 `evidences.paper_id` 归属到本论文）。")
        lines.append("")
        return lines
    for finding in findings:
        status = str(finding.get("support_status") or "insufficient")
        lines.append(
            f"- **[{_STATUS_LABELS.get(status, status)}]** {finding.get('claim_text') or ''}"
        )
        if finding.get("status_reason"):
            lines.append(f"  - 依据: {finding['status_reason']}")
        lines.append(
            f"  - `draft_claims.id={finding.get('claim_id')}`（draft_id={finding.get('draft_id')}），"
            f"证据 {len(finding.get('evidences') or [])} 条"
        )
    lines.append("")
    return lines


def _spans_section(paper: Mapping[str, Any]) -> list[str]:
    structure = paper.get("structure") or {}
    spans = structure.get("spans") or []
    lines = ["## 原文定位片段", ""]
    lines.append(
        f"- parse_status: `{structure.get('parse_status')}`；coverage: `{structure.get('coverage')}`；"
        f"evidence_scope: `{structure.get('evidence_scope')}`"
    )
    lines.append(
        f"- 返回 {structure.get('spans_returned')} / 共 {structure.get('spans_total')} 条"
        f"（truncated={structure.get('spans_truncated')}）"
    )
    if not spans:
        lines.append("")
        lines.append("> 无 `paper_spans` 记录（未解析或未达全文闸门）。")
        lines.append("")
        return lines
    for span in spans:
        quote = span.get("quote_text") or ""
        lines.append(
            f"- `{span.get('section_name') or 'unknown'}` p.{span.get('page_number')} "
            f"[{span.get('char_start')},{span.get('char_end')}) `sha256={span.get('quote_sha256')}`"
        )
        lines.append(f"  > {quote}")
    lines.append("")
    return lines


def paper_note(
    paper: Mapping[str, Any],
    *,
    exported_at: str,
    entity_links: Mapping[int, list[tuple[str, str]]],
    paper_id: int,
) -> str:
    """渲染单篇论文笔记（含 wiki 链接）。"""
    metadata = paper.get("metadata") or {}
    structure = paper.get("structure") or {}
    link_rows = entity_links.get(paper_id, [])
    fields = {
        "type": "paper",
        "paper_id": to_int(metadata.get("paper_id")),
        "title": real_value(metadata.get("title")),
        "authors": [
            row.get("name") for row in (metadata.get("authors") or []) if isinstance(row, Mapping)
        ]
        or None,
        "year": metadata.get("year"),
        "venue": metadata.get("venue"),
        "doi": metadata.get("doi"),
        "arxiv_id": metadata.get("arxiv_id"),
        "source": metadata.get("source"),
        "external_id": metadata.get("external_id"),
        "url": metadata.get("url") or metadata.get("canonical_url"),
        "citation_count": metadata.get("citation_count"),
        "citation_velocity": metadata.get("citation_velocity"),
        "parse_status": structure.get("parse_status"),
        "coverage": structure.get("coverage"),
        "evidence_scope": structure.get("evidence_scope"),
        "entities": [name for _entity_id, name in link_rows] or None,
        "derived_from": "papers / paper_documents / paper_spans / paper_cards / draft_claims / evidences",
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
    }
    lines = [_front_matter(fields), ""]
    lines.append(f"# {metadata.get('title') or f'Paper {paper_id}'}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    lines.append(f"- **论文 ID**: {paper_id}")
    lines.append(f"- **来源**: `{metadata.get('source')}` / `{metadata.get('external_id')}`")
    lines.append(f"- **作者**: {', '.join(entry for entry in fields['authors'] or []) or 'null'}")
    lines.append(f"- **年份**: {metadata.get('year')}")
    lines.append(f"- **DOI**: {metadata.get('doi')}")
    lines.append(f"- **arXiv**: {metadata.get('arxiv_id')}")
    lines.append(f"- **Venue**: {metadata.get('venue')}")
    lines.append(f"- **URL**: {metadata.get('url') or metadata.get('canonical_url')}")
    lines.append(f"- **引用数**: {metadata.get('citation_count')}")
    lines.append("")
    if metadata.get("abstract"):
        lines.extend(["## 摘要", "", str(metadata["abstract"]), ""])
    lines.extend(_card_section(paper.get("card")))
    lines.extend(_findings_section(paper))
    lines.extend(["## 实体链接", ""])
    if link_rows:
        lines.extend(f"- [[{name}]]（`{entity_id}`）" for entity_id, name in link_rows)
    else:
        lines.append("> 该论文无派生实体（`paper_cards.experimental_setup` 无命名列表项）。")
    lines.append("")
    lines.extend(_spans_section(paper))
    lines.append("## 说明")
    lines.append("")
    lines.extend(f"- {note}" for note in paper.get("notes") or [])
    lines.append(f"- {locator_note()}")
    lines.append("")
    return "\n".join(lines)


def entity_note(
    entity: Mapping[str, Any],
    *,
    exported_at: str,
    paper_links: Sequence[tuple[int, str]],
) -> str:
    """渲染实体笔记（含反向链接到论文笔记）。"""
    fields = {
        "type": "entity",
        "entity_id": entity.get("entity_id"),
        "name": entity.get("name"),
        "entity_type": entity.get("entity_type"),
        "paper_ids": entity.get("paper_ids"),
        "paper_count": entity.get("paper_count"),
        "confidence": entity.get("confidence"),
        "confidence_source": entity.get("confidence_source"),
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
    }
    lines = [_front_matter(fields), ""]
    lines.append(f"# {entity.get('name')}")
    lines.append("")
    lines.append(f"> {DISCLAIMER}")
    lines.append("")
    lines.append(f"- **实体类型**: `{entity.get('entity_type')}`")
    lines.append(f"- **实体标识**: `{entity.get('entity_id')}`（派生标识，非表行 id）")
    lines.append(f"- **出现论文数**: {entity.get('paper_count')}")
    lines.append(f"- **置信度**: {entity.get('confidence')}（{entity.get('confidence_source') or '无来源列'}）")
    lines.append("")
    lines.append("## 来源行（derived_from）")
    lines.append("")
    for origin in entity.get("derived_from") or []:
        if isinstance(origin, Mapping):
            lines.append(
                f"- `{origin.get('table')}` row_id={origin.get('row_id')} "
                f"field=`{origin.get('field')}` (paper_id={origin.get('paper_id')})"
            )
    lines.append("")
    lines.append("## 关联论文")
    lines.append("")
    if paper_links:
        lines.extend(f"- [[{name}]]（paper_id={paper_id}）" for paper_id, name in paper_links)
    else:
        lines.append("> 无（实体未被任何已导出论文引用）。")
    lines.append("")
    return "\n".join(lines)


def _readme(
    *, exported_at: str, paper_count: int, entity_count: int, relationship_count: int
) -> str:
    lines = [
        "# SciLoop Obsidian 导出",
        "",
        f"> {DISCLAIMER}",
        "",
        f"- schema_version: `{SCHEMA_VERSION}`",
        f"- exported_at: `{exported_at}`",
        "- access_mode: `public_demo`",
        f"- {SCOPE_NOTE}",
        "",
        "## 生成方式",
        "",
        "本 ZIP 由 SciLoop 后端 `GET /api/v1/exports/obsidian` 生成，全部内容由以下真实表当场读取并派生：",
        "",
        "| 笔记内容 | 来源表 |",
        "|---|---|",
        "| 论文 front-matter / 书目信息 | `papers`、`paper_identities` |",
        "| 章节 / 页码 / 字符区间 / 原文片段 | `paper_documents`、`paper_spans` |",
        "| 解析卡片 8 字段（方法/数据集/指标） | `paper_cards`（该论文最新 version） |",
        "| 发现（Claim 三态）与证据定位 | `draft_claims`、`evidences` |",
        "| `entities/` 实体笔记 | 从 `paper_cards.experimental_setup.{datasets,baselines,metrics}` 派生 |",
        "",
        "## 目录结构",
        "",
        "```text",
        "README.md",
        "papers/<安全标题>.md      # 论文笔记：YAML front-matter + 摘要 + 卡片 + 发现 + 实体链接 + 定位片段",
        "entities/<安全实体名>.md   # 实体笔记：derived_from 来源行 + 反向链接论文",
        "```",
        "",
        "## 字段含义（论文笔记 front-matter）",
        "",
        "| 字段 | 含义 |",
        "|---|---|",
        "| `paper_id` | `papers.id` |",
        "| `authors` | `papers.authors[].name`；缺失为 `null` |",
        "| `year` | `papers.published_at` 的年份 |",
        "| `doi` / `arxiv_id` | `papers.doi` / `paper_identities(id_type='arxiv')` |",
        "| `parse_status` / `coverage` / `evidence_scope` | `paper_documents` 最能支撑证据的一条；`evidence_scope=abstract_only` 表示未达全文闸门（coverage<0.60） |",
        "| `entities` | 该论文引用的派生实体名（同名实体笔记见 `entities/`） |",
        "",
        "## 定位口径",
        "",
        f"- {locator_note()}",
        "- `flashcards` / `annotations`：SciLoop **无**闪卡（SRS）域与用户批注表，因此本导出不含对应笔记。",
        "",
        "## 统计",
        "",
        f"- 论文笔记: {paper_count}",
        f"- 实体笔记: {entity_count}",
        f"- 派生关系: {relationship_count}（见 `GET /api/v1/exports/csv` 的 `relationships.csv`）",
        "",
        "## 映射声明（EasyPaper ↔ SciLoop）",
        "",
        "| EasyPaper 字段 | SciLoop 来源 | 状态 |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| `{note['easypaper_field']}` | {note['sciloop_source']} | {note['status']} |"
        for note in mapping_notes()
    )
    lines.append("")
    lines.append("## 安全化约定")
    lines.append("")
    lines.append("- 文件名去除路径分隔符与控制字符，重名加短哈希后缀。")
    lines.append("- ZIP 内条目名不含 `..`、不含绝对路径（由 `safe_zip_entry` 二次清洗）。")
    lines.append("")
    return "\n".join(lines)


def build_obsidian_zip(
    papers: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
    *,
    exported_at: str,
) -> tuple[bytes, dict[str, Any]]:
    """构造 Obsidian ZIP；返回 ``(zip 字节, 统计)``。"""
    used: set[str] = set()

    entity_names: dict[str, str] = {}
    entity_key_by_id: dict[str, Mapping[str, Any]] = {}
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "entity")
        base = safe_filename(
            f"{entity.get('name')}", fallback=f"entity-{len(entity_names) + 1}"
        )
        resolved = unique_name(base, used, salt=entity_id)
        entity_names[entity_id] = resolved
        entity_key_by_id[entity_id] = entity

    title_names: dict[int, str] = {}
    for paper in papers:
        metadata = paper.get("metadata") or {}
        paper_id = to_int(metadata.get("paper_id")) or 0
        base = safe_filename(
            metadata.get("title") or f"paper-{paper_id}", fallback=f"paper-{paper_id}"
        )
        title_names[paper_id] = unique_name(base, used, salt=f"paper-{paper_id}")

    # 论文 → 实体 wiki 链接（仅链接真实存在的实体笔记，避免死链）
    relation_index: dict[tuple[str, str], str] = {}
    for entity in entities:
        relation_index[
            (str(entity.get("entity_type")), str(entity.get("name")).lower())
        ] = str(entity.get("entity_id"))
    paper_entity_links: dict[int, list[tuple[str, str]]] = {}
    entity_paper_links: dict[str, list[tuple[int, str]]] = {}
    for paper in papers:
        metadata = paper.get("metadata") or {}
        paper_id = to_int(metadata.get("paper_id")) or 0
        links: list[tuple[str, str]] = []
        for entity_type, field in (
            ("dataset", "datasets"),
            ("baseline", "baselines"),
            ("metric", "metrics"),
        ):
            for item in paper.get(field) or []:
                name = real_value(item.get("name"))
                if name is None:
                    continue
                entity_id = relation_index.get((entity_type, str(name).lower()))
                if entity_id is None or entity_id not in entity_names:
                    continue
                note_name = entity_names[entity_id]
                links.append((entity_id, note_name))
                entity_paper_links.setdefault(entity_id, []).append(
                    (paper_id, title_names.get(paper_id, f"paper-{paper_id}"))
                )
        paper_entity_links[paper_id] = links

    buffer = io.BytesIO()
    entries: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        readme_name = safe_zip_entry("README.md")
        archive.writestr(
            readme_name,
            _readme(
                exported_at=exported_at,
                paper_count=len(papers),
                entity_count=len(entities),
                relationship_count=len(relationships),
            ).encode("utf-8"),
        )
        entries.append(readme_name)
        for paper in papers:
            metadata = paper.get("metadata") or {}
            paper_id = to_int(metadata.get("paper_id")) or 0
            note_name = safe_zip_entry(f"papers/{title_names.get(paper_id, f'paper-{paper_id}')}.md")
            archive.writestr(
                note_name,
                paper_note(
                    paper,
                    exported_at=exported_at,
                    entity_links=paper_entity_links,
                    paper_id=paper_id,
                ).encode("utf-8"),
            )
            entries.append(note_name)
        for entity_id, entity in entity_key_by_id.items():
            note_name = safe_zip_entry(f"entities/{entity_names[entity_id]}.md")
            archive.writestr(
                note_name,
                entity_note(
                    entity,
                    exported_at=exported_at,
                    paper_links=entity_paper_links.get(entity_id, []),
                ).encode("utf-8"),
            )
            entries.append(note_name)

    stats = {
        "paper_notes": len(papers),
        "entity_notes": len(entity_key_by_id),
        "entries": len(entries),
        "entry_names": entries,
        "unique_names": len(set(entries)),
    }
    return buffer.getvalue(), stats
