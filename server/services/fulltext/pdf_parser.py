# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""L2 回退：pymupdf 解析 PDF，按坐标聚类恢复双栏阅读顺序（WP05-T3）。

为什么双栏顺序是最容易出错的一环
--------------------------------

``page.get_text("blocks")`` 返回的块顺序**不保证**是阅读顺序（同一栏内按
``y0`` 排，栏与栏之间可能交错，跨栏的标题/摘要/脚注会插在中间）。
朴素地按 ``(y0, x0)`` 排序，会把右栏第一段排到左栏最后一段之前。

本模块的做法（按页、确定性）：

1. 以该页**正文的水平范围**（所有文本块 ``x0/x1`` 的极值）为基准，
   取中线 ``mid``；
2. 宽度超过正文宽度 62% 且跨越中线的块判定为**通栏块**（标题、摘要、
   跨栏图注、脚注），其余按中心点落在 ``mid`` 左侧/右侧分入左右栏；
3. 用通栏块把页面切成若干**水平带**，带内先输出左栏（按 ``y0``、再按 ``x0``）
   再输出右栏——这就是双栏论文的真实阅读顺序；
4. 若某页左右两栏的字符占比都 < 25%，视为单栏页，直接按 ``(y0, x0)`` 排序。

被 ``FULLTEXT_MAX_PAGES`` 截断时：``char_count`` 仍统计**全部页面**的文本，
``locatable_chars`` 只统计已解析页，因此截断会在 ``coverage`` 上如实体现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.fulltext.records import (
    ParsedBlock,
    ParsedDocument,
    normalize_block_text,
)
from services.fulltext.section_map import looks_like_heading, normalize_section_name

PARSER_NAME = "pymupdf"
PARSER_VERSION = "1.0.0"

#: 通栏块判定：块宽 / 正文宽 大于该比例且跨越中线
SPANNING_WIDTH_RATIO = 0.62
#: 判定为双栏页所需的最小栏内字符占比
COLUMN_MIN_CHAR_RATIO = 0.25
#: 判定为双栏页所需的最小栏内块数
COLUMN_MIN_BLOCKS = 2
#: 页码/arXiv 页眉等噪声行的判定
_PAGE_NUMBER_ONLY_RE = re.compile(r"^\s*\d{1,4}\s*$")
_ARXIV_STAMP_RE = re.compile(r"^\s*arXiv:\s*\S+\s*\[[^\]]+\]\s*\S*\s*$", re.IGNORECASE)
_HYPHEN_LINEBREAK_RE = re.compile(r"([A-Za-z\u00c0-\u024f])-\n([a-z\u00c0-\u024f])")
_LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\u00ad": "",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
}


class NoTextLayerError(RuntimeError):
    """PDF 没有文本层（扫描件）——对应 parse_status='unavailable'。"""

    def __init__(self, message: str, *, page_count: int | None = None) -> None:
        super().__init__(message)
        self.page_count = page_count


@dataclass(slots=True)
class _TextBlock:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page_number: int

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


def _get_pymupdf():  # pragma: no cover - 依赖运行环境
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 pymupdf：FULLTEXT_PDF_PARSER=pymupdf 需要安装 pymupdf 依赖"
            ) from exc
    return pymupdf


def _clean_pdf_text(raw: str) -> str:
    text = raw or ""
    for source, target in _LIGATURES.items():
        text = text.replace(source, target)
    text = _HYPHEN_LINEBREAK_RE.sub(r"\1\2", text)  # 断词连字符还原
    return normalize_block_text(text)


def _is_noise(text: str) -> bool:
    if _PAGE_NUMBER_ONLY_RE.match(text):
        return True
    return bool(_ARXIV_STAMP_RE.match(text))


def _column_split(
    blocks: list[_TextBlock],
) -> tuple[list[int], list[int], list[int], float, float] | None:
    """返回 ``(left_idx, right_idx, spanning_idx, mid, body_width)``；单栏页返回 ``None``。"""
    if len(blocks) < 4:
        return None
    body_left = min(b.x0 for b in blocks)
    body_right = max(b.x1 for b in blocks)
    body_width = body_right - body_left
    if body_width <= 0:
        return None
    mid = body_left + body_width / 2.0
    span_threshold = body_width * SPANNING_WIDTH_RATIO

    left: list[int] = []
    right: list[int] = []
    spanning: list[int] = []
    for index, block in enumerate(blocks):
        crosses_mid = (
            block.x0 < mid - body_width * 0.02 and block.x1 > mid + body_width * 0.02
        )
        if block.width >= span_threshold and crosses_mid:
            spanning.append(index)
        elif block.center_x < mid:
            left.append(index)
        else:
            right.append(index)

    if len(left) < COLUMN_MIN_BLOCKS or len(right) < COLUMN_MIN_BLOCKS:
        return None
    total_chars = sum(len(b.text) for b in blocks) or 1
    left_chars = sum(len(blocks[i].text) for i in left)
    right_chars = sum(len(blocks[i].text) for i in right)
    if left_chars / total_chars < COLUMN_MIN_CHAR_RATIO:
        return None
    if right_chars / total_chars < COLUMN_MIN_CHAR_RATIO:
        return None
    return left, right, spanning, mid, body_width


def _sorted_indices(blocks: list[_TextBlock], indices: list[int]) -> list[int]:
    return sorted(indices, key=lambda i: (round(blocks[i].y0, 1), blocks[i].x0))


def order_page_blocks(blocks: list[_TextBlock]) -> list[_TextBlock]:
    """恢复一页的阅读顺序（双栏 → 带内左栏优先；单栏 → ``(y0, x0)``）。"""
    if len(blocks) <= 1:
        return list(blocks)

    split = _column_split(blocks)
    if split is None:
        return sorted(blocks, key=lambda b: (round(b.y0, 1), b.x0))

    left_idx, right_idx, spanning_idx, _mid, _body_width = split
    spanning_idx_sorted = _sorted_indices(blocks, spanning_idx)

    ordered: list[int] = []
    remaining = [i for i in range(len(blocks)) if i not in set(spanning_idx)]
    for separator in spanning_idx_sorted:
        separator_y = blocks[separator].y0
        band = [i for i in remaining if blocks[i].y0 < separator_y]
        remaining = [i for i in remaining if i not in set(band)]
        ordered.extend(_sorted_indices(blocks, [i for i in band if i in set(left_idx)]))
        ordered.extend(_sorted_indices(blocks, [i for i in band if i in set(right_idx)]))
        ordered.append(separator)
    left_set, right_set = set(left_idx), set(right_idx)
    ordered.extend(_sorted_indices(blocks, [i for i in remaining if i in left_set]))
    ordered.extend(_sorted_indices(blocks, [i for i in remaining if i in right_set]))
    for index in remaining:  # 理论上不会有遗漏，兜底保证不丢块
        if index not in ordered:
            ordered.append(index)
    return [blocks[i] for i in ordered]


def parse_pdf(
    content: bytes,
    *,
    source_url: str,
    content_sha256: str,
    max_pages: int | None = None,
    parser: str = PARSER_NAME,
    parser_version: str = PARSER_VERSION,
) -> ParsedDocument:
    """解析 PDF 文本层，产出带页码与 bbox 的块序列。

    :raises NoTextLayerError: PDF 无文本层（扫描件）
    :raises ValueError: 不是合法 PDF
    """
    pymupdf = _get_pymupdf()
    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:  # pragma: no cover - 依赖 pymupdf 异常类型
        raise ValueError(f"PDF 无法打开：{exc}") from exc

    try:
        page_count = int(doc.page_count)
        if max_pages is None:
            from services.fulltext.coverage import _settings_attr

            max_pages = int(_settings_attr("fulltext_max_pages", 40) or 40)
        keep_pages = page_count if max_pages <= 0 else min(page_count, max_pages)
        truncated = keep_pages < page_count

        all_page_blocks: list[list[_TextBlock]] = []
        for page_index in range(page_count):
            page = doc.load_page(page_index)
            page_blocks: list[_TextBlock] = []
            for raw_block in page.get_text("blocks"):
                if len(raw_block) < 7:
                    continue
                x0, y0, x1, y1, raw_text, _block_no, block_type = raw_block[:7]
                if int(block_type) != 0:
                    continue
                text = _clean_pdf_text(str(raw_text))
                if not text or len(text) < 2 or _is_noise(text):
                    continue
                page_blocks.append(
                    _TextBlock(
                        x0=float(x0),
                        y0=float(y0),
                        x1=float(x1),
                        y1=float(y1),
                        text=text,
                        page_number=page_index + 1,
                    )
                )
            all_page_blocks.append(order_page_blocks(page_blocks))
    finally:
        doc.close()

    total_chars = sum(len(b.text) for page in all_page_blocks for b in page)
    if total_chars == 0:
        raise NoTextLayerError(
            f"PDF 共 {page_count} 页，未提取到任何文本（无文本层，可能为扫描件）",
            page_count=page_count,
        )

    # char_count 覆盖"全部页面"，因此截断会在 coverage 上如实体现
    char_count = total_chars + 2 * max(0, sum(len(p) for p in all_page_blocks) - 1)

    blocks: list[ParsedBlock] = []
    pieces: list[str] = []
    cursor = 0
    current_section = "other"
    heading_seen = False

    for page in all_page_blocks[:keep_pages]:
        for block in page:
            section = current_section
            kind = "paragraph"
            if looks_like_heading(block.text):
                mapped = normalize_section_name(block.text)
                if mapped == "abstract":
                    current_section = "abstract"
                    heading_seen = True
                elif mapped != "other":
                    current_section = mapped
                    heading_seen = True
                section = current_section
                kind = "heading"
            elif not heading_seen:
                # 首个章节标题之前的正文：论文约定为摘要
                section = "abstract"
            else:
                section = current_section

            if cursor:
                cursor += 2
            start = cursor
            pieces.append(block.text)
            cursor = start + len(block.text)
            blocks.append(
                ParsedBlock(
                    text=block.text,
                    char_start=start,
                    char_end=cursor,
                    section_name=section,
                    page_number=block.page_number,
                    bbox=block.bbox,
                    kind=kind,
                )
            )

    full_text = "\n\n".join(pieces)
    warnings: list[str] = []
    if not heading_seen:
        warnings.append("PDF 未识别到章节标题，段落按摘要/other 归类")
    if truncated:
        warnings.append(
            f"PDF 共 {page_count} 页，超过 FULLTEXT_MAX_PAGES={max_pages}，"
            f"仅解析前 {keep_pages} 页；coverage 已体现截断"
        )

    return ParsedDocument(
        source_type="pdf",
        source_url=source_url,
        parser=parser,
        parser_version=parser_version,
        content_sha256=content_sha256,
        full_text=full_text,
        blocks=blocks,
        char_count=char_count,
        page_count=page_count,
        truncated=truncated,
        warnings=warnings,
    )
