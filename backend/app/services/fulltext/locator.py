# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""原文片段定位与校验（WP05-T5，附录 D.0 硬约束③）。

**校验顺序：先比对 ``quote_sha256``，再比对字符偏移。**

这条顺序是刻意设计的：论文重新解析后，字符偏移会整体漂移，
但引用文本本身不变。若把偏移当作唯一定位依据，一次重解析就会让全库
证据失效；先比对哈希，则"文本没变"永远判定有效（``valid_by_hash``），
UI 只需提示"精确定位需重新锚定"。

verdict 取值（contracts.evidence_rules.verification_verdicts）：

===============  =============  =============  ==========================
verdict          hash_match     offset_match   含义
===============  =============  =============  ==========================
``valid``        True           True           哈希与偏移双命中（最强）
``valid_by_hash``True           False / None   文本一致，偏移失效或不可校验
``invalid``      False          —              引用文本被改动，证据不成立
===============  =============  =============  ==========================
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from app.services.fulltext.records import (
    PaperSpanRecord,
    ParsedBlock,
    quote_sha256,
)
from app.services.fulltext.text_cache import TextCache, default_text_cache

SpanLike = PaperSpanRecord | Mapping[str, Any]


def _field(span: SpanLike, name: str, default: Any = None) -> Any:
    if isinstance(span, Mapping):
        return span.get(name, default)
    return getattr(span, name, default)


def compute_offset_match(
    document_text: str | None,
    char_start: int | None,
    char_end: int | None,
    quote_text: str,
) -> bool | None:
    """偏移比对结果；文本不可用时返回 ``None``（而不是假装通过）。"""
    if document_text is None:
        return None
    if char_start is None or char_end is None:
        return False
    if char_start < 0 or char_end < char_start or char_end > len(document_text):
        return False
    return document_text[char_start:char_end] == quote_text


def verify_span(
    span: SpanLike,
    document_text: str | None = None,
    *,
    text_cache: TextCache | None = None,
) -> dict[str, Any]:
    """校验一个 ``paper_span``，返回 ``{verdict, hash_match, offset_match}``。

    :param span: ``PaperSpanRecord`` 或含同名字段的字典
    :param document_text: 该 ``document_version`` 的归一全文；
        为 ``None`` 时按 ``(paper_id, document_version)`` 从文本缓存读取，
        仍取不到则 ``offset_match=None``（**不冒充通过**）
    """
    quote_text = _field(span, "quote_text") or ""
    stored_hash = (_field(span, "quote_sha256") or "").lower()
    recomputed = quote_sha256(quote_text)
    hash_match = bool(stored_hash) and recomputed == stored_hash

    text = document_text
    if text is None:
        paper_id = _field(span, "paper_id")
        document_version = _field(span, "document_version")
        if paper_id is not None and document_version:
            cache = text_cache or default_text_cache()
            text = cache.get(int(paper_id), str(document_version))

    offset_match = compute_offset_match(
        text,
        _field(span, "char_start"),
        _field(span, "char_end"),
        quote_text,
    )

    if hash_match and offset_match is True:
        verdict = "valid"
        reason = "quote_sha256 与字符偏移均命中"
    elif hash_match:
        verdict = "valid_by_hash"
        reason = (
            "quote_sha256 命中，偏移不可校验（无全文缓存）：以引用文本为准"
            if offset_match is None
            else "quote_sha256 命中，字符偏移已失效：以引用文本为准"
        )
    else:
        verdict = "invalid"
        reason = "quote_sha256 不匹配：引用文本与落库时不一致，证据不成立"

    return {
        "verdict": verdict,
        "hash_match": hash_match,
        "offset_match": offset_match,
        "expected_quote_sha256": recomputed,
        "stored_quote_sha256": stored_hash,
        "reason": reason,
    }


def verify_spans(
    spans: Iterable[SpanLike],
    *,
    document_text: str | None = None,
    text_cache: TextCache | None = None,
) -> list[dict[str, Any]]:
    """批量校验，返回与输入等长的结果列表。"""
    results = []
    for span in spans:
        text = document_text
        if text is None:
            paper_id = _field(span, "paper_id")
            document_version = _field(span, "document_version")
            if paper_id is not None and document_version:
                cache = text_cache or default_text_cache()
                text = cache.get(int(paper_id), str(document_version))
        results.append(verify_span(span, text, text_cache=text_cache))
    return results


def relocate_quote(document_text: str | None, quote_text: str) -> tuple[int, int] | None:
    """在（可能重新解析过的）全文里重新锚定引用文本。

    用于 ``valid_by_hash`` 的修复：偏移失效但哈希命中时，可以据此把
    ``char_start/char_end`` 重新对齐，无需人工介入。找不到返回 ``None``。
    """
    if not document_text or not quote_text:
        return None
    index = document_text.find(quote_text)
    if index < 0:
        # 尝试忽略空白差异后重锚定
        normalized_target = " ".join(quote_text.split())
        normalized_doc = " ".join(document_text.split())
        if normalized_target and normalized_target in normalized_doc:
            return None  # 命中但无法映射回原偏移，交回调用方人工处理
        return None
    return (index, index + len(quote_text))


def build_span(
    *,
    paper_id: int,
    document_version: str,
    block: ParsedBlock,
) -> PaperSpanRecord:
    """由解析块构造 ``paper_spans`` 记录（``quote_sha256`` 必填）。"""
    if not block.text:
        raise ValueError("空文本不能生成 paper_span")
    if block.char_end <= block.char_start:
        raise ValueError("char_end 必须大于 char_start")
    return PaperSpanRecord(
        paper_id=paper_id,
        document_version=document_version,
        char_start=block.char_start,
        char_end=block.char_end,
        quote_text=block.text,
        quote_sha256=quote_sha256(block.text),
        section_name=block.section_name,
        page_number=block.page_number,
        bbox=block.to_span_dict()["bbox"],
    )


def build_spans(
    *,
    paper_id: int,
    document_version: str,
    blocks: Iterable[ParsedBlock],
    min_paragraph_chars: int = 40,
    max_spans: int = 0,
) -> list[PaperSpanRecord]:
    """为可定位块批量生成 ``paper_spans``。

    - 标题（``kind='heading'``）无论长短都保留，方便前端按章节跳转；
    - 普通段落需 >= ``min_paragraph_chars``，过滤掉页码、残余噪声行；
    - ``max_spans > 0`` 时按块顺序截断（默认 0 = 不限）。
    """
    spans: list[PaperSpanRecord] = []
    for block in blocks:
        if not block.is_locatable:
            continue
        if block.kind != "heading" and len(block.text) < min_paragraph_chars:
            continue
        spans.append(
            build_span(
                paper_id=paper_id,
                document_version=document_version,
                block=block,
            )
        )
        if max_spans and len(spans) >= max_spans:
            break
    return spans


def coerce_text_loader(cache: TextCache) -> Callable[[int, str], str | None]:
    """返回 ``(paper_id, document_version) -> text`` 的读取器（供 WP13 注入）。"""
    return lambda paper_id, document_version: cache.get(paper_id, document_version)
