# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""原文解析：PDF → 带**稳定 block id** 的阅读文档（EasyPaper §4.3）。

产出结构（字段名与 EasyPaper §4.3 一致）::

    {
      "schema_version": 1,
      "fingerprint": "<原文 PDF 的 sha256>",
      "title": "...", "title_source": "pdf_meta|heading|paper",
      "parse_status": "ok|partial|unavailable",
      "page_count": 12,
      "pages":    [{"page": 1, "width": .., "height": .., "block_ids": [...], "char_count": .., "is_scan": false}],
      "sections": [{"id": "sec_method", "name": "method", "title": "3 Method", "page": 3, "block_ids": [...]}],
      "blocks":   [{"id": "b_p3_9f2c1a4b7d80", "type": "paragraph", "source_text": "...",
                    "bbox": [x0, y0, x1, y1], "page": 3, "section_id": "sec_method",
                    "sentences": ["...", "..."]}],
      "warnings": [...]
    }

block id 的派生规则（**稳定 id 的唯一来源，禁止在别处另算**）
------------------------------------------------------------
::

    seed = sha256( 归一化文本 ).hexdigest()[:12]      # 内容寻址，与页内位置/块数无关
    id   = "b_p{page}_{seed}"                         # 再绑物理页锚点
    重复 = "b_p{page}_{seed}_2", "_3", ...            # 同页出现完全相同文本时按阅读顺序递推

为什么是「页 + 文本哈希」而不是序号
-----------------------------------
序号（``block-1`` / ``block-2``）在解析器升级、页码噪声被过滤、双栏顺序修正后会**整体漂移**，
「阅读位置恢复 / 批注跨版本投影 / AI 证据引用」会同时失效。内容寻址的 id：

- 与块在页内的位置、页内块数量无关 → 同一份 PDF 重复解析**必然得到相同 id**；
- 仍绑物理页 → 保留页锚点；页码确实变化时 id 如实变化，不做兼容性伪装；
- 哈希取的是**归一化文本**（大小写、行内空白、断词连字符已由
  :func:`services.fulltext.records.normalize_block_text` 与 pymupdf 解析器处理），
  排版微调不会打散 id。

同一页出现两段完全相同文本时用出现序号消歧（``_2`` / ``_3``）；序号由确定性阅读顺序决定，
因此同样可复现。

诚实性红线
----------
- **无文本层（扫描件）不伪造正文**：整篇无文本 → ``parse_status='unavailable'``，
  逐页 ``type='scan'`` 且 ``source_text=''``；仅部分页无文本 → ``parse_status='partial'``
  并在 ``warnings`` 中列出页码。
- ``type`` 只输出**确实可判定**的类别（heading / paragraph / caption / equation / scan）；
  ``table`` / ``figure`` 需版面模型（``docs/README-工程.md`` §9 缺口 8），当前不做视觉判定，
  因此不输出、也不臆测。
- 页数 / 体积超限直接抛错，不静默截断。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from services.fulltext import NoTextLayerError, parse_pdf

#: ``normalize_block_text`` 未在 ``services.fulltext`` 顶层导出，按子模块引用
from services.fulltext.records import normalize_block_text
from services.reader import storage
from services.reader.errors import (
    InvalidSourcePdfError,
    PageLimitExceededError,
    SourceTooLargeError,
)

#: 阅读文档 schema 版本（EasyPaper §4.3 冻结为 1）
SCHEMA_VERSION = 1
#: 页数上限（与翻译模块一致）
MAX_PAGES = 100
#: block 类型受控值域（table / figure 为预留：需版面模型，当前不臆测）
BLOCK_TYPES: tuple[str, ...] = (
    "heading",
    "paragraph",
    "table",
    "equation",
    "caption",
    "figure",
    "scan",
)
PARSER_NAME = "pymupdf"
PARSER_VERSION = "reader-block-v1"

#: block id 形态：``b_p<页码>_<12 位哈希>[_出现序号]``
BLOCK_ID_RE = re.compile(r"^b_p(?P<page>\d+)_(?P<seed>[0-9a-f]{12})(?:_(?P<dup>\d+))?$")
SEED_LEN = 12

_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?|tab(?:le)?|algorithm|listing|eq(?:uation)?)\.?\s*\d+",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])[\s\n]+")
#: 公式判定字符集（不含字母数字）
_MATH_CHARS = frozenset("=+−-*/^_{}[]()<>∑∫√≈≤≥±×·→∞∝∂∇αβγδεζηθλμνξπρστυφχψω")
_MATH_MIN_RATIO = 0.30
_MATH_MIN_CHARS = 4
_EQUATION_MAX_LEN = 240
_SCAN_WARNING = "未提取到任何文本，已标记 type='scan'（不得伪造正文，需 OCR/视觉链路）"


def _get_pymupdf():  # pragma: no cover - 依赖运行环境
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pymupdf：全文阅读器解析需要 pymupdf 依赖（backend/pyproject.toml 已声明）"
            ) from exc
    return pymupdf


def block_seed(text: str) -> str:
    """归一化文本的 sha256 前 12 位（稳定 id 的内容寻址依据）。"""
    return hashlib.sha256(normalize_block_text(text).encode("utf-8")).hexdigest()[:SEED_LEN]


def derive_block_id(page: int, text: str, *, duplicate_index: int = 0) -> str:
    """由 ``页 + 归一化文本`` 派生稳定 block id。

    :param duplicate_index: 同一页内**完全相同文本**的出现序号（0 为首次出现）
    """
    base = f"b_p{int(page)}_{block_seed(text)}"
    if duplicate_index <= 0:
        return base
    return f"{base}_{duplicate_index + 1}"


def classify_block(text: str, *, kind: str) -> str:
    """判定 block 类型（只输出可判定的类别，不做视觉臆测）。"""
    if kind == "heading":
        return "heading"
    stripped = text.strip()
    if _CAPTION_RE.match(stripped):
        return "caption"
    if 0 < len(stripped) <= _EQUATION_MAX_LEN:
        math_chars = sum(1 for char in stripped if char in _MATH_CHARS)
        if math_chars >= _MATH_MIN_CHARS and math_chars / len(stripped) >= _MATH_MIN_RATIO:
            return "equation"
    return "paragraph"


def split_sentences(text: str) -> list[str]:
    """按句末标点切句（保持顺序；不改写内容）。"""
    stripped = text.strip()
    if not stripped:
        return []
    return [piece.strip() for piece in _SENTENCE_SPLIT_RE.split(stripped) if piece.strip()]


def section_id_for(name: str) -> str:
    """章节 id：``sec_<归一化名>``（章节名来自受控词表，天然稳定）。"""
    return "sec_" + storage.sanitize_segment(name or "other", fallback="other").lower()


def _page_geometry(handle: Any, page_count: int) -> list[dict[str, float]]:
    """逐页真实宽高；读取失败该页记为 0（**不猜默认值**）。"""
    geometry: list[dict[str, float]] = []
    for index in range(page_count):
        try:
            rect = handle.load_page(index).rect
            geometry.append(
                {"width": round(float(rect.width), 2), "height": round(float(rect.height), 2)}
            )
        except Exception:  # pragma: no cover - 依赖 pymupdf 异常类型
            geometry.append({"width": 0.0, "height": 0.0})
    return geometry


def _pdf_metadata_title(handle: Any) -> str:
    try:
        meta = handle.metadata or {}
    except Exception:  # pragma: no cover - 依赖 pymupdf 异常类型
        return ""
    title = str(meta.get("title") or "").strip()
    return title if len(title) >= 4 else ""


def _scan_entry(page: int, geometry: list[dict[str, float]], section_id: str) -> dict[str, Any]:
    """扫描页占位 block：**空文本**，如实标记，绝不用摘要或译文顶替正文。"""
    box = geometry[page - 1] if 0 < page <= len(geometry) else {"width": 0.0, "height": 0.0}
    return {
        "id": derive_block_id(page, ""),
        "type": "scan",
        "source_text": "",
        "bbox": [0.0, 0.0, box["width"], box["height"]],
        "page": page,
        "section_id": section_id,
        "sentences": [],
    }


def build_reading_document(
    content: bytes,
    *,
    paper_id: int,
    source_url: str,
    title_fallback: str,
    max_pages: int = MAX_PAGES,
    max_bytes: int = storage.MAX_SOURCE_BYTES,
) -> dict[str, Any]:
    """把原文 PDF 解析成阅读文档（纯函数，无数据库；同输入必然同输出）。

    :raises SourceTooLargeError: 体积超限
    :raises PageLimitExceededError: 页数超限
    :raises InvalidSourcePdfError: 不是可打开的 PDF
    """
    if not content:
        raise InvalidSourcePdfError("原文为空，无法解析")
    if len(content) > max_bytes:
        raise SourceTooLargeError(
            f"原文体积 {len(content)} 字节超过上限 {max_bytes} 字节",
            detail={"size_bytes": len(content), "limit_bytes": max_bytes},
        )

    fingerprint = storage.sha256_bytes(content)
    pymupdf = _get_pymupdf()
    try:
        handle = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:  # pragma: no cover - 依赖 pymupdf 异常类型
        raise InvalidSourcePdfError(f"PDF 无法打开：{exc}") from exc

    warnings: list[str] = []
    try:
        page_count = int(handle.page_count)
        if page_count <= 0:
            raise InvalidSourcePdfError("PDF 页数为 0，无法建立阅读文档")
        if page_count > max_pages:
            raise PageLimitExceededError(
                f"PDF 共 {page_count} 页，超过阅读器上限 {max_pages} 页",
                detail={"page_count": page_count, "limit": max_pages},
            )
        geometry = _page_geometry(handle, page_count)
        meta_title = _pdf_metadata_title(handle)

        parsed = None
        text_error: str | None = None
        try:
            # max_pages=0：页数上限已在上方显式裁决，此处解析全部页面（不静默截断）
            parsed = parse_pdf(
                content,
                source_url=source_url,
                content_sha256=fingerprint,
                max_pages=0,
                parser=PARSER_NAME,
                parser_version=PARSER_VERSION,
            )
        except NoTextLayerError as exc:
            text_error = str(exc)
            warnings.append(text_error)

        parser_warnings = list(parsed.warnings) if parsed is not None else []
        parsed_blocks = list(parsed.blocks) if parsed is not None else []

        blocks_by_page: dict[int, list[dict[str, Any]]] = {
            number: [] for number in range(1, page_count + 1)
        }
        sections: list[dict[str, Any]] = []
        section_by_name: dict[str, dict[str, Any]] = {}
        per_page_chars: dict[int, int] = dict.fromkeys(range(1, page_count + 1), 0)
        seen_seeds: dict[tuple[int, str], int] = {}

        for parsed_block in parsed_blocks:
            page = int(parsed_block.page_number or 1)
            text = normalize_block_text(parsed_block.text)
            if not text:
                continue
            key = (page, block_seed(text))
            occurrence = seen_seeds.get(key, 0)
            seen_seeds[key] = occurrence + 1
            block_id = derive_block_id(page, text, duplicate_index=occurrence)

            section_name = parsed_block.section_name or "other"
            section = section_by_name.get(section_name)
            if section is None:
                section = {
                    "id": section_id_for(section_name),
                    "name": section_name,
                    "title": text if parsed_block.kind == "heading" else section_name,
                    "page": page,
                    "block_ids": [],
                }
                section_by_name[section_name] = section
                sections.append(section)
            section["block_ids"].append(block_id)

            bbox = [
                round(float(value), 2) for value in (parsed_block.bbox or (0.0, 0.0, 0.0, 0.0))
            ]
            blocks_by_page[page].append(
                {
                    "id": block_id,
                    "type": classify_block(text, kind=parsed_block.kind),
                    "source_text": text,
                    "bbox": bbox,
                    "page": page,
                    "section_id": section["id"],
                    "sentences": split_sentences(text),
                }
            )
            per_page_chars[page] = per_page_chars.get(page, 0) + len(text)

        other_section = section_by_name.get("other")
        if text_error is not None:
            other_section = {
                "id": section_id_for("other"),
                "name": "other",
                "title": "other",
                "page": 1,
                "block_ids": [],
            }
            sections.append(other_section)
            section_by_name["other"] = other_section
            for page in range(1, page_count + 1):
                entry = _scan_entry(page, geometry, other_section["id"])
                blocks_by_page[page].append(entry)
                other_section["block_ids"].append(entry["id"])
            parse_status = "unavailable"
            warnings.append(
                "整篇 PDF 无文本层：blocks 全部为 type='scan'（source_text 为空），"
                "禁止把空文本当正文展示或作为证据"
            )
        else:
            empty_pages = [page for page in range(1, page_count + 1) if not blocks_by_page[page]]
            for page in empty_pages:
                if other_section is None:
                    other_section = {
                        "id": section_id_for("other"),
                        "name": "other",
                        "title": "other",
                        "page": page,
                        "block_ids": [],
                    }
                    sections.append(other_section)
                    section_by_name["other"] = other_section
                entry = _scan_entry(page, geometry, other_section["id"])
                blocks_by_page[page].append(entry)
                other_section["block_ids"].append(entry["id"])
                warnings.append(f"第 {page} 页：{_SCAN_WARNING}")
            parse_status = "partial" if empty_pages else "ok"

        blocks = [
            entry for page in range(1, page_count + 1) for entry in blocks_by_page[page]
        ]
        pages = [
            {
                "page": page,
                "width": geometry[page - 1]["width"],
                "height": geometry[page - 1]["height"],
                "block_ids": [entry["id"] for entry in blocks_by_page[page]],
                "char_count": per_page_chars.get(page, 0),
                "is_scan": not any(entry["source_text"] for entry in blocks_by_page[page]),
            }
            for page in range(1, page_count + 1)
        ]

        if meta_title:
            title, title_source = meta_title, "pdf_meta"
        elif blocks and blocks[0]["type"] == "heading":
            title, title_source = blocks[0]["source_text"][:300], "heading"
        else:
            title, title_source = (title_fallback or "").strip() or f"paper-{paper_id}", "paper"

        warnings.extend(parser_warnings)
    finally:
        handle.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "paper_id": int(paper_id),
        "fingerprint": fingerprint,
        "source_url": source_url,
        "title": title,
        "title_source": title_source,
        "parse_status": parse_status,
        "parser": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "page_count": page_count,
        "block_count": len(blocks),
        "section_count": len(sections),
        "pages": pages,
        "sections": sections,
        "blocks": blocks,
        "warnings": list(dict.fromkeys(warnings)),
    }


def pdf_page_count(content: bytes) -> int | None:
    """读取 PDF 真实页数；不可解析时返回 ``None``（**不猜**）。

    用于登记翻译版本时建立页面对应关系：只有两边页数**真实一致**才敢声明
    ``identity`` 映射，否则如实降级为 ``unavailable`` 交给文本匹配。
    """
    if not content:
        return None
    pymupdf = _get_pymupdf()
    try:
        handle = pymupdf.open(stream=content, filetype="pdf")
    except Exception:  # pragma: no cover - 依赖 pymupdf 异常类型
        return None
    try:
        return int(handle.page_count)
    except Exception:  # pragma: no cover - 依赖 pymupdf 异常类型
        return None
    finally:
        handle.close()


def iter_blocks(document: Any) -> list[dict[str, Any]]:
    """安全取出 ``blocks``（结构异常时返回空列表，不抛业务异常）。"""
    if not isinstance(document, dict):
        return []
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        return []
    return [entry for entry in blocks if isinstance(entry, dict)]


def block_id_index(document: Any) -> dict[str, dict[str, Any]]:
    """``block_id -> block``（校验阅读位置与批注锚点时用）。"""
    return {str(entry["id"]): entry for entry in iter_blocks(document) if entry.get("id")}


__all__ = [
    "BLOCK_ID_RE",
    "BLOCK_TYPES",
    "MAX_PAGES",
    "PARSER_NAME",
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    "block_id_index",
    "block_seed",
    "build_reading_document",
    "classify_block",
    "derive_block_id",
    "iter_blocks",
    "pdf_page_count",
    "section_id_for",
    "split_sentences",
]
