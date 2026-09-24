# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""字段级原文定位（WP06-T2 / 附录 D.0 硬约束②③）。

本模块只做**一件事**：把卡片里的结论性条目（``key_innovation`` /
``main_conclusions`` / ``limitations``）挂到**真实存在**的 ``paper_spans`` 上。

不可协商的口径
--------------
1. **全文闸门**：只有 ``parse_status='ok'`` 且 ``coverage>=0.60`` 才允许正文定位
   （``contracts.evidence_rules.fulltext_gate``）。闸门不满足时所有定位字段置 ``null``。
2. **零编造**：``quote_text`` 永远是**从落库 span 文本里真实切出来**的原文切片，
   ``quote_sha256`` 由本模块对该切片重新计算 —— 模型给的引用若在原文中找不到，
   我们连一个字都不会写进证据，只写 ``evidence_span=null`` + 拒绝原因。
3. **哈希优先**：先把 ``verify_span`` 用在**源 span** 上（先比 ``quote_sha256``，
   再比字符偏移）；源 span 自身哈希不匹配 → 该 span 整条丢弃，绝不拿它当证据。
4. **带 document_version**：任何字符偏移都必须与 ``document_version`` 同时出现
   （``contracts.forbidden_actions`` 明确禁止裸偏移作为唯一定位依据）。

匹配层级（逐级降级，全部落在真实文本上）
----------------------------------------
``exact``       模型引用与 span 文本逐字命中；
``normalized``  仅空白差异（换行/多空格）命中，偏移按原文映射回真实下标；
``sentence``    模型做了轻微改写：取该 span 内相似度 >= 0.80 的**原句**作为引用
                （引用文本仍然是原文，附加 ``match_ratio`` 供审计）；
都不命中 → ``evidence_span=null`` 并登记拒绝原因（含被拒引用文本的 sha256，便于复核）。
"""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from services.fulltext import PaperSpanRecord, quote_sha256, verify_span

logger = logging.getLogger("sciloop.wp06.locator")

#: 允许正文定位的最小覆盖率（附录 D.0）
FULLTEXT_GATE_COVERAGE = 0.60
#: 允许正文定位的解析状态
FULLTEXT_GATE_STATUS = "ok"

#: 结论性字段（只有这三个字段做原文定位）
CONCLUSIVE_FIELDS: tuple[str, ...] = ("key_innovation", "main_conclusions", "limitations")
#: 每个结论性字段里承载"条目正文"的键名
FIELD_TEXT_KEYS: dict[str, str] = {
    "key_innovation": "point",
    "main_conclusions": "conclusion",
    "limitations": "limitation",
}

#: 模糊匹配阈值（保守：相似度与词覆盖率**同时**达标才接受）
#: 阈值由一次性标定脚本给出（真改写 0.659~0.724/0.80~1.00，无关句 0.171~0.438/0.14~0.50）
FUZZY_MIN_RATIO = 0.65
FUZZY_MIN_CONTAINMENT = 0.60
#: 句子候选的最小长度（过短的句子不具备区分度）
SENTENCE_MIN_CHARS = 24
#: 单条待定位引用的最大长度（避免把整段当引用做 O(n^2) 比对）
MAX_QUOTE_CHARS = 1200
#: 单个 span 参与模糊匹配的最大字符数
MAX_FUZZY_SPAN_CHARS = 6000

_WS_RE = re.compile(r"\s+")
_SENTENCE_RE = re.compile(r"[^.!?;。！？；\n]+[.!?;。！？；]?")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-_]{2,}|[\u4e00-\u9fff]{2,}")

#: 视为"模型没有给出可用引用"的占位值。
#: ⚠️ 这里是**占位值的单一来源**：``card_builder`` 判定"未提及"也用它，两边必须同步 ——
#: 漏一个的后果是**占位值被当成真引用去定位**（等于让证据链造假）。
#: 2026-09-24 卡片改中文口径后占位值用「未提及」，故一并收进本集合。
UNKNOWN_VALUES = frozenset(
    {"", "unknown", "n/a", "na", "none", "null", "-", "未提供", "未知", "未提及"}
)

#: 兼容旧的私有名（本模块内历史引用）
_UNKNOWN_VALUES = UNKNOWN_VALUES


# --------------------------------------------------------------------------- #
# 文本工具
# --------------------------------------------------------------------------- #
def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """折叠空白（连续空白 → 单个空格）并保留归一后每个字符的原文下标。"""
    chars: list[str] = []
    index: list[int] = []
    prev_space = False
    for position, char in enumerate(text):
        if char.isspace():
            if prev_space:
                continue
            chars.append(" ")
            index.append(position)
            prev_space = True
        else:
            chars.append(char)
            index.append(position)
            prev_space = False
    return "".join(chars), index


def normalize_text(text: str) -> str:
    """仅用于比较的归一文本。"""
    return _WS_RE.sub(" ", text or "").strip()


def locate_normalized(haystack: str, needle: str) -> tuple[int, int] | None:
    """忽略空白差异定位，返回在 ``haystack`` **原文**中的 ``(start, end)``。"""
    if not haystack or not needle:
        return None
    flat_hay, mapping = normalize_with_map(haystack)
    flat_needle = normalize_text(needle)
    if not flat_needle:
        return None
    position = flat_hay.find(flat_needle)
    if position < 0:
        return None
    start = mapping[position]
    end = mapping[position + len(flat_needle) - 1] + 1
    return start, end


def token_set(text: str) -> set[str]:
    """粗粒度词集合（用于模糊匹配前的廉价剪枝）。"""
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text or "")}


def sentence_windows(text: str, *, max_sentences: int = 3) -> list[tuple[int, int]]:
    """切句并返回 1..``max_sentences`` 句的连续窗口区间（真实下标）。"""
    spans = [(m.start(), m.end()) for m in _SENTENCE_RE.finditer(text or "")]
    spans = [(s, e) for s, e in spans if e > s]
    windows: list[tuple[int, int]] = []
    for i in range(len(spans)):
        for size in range(1, max_sentences + 1):
            if i + size > len(spans):
                break
            window = (spans[i][0], spans[i + size - 1][1])
            if window[1] - window[0] >= SENTENCE_MIN_CHARS:
                windows.append(window)
    return windows


def is_usable_quote(value: Any) -> bool:
    """模型给出的引用是否值得尝试定位（占位值/空白一律视为没有引用）。"""
    if value is None:
        return False
    text = str(value).strip()
    if text.lower() in _UNKNOWN_VALUES:
        return False
    return len(text) >= 8


# --------------------------------------------------------------------------- #
# 定位结果
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class LocatedQuote:
    """一次成功的定位：真实 span + 真实字符区间 + 真实哈希。"""

    span: PaperSpanRecord
    char_start: int
    char_end: int
    quote_text: str
    quote_sha256: str
    match_type: str  # exact | normalized | sentence
    match_ratio: float
    match_containment: float
    verification: dict[str, Any]
    source_span_verdict: str

    def to_evidence_span(self, *, field_name: str, entry_index: int) -> dict[str, Any]:
        """转成卡片里的 ``evidence_span``（字段口径见 contracts.evidence_rules）。"""
        return {
            "evidence_type": "paper_span",
            "paper_id": int(self.span.paper_id),
            "document_version": str(self.span.document_version),
            "span_id": self.span.id,
            "section_name": self.span.section_name,
            "page_number": self.span.page_number,
            "bbox": self.span.bbox,
            "char_start": int(self.char_start),
            "char_end": int(self.char_end),
            "quote_text": self.quote_text,
            "quote_sha256": self.quote_sha256,
            "source_span_char_span": [int(self.span.char_start), int(self.span.char_end)],
            "source_span_verdict": self.source_span_verdict,
            "verification": self.verification,
            "locator": {
                "field": field_name,
                "entry_index": entry_index,
                "match_type": self.match_type,
                "match_ratio": round(float(self.match_ratio), 4),
                "match_containment": round(float(self.match_containment), 4),
            },
        }


@dataclass(slots=True)
class LocateReport:
    """定位统计（写入卡片的 ``evidence_meta`` 供前端与审计使用）。"""

    located_count: int = 0
    unlocated_count: int = 0
    by_field: dict[str, dict[str, int]] = field(default_factory=dict)
    rejects: list[dict[str, Any]] = field(default_factory=list)
    gate: dict[str, Any] = field(default_factory=dict)

    def to_meta(self) -> dict[str, Any]:
        return {
            "located_count": self.located_count,
            "unlocated_count": self.unlocated_count,
            "by_field": self.by_field,
            "gate": self.gate,
            "rejects": self.rejects,
            "locate_rule": (
                "quote_text 为落库 span 的真实切片，quote_sha256 由服务端重新计算；"
                "定位不到一律 evidence_span=null，禁止编造引用"
            ),
        }


# --------------------------------------------------------------------------- #
# span 索引
# --------------------------------------------------------------------------- #
class SpanIndex:
    """某个 ``document_version`` 的 span 索引（含可选全文，用于偏移校验）。"""

    def __init__(
        self,
        paper_id: int,
        document_version: str,
        spans: Sequence[PaperSpanRecord],
        *,
        full_text: str | None = None,
    ) -> None:
        self.paper_id = int(paper_id)
        self.document_version = str(document_version)
        self.spans = list(spans)
        self.full_text = full_text
        self._normalized: dict[int, tuple[str, list[int]]] = {}
        self._tokens: dict[int, set[str]] = {}
        self._verdicts: dict[int, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self.spans)

    def text_of(self, span: PaperSpanRecord) -> str:
        return span.quote_text or ""

    def span_verdict(self, span: PaperSpanRecord) -> dict[str, Any]:
        """源 span 的完整性结论（哈希优先；无缓存 → offset_match=None）。"""
        key = id(span)
        cached = self._verdicts.get(key)
        if cached is None:
            cached = verify_span(span, self.full_text)
            self._verdicts[key] = cached
        return cached

    def normalized_of(self, span: PaperSpanRecord) -> tuple[str, list[int]]:
        key = id(span)
        cached = self._normalized.get(key)
        if cached is None:
            cached = normalize_with_map(self.text_of(span))
            self._normalized[key] = cached
        return cached

    def tokens_of(self, span: PaperSpanRecord) -> set[str]:
        key = id(span)
        cached = self._tokens.get(key)
        if cached is None:
            cached = token_set(self.text_of(span))
            self._tokens[key] = cached
        return cached

    # -- 定位 ---------------------------------------------------------- #
    def locate(self, quote: str) -> LocatedQuote | None:
        """在全部 span 上定位引用；返回 ``None`` 表示**定位不到**（不编造）。"""
        needle = str(quote).strip()
        if not is_usable_quote(needle):
            return None
        if len(needle) > MAX_QUOTE_CHARS:
            needle = needle[:MAX_QUOTE_CHARS]

        for span in self.spans:
            hit = self._locate_in_span(span, needle, exact_only=True)
            if hit is not None:
                return hit

        for span in self.spans:
            hit = self._locate_in_span(span, needle, normalized_only=True)
            if hit is not None:
                return hit

        needle_tokens = token_set(needle)
        best: LocatedQuote | None = None
        for span in self.spans:
            text = self.text_of(span)
            if not text or len(text) > MAX_FUZZY_SPAN_CHARS:
                continue
            span_tokens = self.tokens_of(span)
            if needle_tokens and span_tokens:
                overlap = len(needle_tokens & span_tokens) / len(needle_tokens)
                if overlap < 0.30:
                    continue
            hit = self._locate_fuzzy(span, needle)
            if hit is not None and (best is None or hit.match_ratio > best.match_ratio):
                best = hit
        return best

    # -- 内部 ---------------------------------------------------------- #
    def _locate_in_span(
        self,
        span: PaperSpanRecord,
        needle: str,
        *,
        exact_only: bool = False,
        normalized_only: bool = False,
    ) -> LocatedQuote | None:
        text = self.text_of(span)
        if not text:
            return None
        verdict = self.span_verdict(span)
        if verdict.get("verdict") == "invalid":
            # 源 span 自身文本与落库哈希不一致 → 该 span 不可作为证据
            return None

        found: tuple[int, int] | None = None
        match_type = "exact"
        ratio = 1.0
        if not normalized_only:
            position = text.find(needle)
            if position >= 0:
                found = (position, position + len(needle))
        if found is None and not exact_only:
            normalized = locate_normalized(text, needle)
            if normalized is not None:
                found = normalized
                match_type = "normalized"
                ratio = 1.0
        if found is None:
            return None

        start, end = found
        if start >= end or end > len(text):
            return None
        quote_text = text[start:end]
        return self._build(span, start, end, quote_text, match_type, ratio, 1.0, verdict)

    def _locate_fuzzy(self, span: PaperSpanRecord, needle: str) -> LocatedQuote | None:
        text = self.text_of(span)
        verdict = self.span_verdict(span)
        if verdict.get("verdict") == "invalid":
            return None
        flat_needle = normalize_text(needle)
        if not flat_needle:
            return None
        needle_tokens = token_set(flat_needle)
        best_ratio = 0.0
        best_containment = 0.0
        best_window: tuple[int, int] | None = None
        for start, end in sentence_windows(text):
            candidate = normalize_text(text[start:end])
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, flat_needle, candidate).ratio()
            candidate_tokens = token_set(candidate)
            containment = (
                len(needle_tokens & candidate_tokens) / len(needle_tokens) if needle_tokens else 0.0
            )
            if ratio < FUZZY_MIN_RATIO or containment < FUZZY_MIN_CONTAINMENT:
                continue
            if (ratio, containment) > (best_ratio, best_containment):
                best_ratio = ratio
                best_containment = containment
                best_window = (start, end)
        if best_window is None:
            return None
        start, end = best_window
        return self._build(
            span,
            start,
            end,
            text[start:end],
            "sentence",
            best_ratio,
            best_containment,
            verdict,
        )

    def _build(
        self,
        span: PaperSpanRecord,
        start: int,
        end: int,
        quote_text: str,
        match_type: str,
        ratio: float,
        containment: float,
        source_verdict: dict[str, Any],
    ) -> LocatedQuote:
        # 证据片段的哈希由服务端对真实切片重新计算（绝不采信模型给的哈希）
        digest = quote_sha256(quote_text)
        # ``start``/``end`` 是**相对 span 文本**的局部偏移；paper_spans 的字符区间
        # 是相对归一全文的文档级偏移，因此必须加上源 span 的 char_start 作为基准
        # （contracts 禁止把裸偏移当作唯一定位依据，偏移必须与 document_version 配套且可校验）
        base = int(span.char_start or 0)
        doc_start = base + int(start)
        doc_end = base + int(end)
        evidence_like = {
            "paper_id": int(span.paper_id),
            "document_version": str(span.document_version),
            "char_start": doc_start,
            "char_end": doc_end,
            "quote_text": quote_text,
            "quote_sha256": digest,
        }
        verification = verify_span(evidence_like, self.full_text)
        return LocatedQuote(
            span=span,
            char_start=doc_start,
            char_end=doc_end,
            quote_text=quote_text,
            quote_sha256=digest,
            match_type=match_type,
            match_ratio=float(ratio),
            match_containment=float(containment),
            verification={
                "verdict": verification["verdict"],
                "hash_match": verification["hash_match"],
                "offset_match": verification["offset_match"],
                "reason": verification["reason"],
            },
            source_span_verdict=str(source_verdict.get("verdict")),
        )


# --------------------------------------------------------------------------- #
# 对卡片结论性字段批量定位
# --------------------------------------------------------------------------- #
def gate_state(
    *,
    parse_status: str | None,
    coverage: float | None,
    document_version: str | None = None,
) -> dict[str, Any]:
    """判定全文闸门状态（附录 D.0 硬约束②）。"""
    allowed = (
        parse_status == FULLTEXT_GATE_STATUS
        and coverage is not None
        and (float(coverage) >= FULLTEXT_GATE_COVERAGE)
    )
    if parse_status is None:
        reason = "no_document:该论文尚无 paper_documents 记录，仅摘要级证据"
    elif parse_status != FULLTEXT_GATE_STATUS:
        reason = f"parse_status={parse_status}（要求 ok），仅摘要级证据"
    elif coverage is None:
        reason = "coverage 缺失，仅摘要级证据"
    elif float(coverage) < FULLTEXT_GATE_COVERAGE:
        reason = f"coverage={float(coverage):.3f} < {FULLTEXT_GATE_COVERAGE}，仅摘要级证据"
    else:
        reason = "parse_status=ok 且 coverage>=0.60，允许正文定位"
    return {
        "allowed": bool(allowed),
        "reason": reason,
        "parse_status": parse_status,
        "coverage": float(coverage) if coverage is not None else None,
        "document_version": document_version,
        "threshold": FULLTEXT_GATE_COVERAGE,
        "required_status": FULLTEXT_GATE_STATUS,
    }


def placeholder_entries(
    entries: Iterable[dict[str, Any]], *, text_key: str, note: str
) -> list[dict[str, Any]]:
    """闸门不满足 / 无可定位 span 时：定位字段全为 null（如实标注，不编造）。"""
    out: list[dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        item["evidence_span"] = None
        item["evidence_scope"] = "abstract_only"
        item["evidence_note"] = note
        item.pop("evidence_quote", None)
        out.append(item)
    return out


def locate_field_entries(
    field_name: str,
    entries: Sequence[dict[str, Any]],
    *,
    index: SpanIndex | None,
    gate: dict[str, Any],
    report: LocateReport,
) -> list[dict[str, Any]]:
    """对单个结论性字段的全部条目做定位，并回填 ``evidence_span``。"""
    text_key = FIELD_TEXT_KEYS.get(field_name, "point")
    stats = report.by_field.setdefault(field_name, {"total": 0, "located": 0, "unlocated": 0})
    out: list[dict[str, Any]] = []
    for position, entry in enumerate(entries):
        item = dict(entry)
        quote = item.pop("evidence_quote", None)
        stats["total"] += 1
        if not gate.get("allowed") or index is None or len(index) == 0:
            item["evidence_span"] = None
            item["evidence_scope"] = "abstract_only"
            item["evidence_note"] = (
                "fulltext_gate_not_satisfied"
                if not gate.get("allowed")
                else "no_paper_spans:该论文无可用 paper_spans 片段"
            )
            stats["unlocated"] += 1
            report.unlocated_count += 1
            out.append(item)
            continue

        if not is_usable_quote(quote):
            item["evidence_span"] = None
            item["evidence_scope"] = "fulltext"
            item["evidence_note"] = "missing_evidence_quote:模型未给出可核验的原文引用"
            stats["unlocated"] += 1
            report.unlocated_count += 1
            out.append(item)
            continue

        located = index.locate(str(quote))
        if located is None:
            item["evidence_span"] = None
            item["evidence_scope"] = "fulltext"
            item["evidence_note"] = "unlocated:quote_not_found_in_paper_spans"
            report.unlocated_count += 1
            stats["unlocated"] += 1
            report.rejects.append(
                {
                    "field": field_name,
                    "entry_index": position,
                    "reason": "quote_not_found_in_paper_spans",
                    "rejected_quote_sha256": quote_sha256(str(quote).strip()),
                    "rejected_quote_chars": len(str(quote).strip()),
                    "rejected_quote_head": str(quote).strip()[:60],
                    "text_key": text_key,
                }
            )
            out.append(item)
            continue

        item["evidence_span"] = located.to_evidence_span(
            field_name=field_name, entry_index=position
        )
        item["evidence_scope"] = "fulltext"
        item["evidence_note"] = f"located:{located.match_type}"
        report.located_count += 1
        stats["located"] += 1
        out.append(item)
    return out


def locate_card(
    card: dict[str, Any],
    *,
    index: SpanIndex | None,
    gate: dict[str, Any],
) -> tuple[dict[str, Any], LocateReport]:
    """对卡片的三个结论性字段做字段级定位。

    返回 ``(回填 evidence_span 的卡片, 定位报告)``。
    """
    report = LocateReport(gate=dict(gate))
    resolved: dict[str, Any] = dict(card)
    for field_name in CONCLUSIVE_FIELDS:
        entries = card.get(field_name)
        if not isinstance(entries, list):
            continue
        resolved[field_name] = locate_field_entries(
            field_name,
            [item for item in entries if isinstance(item, dict)],
            index=index,
            gate=gate,
            report=report,
        )

    if not gate.get("allowed"):
        report.rejects.append(
            {
                "field": "*",
                "entry_index": None,
                "reason": "fulltext_gate_not_satisfied",
                "detail": gate.get("reason"),
            }
        )
    report.rejects = report.rejects[:40]
    return resolved, report


def verify_located_span(
    evidence_span: dict[str, Any] | None,
    *,
    document_text: str | None = None,
    text_cache: Any | None = None,
) -> dict[str, Any]:
    """校验卡片里已落库的 ``evidence_span``（哈希优先，复用 WP05 的 ``verify_span``）。

    ``evidence_span`` 为 ``None`` 时返回 ``{'verdict': 'absent'}`` —— 未定位就是未定位，
    不给任何"可能有效"的模糊结论。供 WP07 高亮与 WP13 Claim 级证据复用。
    """
    if not evidence_span:
        return {
            "verdict": "absent",
            "hash_match": False,
            "offset_match": None,
            "reason": "该条目未定位到原文（evidence_span=null），不得当作证据使用",
        }
    payload = {
        "paper_id": evidence_span.get("paper_id"),
        "document_version": evidence_span.get("document_version"),
        "char_start": evidence_span.get("char_start"),
        "char_end": evidence_span.get("char_end"),
        "quote_text": evidence_span.get("quote_text"),
        "quote_sha256": evidence_span.get("quote_sha256"),
    }
    return verify_span(payload, document_text, text_cache=text_cache)


__all__ = [
    "CONCLUSIVE_FIELDS",
    "FIELD_TEXT_KEYS",
    "FULLTEXT_GATE_COVERAGE",
    "FULLTEXT_GATE_STATUS",
    "FUZZY_MIN_CONTAINMENT",
    "FUZZY_MIN_RATIO",
    "LocateReport",
    "LocatedQuote",
    "SpanIndex",
    "UNKNOWN_VALUES",
    "gate_state",
    "is_usable_quote",
    "locate_card",
    "locate_field_entries",
    "locate_normalized",
    "normalize_text",
    "normalize_with_map",
    "placeholder_entries",
    "sentence_windows",
    "token_set",
    "verify_located_span",
]
