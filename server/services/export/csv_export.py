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
"""CSV ZIP 导出（``entities.csv`` + ``relationships.csv``，EasyPaper §5.7）。

- 编码：**UTF-8 with BOM**（``utf-8-sig``），Excel 直接双击不乱码（README 中明确说明）。
- 换行：``\\r\\n``（Excel 友好）。
- 表头固定，列名见 :data:`ENTITY_COLUMNS` / :data:`RELATIONSHIP_COLUMNS`，
  并在 ZIP 内 ``README.md`` 与本模块常量中同步列出。
- 每个条目的 ``derived_from_*`` 列写明真实来源表与行 id，**不允许为空**（派生数据必须可溯源）。
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

from services.export.mapping import (
    DISCLAIMER,
    SCHEMA_VERSION,
    SCOPE_NOTE,
    mapping_notes,
    safe_zip_entry,
)

__all__ = [
    "ENTITY_COLUMNS",
    "RELATIONSHIP_COLUMNS",
    "build_csv_zip",
    "entities_csv",
    "relationships_csv",
]

#: ``entities.csv`` 列名（固定）
ENTITY_COLUMNS: tuple[str, ...] = (
    "entity_id",
    "name",
    "entity_type",
    "paper_ids",
    "paper_count",
    "derived_from_table",
    "derived_from_row_ids",
    "derived_from_fields",
    "confidence",
    "confidence_source",
)

#: ``relationships.csv`` 列名（固定）
RELATIONSHIP_COLUMNS: tuple[str, ...] = (
    "relationship_id",
    "from_type",
    "from_id",
    "from_name",
    "to_type",
    "to_id",
    "to_name",
    "relation",
    "paper_id",
    "derived_from_table",
    "derived_from_row_ids",
    "derived_from_fields",
    "confidence",
    "confidence_source",
)


def _origins(node: Mapping[str, Any]) -> tuple[str, str, str]:
    """把 ``derived_from`` 列表压成三列（表名 / 行 id / 字段路径），保持同序对应。"""
    tables: list[str] = []
    row_ids: list[str] = []
    fields: list[str] = []
    for origin in node.get("derived_from") or []:
        if not isinstance(origin, Mapping):
            continue
        tables.append(str(origin.get("table") or ""))
        row_ids.append(str(origin.get("row_id")))
        fields.append(str(origin.get("field") or ""))
    return "|".join(tables), "|".join(row_ids), "|".join(fields)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple | set):
        return "|".join(str(item) for item in value)
    return str(value)


def _encode(rows: Sequence[Sequence[Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    for row in rows:
        writer.writerow([_cell(value) for value in row])
    return stream.getvalue().encode("utf-8-sig")


def entities_csv(entities: Sequence[Mapping[str, Any]]) -> bytes:
    """``entities.csv``：首行表头 + 每条派生实体一行。"""
    rows: list[list[Any]] = [list(ENTITY_COLUMNS)]
    for entity in entities:
        table, row_ids, fields = _origins(entity)
        rows.append(
            [
                entity.get("entity_id"),
                entity.get("name"),
                entity.get("entity_type"),
                entity.get("paper_ids"),
                entity.get("paper_count"),
                table,
                row_ids,
                fields,
                entity.get("confidence"),
                entity.get("confidence_source"),
            ]
        )
    return _encode(rows)


def relationships_csv(relationships: Sequence[Mapping[str, Any]]) -> bytes:
    """``relationships.csv``：首行表头 + 每条派生关系一行。"""
    rows: list[list[Any]] = [list(RELATIONSHIP_COLUMNS)]
    for node in relationships:
        origin = node.get("derived_from") or []
        table, row_ids, fields = _origins({"derived_from": origin})
        source = node.get("from") or {}
        target = node.get("to") or {}
        rows.append(
            [
                node.get("relationship_id"),
                source.get("type"),
                source.get("id"),
                source.get("name"),
                target.get("type"),
                target.get("id"),
                target.get("name"),
                node.get("relation"),
                node.get("paper_id"),
                table,
                row_ids,
                fields,
                node.get("confidence"),
                node.get("confidence_source"),
            ]
        )
    return _encode(rows)


def _readme(*, exported_at: str, entity_count: int, relationship_count: int) -> str:
    lines = [
        "# SciLoop CSV 导出",
        "",
        f"> {DISCLAIMER}",
        "",
        f"- schema_version: `{SCHEMA_VERSION}`",
        f"- exported_at: `{exported_at}`",
        "- access_mode: `public_demo`",
        f"- {SCOPE_NOTE}",
        "- **编码：UTF-8 with BOM（`utf-8-sig`）**，换行 CRLF，Excel 直接打开不乱码。",
        "",
        "## entities.csv（列名固定）",
        "",
        "| 列 | 含义 |",
        "|---|---|",
        "| `entity_id` | 派生实体标识（`<type>:<name-slug>`，非表行 id） |",
        "| `name` | 实体名（取自 `paper_cards.experimental_setup` 命名列表原文） |",
        "| `entity_type` | `dataset` / `baseline` / `metric` |",
        "| `paper_ids` | 引用该实体的论文 id（`|` 分隔） |",
        "| `paper_count` | 引用论文数 |",
        "| `derived_from_table` | 来源表（固定为 `paper_cards`） |",
        "| `derived_from_row_ids` | 来源行 id（`paper_cards.id`，多行 `|` 分隔） |",
        "| `derived_from_fields` | 来源字段路径（如 `experimental_setup.datasets[0]`） |",
        "| `confidence` | `paper_cards` 无 confidence 列 → 空 |",
        "| `confidence_source` | 同上 |",
        "",
        "## relationships.csv（列名固定）",
        "",
        "| 列 | 含义 |",
        "|---|---|",
        "| `relationship_id` | 顺序号 `rel-000001…` |",
        "| `from_type` / `from_id` / `from_name` | 起点（`paper` 或 `claim`） |",
        "| `to_type` / `to_id` / `to_name` | 终点（`dataset`/`baseline`/`metric`/`paper`） |",
        "| `relation` | `paper_uses_dataset` / `paper_uses_baseline` / `paper_reports_metric` / `claim_supported_by_paper` / `claim_cites_paper` |",
        "| `paper_id` | 该关系所属论文 |",
        "| `derived_from_table` / `derived_from_row_ids` / `derived_from_fields` | 真实来源表与行 id（**不允许为空**） |",
        "| `confidence` / `confidence_source` | 仅当来源有值：Claim→论文关系取 `evidences.weight`；其余为空 |",
        "",
        "## 统计",
        "",
        f"- entities.csv 行数（不含表头）: {entity_count}",
        f"- relationships.csv 行数（不含表头）: {relationship_count}",
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
    return "\n".join(lines)


def build_csv_zip(
    entities: Sequence[Mapping[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
    *,
    exported_at: str,
) -> tuple[bytes, dict[str, Any]]:
    """构造 CSV ZIP；返回 ``(zip 字节, 统计)``。"""
    buffer = io.BytesIO()
    entries: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            ("entities.csv", entities_csv(entities)),
            ("relationships.csv", relationships_csv(relationships)),
            (
                "README.md",
                _readme(
                    exported_at=exported_at,
                    entity_count=len(entities),
                    relationship_count=len(relationships),
                ).encode("utf-8"),
            ),
        ):
            entry = safe_zip_entry(name)
            archive.writestr(entry, payload)
            entries.append(entry)
    stats = {
        "entries": len(entries),
        "entry_names": entries,
        "entity_rows": len(entities),
        "relationship_rows": len(relationships),
        "entity_columns": list(ENTITY_COLUMNS),
        "relationship_columns": list(RELATIONSHIP_COLUMNS),
        "encoding": "utf-8-sig",
    }
    return buffer.getvalue(), stats
