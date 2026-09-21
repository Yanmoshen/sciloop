# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""基于实词覆盖率的 span 定位（WP08 内部工具，供矩阵 / 空白清单复用）。

为什么不用逐字匹配：WP06 卡片里的结论性条目是模型对原文的**改写**，
逐字 ILIKE 在真实库上的命中率实测仅 2/12（见 ``_progress.json`` 的证据）。

本模块只做「定位候选」，**不做事实判定**：命中结果一律交给 WP05 的
:func:`services.fulltext.verify_span` 做哈希优先校验，校验不通过则丢弃，
绝不产出没有定位依据的证据（contracts.evidence_rules）。
"""

from __future__ import annotations

import re
from typing import Any

#: 实词提取（≥4 字母的英文词；数字与短词不参与，避免噪声匹配）
WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")

#: 停用词（不影响语义的虚词 / 高频词）
STOP_WORDS: frozenset[str] = frozenset(
    {
        "about", "after", "also", "among", "another", "are", "because", "been",
        "before", "being", "between", "both", "but", "can", "could", "does",
        "during", "each", "even", "from", "further", "have", "having", "here",
        "however", "into", "its", "itself", "many", "may", "more", "most",
        "much", "must", "not", "only", "other", "others", "our", "ours",
        "over", "same", "should", "some", "such", "than", "that", "the",
        "their", "them", "then", "there", "these", "they", "this", "those",
        "through", "thus", "under", "until", "very", "was", "were", "what",
        "when", "where", "which", "while", "will", "with", "within", "without",
        "would", "your",
    }
)

#: 定位方式（写入证据块，前端据此显示"逐字命中 / 语义段命中"）
LOCATOR_EXACT = "exact_phrase"
LOCATOR_OVERLAP = "content_overlap"
LOCATOR_CARD = "card_evidence_span"

#: 覆盖率定位阈值（在真实 13 张卡片上实测：hit>=5 且 cov>=0.5 命中 11/12）
DEFAULT_MIN_HITS = 5
DEFAULT_MIN_COVERAGE = 0.5


def stem(word: str) -> str:
    """极轻量词干化（只去常见后缀，避免引入额外依赖）。"""
    value = word.lower().strip("-")
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ed", "es", "s"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            return value[: -len(suffix)]
    return value


def content_words(value: str | None) -> set[str]:
    """抽取实词集合（词干化 + 去停用词）。"""
    if not value:
        return set()
    return {
        stem(raw)
        for raw in WORD_RE.findall(value)
        if raw.lower() not in STOP_WORDS
    }


def exact_phrase(value: str | None, *, words: int = 8) -> str:
    """取前 ``words`` 个实词拼成的短语（用于逐字命中探测）。"""
    raw = WORD_RE.findall(value or "")
    return " ".join(raw[:words]) if len(raw) >= words else ""


def overlap_score(needle_words: set[str], haystack_text: str) -> tuple[int, float]:
    """返回 ``(命中实词数, 覆盖率)``。"""
    if not needle_words:
        return 0, 0.0
    hit = len(needle_words & content_words(haystack_text))
    return hit, hit / len(needle_words)


def best_overlap(
    needle: str | None,
    spans: list[dict[str, Any]],
    *,
    min_hits: int = DEFAULT_MIN_HITS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> tuple[dict[str, Any], int, float] | None:
    """在 ``spans`` 中挑实词覆盖最好的一个；不达阈值返回 ``None``。"""
    needle_words = content_words(needle)
    if len(needle_words) < min_hits:
        return None
    best: tuple[tuple[int, float], dict[str, Any], int, float] | None = None
    for span in spans:
        hit, coverage = overlap_score(needle_words, str(span.get("quote_text") or ""))
        if hit < min_hits or coverage < min_coverage:
            continue
        key = (hit, coverage)
        if best is None or key > best[0]:
            best = (key, span, hit, coverage)
    if best is None:
        return None
    _, span, hit, coverage = best
    return span, hit, coverage


def best_exact(text: str | None, spans: list[dict[str, Any]]) -> dict[str, Any] | None:
    """逐字短语命中（优先尝试 8/5/4 词，命中即返回）。"""
    for size in (8, 5, 4):
        phrase = exact_phrase(text, words=size)
        if not phrase:
            continue
        needle = phrase.lower()
        for span in spans:
            haystack = str(span.get("quote_text") or "").lower()
            if needle and needle in haystack:
                return span
    return None


def span_payload(
    span: dict[str, Any],
    *,
    locator_kind: str,
    match_hits: int | None = None,
    match_coverage: float | None = None,
) -> dict[str, Any]:
    """把一行 ``paper_spans`` 转成证据块（字段与 contracts.evidence_rules 对齐）。"""
    return {
        "paper_span_id": span.get("id"),
        "paper_id": span.get("paper_id"),
        "document_version": span.get("document_version"),
        "section_name": span.get("section_name"),
        "page_number": span.get("page_number"),
        "char_start": span.get("char_start"),
        "char_end": span.get("char_end"),
        "quote_text": span.get("quote_text"),
        "quote_sha256": span.get("quote_sha256"),
        "locator_kind": locator_kind,
        "match_hits": match_hits,
        "match_coverage": None if match_coverage is None else round(match_coverage, 3),
    }


def locate_in_spans(
    text: str | None,
    spans: list[dict[str, Any]],
    *,
    min_hits: int = DEFAULT_MIN_HITS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> dict[str, Any] | None:
    """三级定位：逐字短语 → 实词覆盖 → 放弃（返回 ``None``，绝不编造）。"""
    span = best_exact(text, spans)
    if span is not None:
        return span_payload(span, locator_kind=LOCATOR_EXACT)
    got = best_overlap(text, spans, min_hits=min_hits, min_coverage=min_coverage)
    if got is None:
        return None
    span, hits, coverage = got
    return span_payload(
        span,
        locator_kind=LOCATOR_OVERLAP,
        match_hits=hits,
        match_coverage=coverage,
    )


__all__ = [
    "DEFAULT_MIN_COVERAGE",
    "DEFAULT_MIN_HITS",
    "LOCATOR_CARD",
    "LOCATOR_EXACT",
    "LOCATOR_OVERLAP",
    "best_exact",
    "best_overlap",
    "content_words",
    "exact_phrase",
    "locate_in_spans",
    "overlap_score",
    "span_payload",
    "stem",
]
