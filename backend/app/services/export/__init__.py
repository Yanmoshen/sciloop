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
"""多格式导出服务（EasyPaper 第 ④ 个核心模块在 SciLoop 的落地）。

对外契约（供 ``app/api/v1/exports.py`` 使用）::

    from app.services.export import (
        ExportRepository, load_library, load_paper_knowledge,
        derive_entities, derive_relationships, export_meta, source_counts,
        render_bibtex, render_csl_items, build_obsidian_zip, build_csv_zip,
    )

JSON 形状对齐 EasyPaper §5.4，但**字段来源全部是 SciLoop 真实表**；
EasyPaper 有、SciLoop 没有的字段（``flashcards`` / ``annotations`` / 知识库实体表）
一律输出空值并在 ``notes`` / ``@meta.mapping_notes`` 中如实说明，禁止假数据填充。

子模块
------
:mod:`mapping`          EasyPaper ↔ SciLoop 映射声明 + 安全化 / 转义 / 清洗工具
:mod:`knowledge_json`   全库与单篇知识 JSON（批量、分页、带上限）
:mod:`bibtex`           BibTeX（特殊字符转义、空字段跳过、稳定唯一 cite key）
:mod:`csl_json`         CSL-JSON（Zotero / Mendeley）
:mod:`obsidian`         Obsidian ZIP（Markdown 笔记 + wiki 链接）
:mod:`csv_export`       CSV ZIP（entities.csv + relationships.csv）
"""

from __future__ import annotations

from app.services.export.bibtex import bibtex_entry, render_bibtex
from app.services.export.csl_json import csl_item, render_csl_items
from app.services.export.csv_export import (
    ENTITY_COLUMNS,
    RELATIONSHIP_COLUMNS,
    build_csv_zip,
    entities_csv,
    relationships_csv,
)
from app.services.export.knowledge_json import (
    EXPORT_BATCH_SIZE,
    MAX_ENTITIES,
    MAX_RELATIONSHIPS,
    QUOTE_TEXT_CHARS,
    SPAN_LIMIT_PER_PAPER,
    ExportRepository,
    build_paper_knowledge,
    derive_entities,
    derive_relationships,
    export_meta,
    load_library,
    load_metadata_list,
    load_paper_knowledge,
    source_counts,
)
from app.services.export.mapping import (
    ACCESS_MODE,
    CLAIM_OWNER_TYPE,
    DISCLAIMER,
    MAPPING_NOTES,
    SCHEMA_VERSION,
    SCOPE_NOTE,
    escape_bibtex,
    locator_note,
    mapping_notes,
    safe_filename,
    safe_zip_entry,
)
from app.services.export.obsidian import build_obsidian_zip, entity_note, paper_note

__all__ = [
    "ACCESS_MODE",
    "CLAIM_OWNER_TYPE",
    "DISCLAIMER",
    "ENTITY_COLUMNS",
    "EXPORT_BATCH_SIZE",
    "MAPPING_NOTES",
    "MAX_ENTITIES",
    "MAX_RELATIONSHIPS",
    "QUOTE_TEXT_CHARS",
    "RELATIONSHIP_COLUMNS",
    "SCHEMA_VERSION",
    "SCOPE_NOTE",
    "SPAN_LIMIT_PER_PAPER",
    "ExportRepository",
    "bibtex_entry",
    "build_csv_zip",
    "build_obsidian_zip",
    "build_paper_knowledge",
    "csl_item",
    "derive_entities",
    "derive_relationships",
    "entities_csv",
    "entity_note",
    "escape_bibtex",
    "export_meta",
    "load_library",
    "load_metadata_list",
    "load_paper_knowledge",
    "locator_note",
    "mapping_notes",
    "paper_note",
    "relationships_csv",
    "render_bibtex",
    "render_csl_items",
    "safe_filename",
    "safe_zip_entry",
    "source_counts",
]
