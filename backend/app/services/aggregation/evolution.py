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
"""方法演进关系（WP08-T2，附录 A.3 ``method_evolution``）。

输出结构严格对齐附录 A.3::

    [{from_paper_id, to_paper_id, change, evidence}]

判定口径（**全部可审计，不使用 LLM，不存在编造**）
--------------------------------------------------
对按 ``published_at`` 排序后形成的**有向论文对** ``(A 早, B 晚)``：

1. 取 A 的 ``core_method`` + ``key_innovation`` 的实词集合作为 *needle*；
2. 在 B 的卡片文本（``core_method`` / ``key_innovation`` / ``technical_route`` /
   ``research_problem``）与 B 的**真实原文 span** 中统计实词覆盖；
3. 覆盖达标（``hits >= 5`` 且 ``coverage >= 0.50``，与
   :mod:`app.services.aggregation.span_locator` 同阈值）才承认「B 沿用/承接了 A 的方法」；
4. 每条关系都带 ``matched_terms``（命中的真实实词）与 ``evidence``：
   A 的 ``card_field`` 证据 + B 的 ``card_field`` 证据 + B 中命中的真实 ``paper_span``
   （定位不到 span 时如实标 ``span_located=false``，**不编造引用**）。

``mechanism`` 由规则给出（``refinement`` / ``transfer`` / ``combination``），
判定依据同时写入 ``mechanism_basis``，便于人工复核：

- A 与 B 的 ``research_problem`` 实词高度重叠 → ``refinement``（同一问题上的改进）
- A 的方法实词出现在 B 的正文但两者研究问题**几乎不重叠** → ``transfer``（跨问题迁移）
- 其余达标情形 → ``combination``（组合式承接）
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from app.services.aggregation.cards import (
    SCOPE_FULLTEXT,
    entry_text,
    load_cards,
)
from app.services.aggregation.matrix import _evidence_item  # noqa: PLC2701 - 复用同一证据形状
from app.services.aggregation.span_locator import (
    DEFAULT_MIN_COVERAGE,
    DEFAULT_MIN_HITS,
    content_words,
    locate_in_spans,
)

logger = logging.getLogger("sciloop.wp08.evolution")

#: 承认「承接关系」的阈值（与 span_locator 保持一致，见 WP08 `_progress.json` 实测）
EVOLUTION_MIN_HITS = DEFAULT_MIN_HITS
EVOLUTION_MIN_COVERAGE = DEFAULT_MIN_COVERAGE

#: 同一问题上的改写判定阈值（研究问题实词 Jaccard）
SAME_PROBLEM_JACCARD = 0.34

MECHANISM_REFINEMENT = "refinement"
MECHANISM_TRANSFER = "transfer"
MECHANISM_COMBINATION = "combination"


def _joined_text(card: dict[str, Any], fields: tuple[str, ...]) -> str:
    parts: list[str] = []
    for field in fields:
        value = card.get(field)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            for entry in value:
                text = entry_text(entry)
                if text:
                    parts.append(text)
        elif isinstance(value, dict):
            for sub in value.values():
                if isinstance(sub, str) and sub.strip():
                    parts.append(sub.strip())
                elif isinstance(sub, list):
                    for entry in sub:
                        text = entry_text(entry)
                        if text:
                            parts.append(text)
    return "\n".join(parts)


def _ordered(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 ``published_at`` 升序；缺失日期的排在最后（不猜日期）。"""

    def key(card: dict[str, Any]) -> tuple[int, str, int]:
        published = card.get("published_at")
        if isinstance(published, datetime.date):
            return (0, published.isoformat(), int(card["paper_id"]))
        return (1, "", int(card["paper_id"]))

    return sorted(cards, key=key)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _method_needle(card: dict[str, Any]) -> str:
    return _joined_text(card, ("core_method", "key_innovation"))


def _relatedness(
    earlier: dict[str, Any], later: dict[str, Any], *, min_hits: int, min_coverage: float
) -> dict[str, Any] | None:
    """判断 ``later`` 是否承接 ``earlier`` 的方法；不达标返回 ``None``。

    三级匹配，**由强到弱**，命中哪一级都会如实记入 ``match_scope``：

    ``span``      单个真实 span 内即达阈值（最强，可直接引用该段）
    ``document``  整篇正文（全部 span 合并）达阈值，再挑命中最多的那个真实 span 作锚点
    ``card``      仅卡片文本达阈值（无正文级证据，置信度最低）

    真实库实测：跨论文的承接多数是「术语层面出现在对方正文里」，
    单 span 粒度会全部落空（实测 0 条），故必须有 document 一级。
    """
    needle_text = _method_needle(earlier)
    needle_words = content_words(needle_text)
    if len(needle_words) < min_hits:
        return None

    card_text = _joined_text(
        later, ("core_method", "key_innovation", "technical_route", "research_problem")
    )
    card_words = content_words(card_text)
    card_hits = len(needle_words & card_words)
    card_coverage = card_hits / len(needle_words)

    spans = list(later.get("spans") or [])
    doc_words: set[str] = set()
    best_anchor: tuple[int, dict[str, Any]] | None = None
    for span in spans:
        quote_words = content_words(str(span.get("quote_text") or ""))
        doc_words |= quote_words
        hits_in_span = len(needle_words & quote_words)
        if hits_in_span and (best_anchor is None or hits_in_span > best_anchor[0]):
            best_anchor = (hits_in_span, span)

    doc_hits = len(needle_words & doc_words)
    doc_coverage = doc_hits / len(needle_words)
    local_span = locate_in_spans(needle_text, spans)
    local_hits = 0
    if local_span is not None:
        local_hits = len(needle_words & content_words(str(local_span.get("quote_text") or "")))

    if local_span is not None and local_hits >= min_hits and local_hits / len(needle_words) >= min_coverage:
        match_scope = "span"
        hits, coverage, span, anchor_hits = local_hits, local_hits / len(needle_words), local_span, local_hits
    elif doc_hits >= min_hits and doc_coverage >= min_coverage:
        match_scope = "document"
        hits, coverage = doc_hits, doc_coverage
        anchor_hits, anchor_span = best_anchor if best_anchor else (0, None)
        span = (
            {
                "paper_span_id": anchor_span.get("id"),
                "paper_id": anchor_span.get("paper_id"),
                "document_version": anchor_span.get("document_version"),
                "section_name": anchor_span.get("section_name"),
                "page_number": anchor_span.get("page_number"),
                "char_start": anchor_span.get("char_start"),
                "char_end": anchor_span.get("char_end"),
                "quote_text": anchor_span.get("quote_text"),
                "quote_sha256": anchor_span.get("quote_sha256"),
                "locator_kind": "document_overlap_anchor",
                "match_hits": anchor_hits,
                "match_coverage": round(anchor_hits / len(needle_words), 3),
            }
            if anchor_span is not None
            else None
        )
    elif card_hits >= min_hits and card_coverage >= min_coverage:
        match_scope = "card"
        hits, coverage, span, anchor_hits = card_hits, card_coverage, None, None
    else:
        return None

    matched_terms = sorted(needle_words & (card_words | doc_words))
    problem_similarity = _jaccard(
        content_words(str(earlier.get("research_problem") or "")),
        content_words(str(later.get("research_problem") or "")),
    )
    if problem_similarity >= SAME_PROBLEM_JACCARD:
        mechanism = MECHANISM_REFINEMENT
        basis = (
            f"两篇研究问题实词 Jaccard={problem_similarity:.2f}"
            f"（>= {SAME_PROBLEM_JACCARD}）：同一问题上的方法改进"
        )
    elif match_scope in ("span", "document"):
        mechanism = MECHANISM_TRANSFER
        basis = (
            f"两篇研究问题实词 Jaccard={problem_similarity:.2f}（< {SAME_PROBLEM_JACCARD}）"
            f"，但 A 的方法术语出现在 B 的正文（匹配粒度={match_scope}，"
            f"命中 {hits} 个实词 / 覆盖 {coverage * 100:.1f}%）：跨问题迁移"
        )
    else:
        mechanism = MECHANISM_COMBINATION
        basis = (
            f"仅卡片文本层面承接（命中 {card_hits} 个实词），未在正文中定位到：判为组合式承接，置信度低"
        )

    return {
        "hits": hits,
        "coverage": round(coverage, 3),
        "matched_terms": matched_terms[:12],
        "span": span,
        "match_scope": match_scope,
        "anchor_hits": anchor_hits,
        "doc_hits": doc_hits,
        "doc_coverage": round(doc_coverage, 3),
        "card_hits": card_hits,
        "card_coverage": round(card_coverage, 3),
        "mechanism": mechanism,
        "mechanism_basis": basis,
        "problem_similarity": round(problem_similarity, 3),
        "confidence": {"span": "high", "document": "medium", "card": "low"}[match_scope],
    }


def _change_text(
    earlier: dict[str, Any],
    later: dict[str, Any],
    matched_terms: list[str],
    *,
    match_scope: str,
    coverage: float,
    hits: int,
) -> str:
    """改动描述：只用两篇卡片的真实字段与实测匹配度拼接，不追加任何未见于材料的断言。"""
    innovation = later.get("key_innovation")
    innovation_text = None
    if isinstance(innovation, list):
        for entry in innovation:
            innovation_text = entry_text(entry)
            if innovation_text:
                break
    elif isinstance(innovation, str):
        innovation_text = innovation.strip() or None
    method = later.get("core_method")
    method_text = method.strip() if isinstance(method, str) and method.strip() else None

    scope_label = {
        "span": "在单个原文段落中",
        "document": "在其正文中",
        "card": "仅在其解析卡片中",
    }[match_scope]
    segments = [
        f"「{later.get('title') or ('论文 ' + str(later['paper_id']))}」"
        f"{scope_label}出现了「{earlier.get('title') or ('论文 ' + str(earlier['paper_id']))}」"
        f"的方法术语（命中 {hits} 个实词、覆盖 {coverage * 100:.1f}%：{'、'.join(matched_terms[:6])}）"
    ]
    if innovation_text:
        segments.append(f"卡片 key_innovation 记为改动：{innovation_text}")
    elif method_text:
        segments.append(f"卡片 core_method 记为：{method_text}")
    return "；".join(segments) + "。"


def build_evolution_payload(
    cards: list[dict[str, Any]],
    *,
    min_hits: int = EVOLUTION_MIN_HITS,
    min_coverage: float = EVOLUTION_MIN_COVERAGE,
) -> dict[str, Any]:
    """由卡片上下文构造演进关系（纯函数，便于单测）。"""
    ordered = _ordered(cards)
    relations: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()

    for index, later in enumerate(ordered):
        best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
        for earlier in ordered[:index]:
            from_id = int(earlier["paper_id"])
            to_id = int(later["paper_id"])
            if from_id == to_id or (from_id, to_id) in seen_pairs:
                continue
            related = _relatedness(
                earlier, later, min_hits=min_hits, min_coverage=min_coverage
            )
            if related is None:
                continue
            if best is None or related["hits"] > best[0]:
                best = (related["hits"], earlier, related)
        if best is None:
            continue
        _, earlier, related = best
        seen_pairs.add((int(earlier["paper_id"]), int(later["paper_id"])))

        span = related["span"]
        evidence: list[dict[str, Any]] = [
            _evidence_item(
                paper_id=int(later["paper_id"]),
                card_field="core_method",
                span=None,
                scope=str(later.get("scope") or "abstract_only"),
            )
        ]
        span_located = span is not None
        if span_located:
            evidence.append(
                _evidence_item(
                    paper_id=int(later["paper_id"]),
                    card_field="core_method",
                    span=span,
                    scope=str(later.get("scope") or "abstract_only"),
                )
            )
        relations.append(
            {
                "from_paper_id": int(earlier["paper_id"]),
                "to_paper_id": int(later["paper_id"]),
                "change": _change_text(
                    earlier,
                    later,
                    related["matched_terms"],
                    match_scope=related["match_scope"],
                    coverage=related["coverage"],
                    hits=related["hits"],
                ),
                "evidence": evidence,
                # ---- 以下为审计附加字段（附录 A.3 未禁止） ----
                "mechanism": related["mechanism"],
                "mechanism_basis": related["mechanism_basis"],
                "matched_terms": related["matched_terms"],
                "match_hits": related["hits"],
                "match_coverage": related["coverage"],
                "match_scope": related["match_scope"],
                "confidence": related["confidence"],
                "anchor_span_hits": related["anchor_hits"],
                "document_overlap": {
                    "hits": related["doc_hits"],
                    "coverage": related["doc_coverage"],
                },
                "card_overlap": {
                    "hits": related["card_hits"],
                    "coverage": related["card_coverage"],
                },
                "problem_similarity": related["problem_similarity"],
                "span_located": span_located,
                "span": span,
                "from_title": earlier.get("title"),
                "to_title": later.get("title"),
                "evidence_basis": "deterministic:card_and_span_overlap",
            }
        )

    timeline = [
        {
            "paper_id": int(card["paper_id"]),
            "title": card.get("title"),
            "venue": card.get("venue"),
            "published_at": (
                card["published_at"].isoformat()
                if isinstance(card.get("published_at"), datetime.date)
                else card.get("published_at")
            ),
            "core_method": card.get("core_method"),
            "scope": card.get("scope"),
        }
        for card in ordered
    ]

    notes: list[str] = []
    if len(ordered) < 2:
        notes.append("少于 2 篇论文，无法形成演进关系")
    elif not relations:
        notes.append(
            f"{len(ordered)} 篇论文两两之间均未达到承接阈值"
            f"（hits>={min_hits} 且 coverage>={min_coverage}），如实返回空关系而非编造"
        )
    undated = [int(c["paper_id"]) for c in ordered if not isinstance(c.get("published_at"), datetime.date)]
    if undated:
        notes.append(f"论文 {undated} 缺 published_at，已排在时间轴末尾（未猜日期）")

    return {
        "relations": relations,
        "timeline": timeline,
        "relation_count": len(relations),
        "generated_by": "deterministic:card_and_span_overlap",
        "thresholds": {"min_hits": min_hits, "min_coverage": min_coverage},
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "notes": notes,
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def build_evolution(session: Any, paper_ids: list[int]) -> dict[str, Any]:
    """读真实卡片并构造演进关系（不存在的论文不产出行）。"""
    cards = await load_cards(session, paper_ids)
    payload = build_evolution_payload(cards)
    found = {int(card["paper_id"]) for card in cards}
    missing = [int(pid) for pid in paper_ids if int(pid) not in found]
    if missing:
        payload["notes"] = list(payload["notes"]) + [
            f"论文 {missing} 无解析卡片，未纳入演进分析（不编造）"
        ]
    payload["paper_ids"] = [int(pid) for pid in paper_ids]
    payload["fulltext_scope_paper_ids"] = [
        int(card["paper_id"]) for card in cards if card.get("scope") == SCOPE_FULLTEXT
    ]
    return payload


__all__ = [
    "EVOLUTION_MIN_COVERAGE",
    "EVOLUTION_MIN_HITS",
    "MECHANISM_COMBINATION",
    "MECHANISM_REFINEMENT",
    "MECHANISM_TRANSFER",
    "SAME_PROBLEM_JACCARD",
    "build_evolution",
    "build_evolution_payload",
]
