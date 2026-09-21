# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""全文阅读器服务（EasyPaper 四核心模块之 ③ 在 SciLoop 的落地）。

模块划分
--------
=========================  ==========================================================
``storage``                库目录（``.cache/reader-library``）路径安全化与归档 ZIP 读写
``parsing``                PyMuPDF 原文解析 + **稳定 block id** 派生规则
``versions``               不可变版本登记（原文 / 中文 / 简单英语 / 双语）与文本索引
``documents``              阅读文档创建、原文定位与查询
``state``                  阅读状态（位置 / 模式 / 字号 / 已理解 / 术语）
``annotations``            批注 CRUD 与**跨版本对齐**（success / partial / pending）
``archive``                归档 ZIP 导出与恢复（不删除既有数据）
``errors``                 统一错误码与 HTTP 状态码
=========================  ==========================================================

对外入口（其他包直接引用，勿改名）::

    from services.reader import (
        build_reading_document, derive_block_id,
        create_or_get_document, get_document, list_documents,
        register_from_manifest, list_versions, read_index, pdf_path,
    )

本轮**不做**（明确留给下一轮）：§4.6 的 LLM 阅读辅助
（``explain`` / ``ask`` / ``summary`` / ``aid``）与 ``GET /reading/{task_id}/export``
——它们依赖真实 LLM 链路与更强的证据校验设计，先把「解析 + 版本 + 状态 + 批注 + 对齐 + 归档」
主干做扎实。
"""

from __future__ import annotations

from services.reader.annotations import (
    ANNOTATION_KINDS,
    align_annotation,
    create_annotation,
    delete_annotation,
    export_payload,
    get_annotation,
    list_annotations,
    normalize_text,
    resolve_anchor,
    update_annotation,
)
from services.reader.archive import (
    build_archive,
    export_archive,
    list_archives,
    restore_archive,
)
from services.reader.documents import (
    create_or_get_document,
    document_detail,
    document_summary,
    get_document,
    list_documents,
    resolve_paper_source,
)
from services.reader.errors import ReaderError
from services.reader.parsing import (
    BLOCK_ID_RE,
    BLOCK_TYPES,
    MAX_PAGES,
    SCHEMA_VERSION,
    block_id_index,
    block_seed,
    build_reading_document,
    derive_block_id,
    iter_blocks,
    pdf_page_count,
)
from services.reader.state import get_or_create_state, patch_state
from services.reader.storage import (
    MAX_ARCHIVE_BYTES,
    MAX_SOURCE_BYTES,
    build_zip,
    default_library_dir,
    read_zip,
    safe_zip_name,
)
from services.reader.versions import (
    artifacts_root,
    build_page_map,
    build_text_index,
    get_version,
    list_versions,
    load_manifest,
    pdf_path,
    read_index,
    register_from_manifest,
    register_original,
    to_api_dict,
)

#: ``build_reading_document`` / ``parse_pdf`` 的统一别名（对外更直白）
parse_document = build_reading_document

__all__ = [
    "ANNOTATION_KINDS",
    "BLOCK_ID_RE",
    "BLOCK_TYPES",
    "MAX_ARCHIVE_BYTES",
    "MAX_PAGES",
    "MAX_SOURCE_BYTES",
    "SCHEMA_VERSION",
    "ReaderError",
    "align_annotation",
    "artifacts_root",
    "block_id_index",
    "block_seed",
    "build_archive",
    "build_page_map",
    "build_text_index",
    "build_zip",
    "create_annotation",
    "create_or_get_document",
    "default_library_dir",
    "delete_annotation",
    "derive_block_id",
    "document_detail",
    "document_summary",
    "export_archive",
    "export_payload",
    "get_annotation",
    "get_document",
    "get_or_create_state",
    "get_version",
    "iter_blocks",
    "list_annotations",
    "list_archives",
    "list_documents",
    "list_versions",
    "load_manifest",
    "normalize_text",
    "parse_document",
    "patch_state",
    "pdf_page_count",
    "pdf_path",
    "read_index",
    "read_zip",
    "register_from_manifest",
    "register_original",
    "resolve_anchor",
    "resolve_paper_source",
    "restore_archive",
    "safe_zip_name",
    "to_api_dict",
    "update_annotation",
]
