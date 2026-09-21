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
"""空白清单（WP08-T3，附录 A.3 ``gaps``）。

每条 Gap 的形状::

    {gap_text, raised_by_paper_ids, unsolved_evidence, novelty_hint, ...}

证据纪律（contracts.forbidden_actions / evidence_rules）
--------------------------------------------------------
- **只使用真实数据**：候选来源只有两处——① ``paper_cards.limitations`` 里
  WP06 已核对过的条目；② ``paper_spans`` 中真实存在、且落在 limitation /
  future work / discussion / conclusion 章节或命中局限提示词的原文段落。
- **绝不编造未解决问题**：任何候选必须能落到至少 1 条真实 span（优先）或
  至少 1 条 ``card_field`` 证据（``limitations`` 字段本身真实存在），
  两者都拿不到的候选直接丢弃并计入 ``dropped`` 明细。
- 每条 Gap 都可展开「提出者论文 + 原文片段」：``raised_by`` 给出
  ``{paper_id, title, span_id, section_name, quote_text, jump_url, evidence}``。
- ``novelty_hint`` 由规则从真实实词生成并标注 ``novelty_hint_source``，
  **不引入任何未见于证据的新断言**。

范围诚实声明：本包不做空白反证检索（WP08 ``out_of_scope``，v1.2 列为 P1），
因此 ``unsolved_scope_note`` 明确写出「未解决」的判定范围仅限本次聚合的论文集合。
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any

from services.aggregation.cards import (
    SCOPE_FULLTEXT,
    card_field_candidate,
    entry_note,
    entry_span,
    entry_text,
    load_cards,
)
from services.aggregation.matrix import _evidence_item  # noqa: PLC2701 - 复用同一证据形状
from services.aggregation.span_locator import (
    DEFAULT_MIN_COVERAGE,
    DEFAULT_MIN_HITS,
    content_words,
    locate_in_spans,
)

logger = logging.getLogger("sciloop.wp08.gap_finder")

#: 局限 / 未来工作提示词。**只收"作者自陈局限"的强提示**——
#: 在真实库上实测发现，``cannot`` / ``constrain`` / ``difficult to`` 这类弱提示会把
#: 结论章节的表格数值也当成空白（实测 484 条噪声），故收紧为下列强模式。
CUE_RE = re.compile(
    r"(limitation|future work|future direction|we leave|left for future|"
    r"remains? open|open question|remains challenging|beyond the scope|"
    r"out of scope|unexplored|shortcoming|deserves further|not supported by|"
    r"cannot handle|we cannot|局限|不足|未来工作|尚未解决)",
    re.IGNORECASE,
)

#: 章节名提示：真实库的章节名只有 other/conclusion/method/experiment/introduction/
#: abstract/related_work，局限通常落在 ``conclusion`` 里，故这里只做**初筛**，
#: 是否算空白一律由 :data:`CUE_RE` 决定。
SECTION_CUE_RE = re.compile(
    r"(limitation|future|discussion|conclusion|remark|局限|讨论|结论|展望)",
    re.IGNORECASE,
)

#: gap 文本长度窗口（太短无信息量，太长不是一条「空白」）
MIN_GAP_CHARS = 80
MAX_GAP_CHARS = 480

#: 卡片 ``limitations`` 条目是 WP06 已核对过的结构化结果，长度门槛放宽
MIN_CARD_GAP_CHARS = 20

#: 散文性判定：过滤掉结论章节里的表格数字串（如 "BPE 0.7795 0.7787 …"）
MIN_PROSE_WORDS = 12
MIN_PROSE_ALPHA_RATIO = 0.55
PROSE_WORD_RE = re.compile(r"[A-Za-z]{2,}")

#: 参考文献行判定：真实库实测会把 bibliography 行当成空白（如 "Christoffersen, M. Damani, …"）
BIBLIO_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}[a-z]?\b")
BIBLIO_INITIAL_RE = re.compile(r"\b[A-Z]\.\s")
MAX_BIBLIO_YEARS = 1
MAX_BIBLIO_INITIALS = 2

#: 每篇论文最多贡献的 span 型空白（防止单篇淹没清单）
MAX_SPAN_GAPS_PER_PAPER = 2

#: 空白清单上限（超出会在 ``notes`` 中披露被截断数量）
MAX_GAPS = 24

#: 合并重复空白的实词 Jaccard 阈值
MERGE_JACCARD = 0.55

#: 与卡片 limitations 条目做跨来源确认所需的最小实词命中
CONFIRM_MIN_HITS = 3

UNSOLVED_SCOPE_NOTE = (
    "「未被解决」的判定范围仅限本次聚合的论文集合（本包不做空白反证检索，"
    "见 WP08 out_of_scope / 计划书 v1.2 P1 项）；跨集合的未解决性需人工或后续反证检索确认。"
)


def _clean(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text


def looks_like_prose(quote: str) -> bool:
    """散文性判定：排除表格数值行与参考文献行。

    真实库实测：不加这两道筛，空白清单会被结论章节的表格数字串（``BPE 0.7795 …``）
    与 bibliography 行（``Christoffersen, M. Damani, …``）淹没。
    """
    if not quote:
        return False
    if len(BIBLIO_YEAR_RE.findall(quote)) > MAX_BIBLIO_YEARS:
        return False
    if len(BIBLIO_INITIAL_RE.findall(quote)) > MAX_BIBLIO_INITIALS:
        return False
    if "et al." in quote:
        return False
    words = PROSE_WORD_RE.findall(quote)
    tokens = quote.split()
    if len(words) < MIN_PROSE_WORDS:
        return False
    ratio = len(words) / max(1, len(tokens))
    return ratio >= MIN_PROSE_ALPHA_RATIO


def _cue_spans(card: dict[str, Any]) -> list[dict[str, Any]]:
    """该论文里「作者自陈局限」的候选真实 span（章节初筛 + 强提示词 + 散文性）。"""
    picked: list[dict[str, Any]] = []
    for span in card.get("spans") or []:
        quote = str(span.get("quote_text") or "")
        if not quote.strip():
            continue
        section = str(span.get("section_name") or "")
        if not (SECTION_CUE_RE.search(section) or CUE_RE.search(quote)):
            continue
        if not CUE_RE.search(quote):
            continue
        if not looks_like_prose(quote):
            continue
        picked.append(span)
    return picked


def _limitation_entries(card: dict[str, Any]) -> list[Any]:
    raw = card.get("limitations")
    if isinstance(raw, list):
        return raw
    if raw:
        return [raw]
    return []


def _card_candidates(card: dict[str, Any]) -> list[dict[str, Any]]:
    """来源①：卡片 ``limitations`` 条目（优先在其真实 span 中定位）。"""
    paper_id = int(card["paper_id"])
    scope = str(card.get("scope") or "abstract_only")
    spans = card.get("spans") or []
    cue_spans = _cue_spans(card)
    candidates: list[dict[str, Any]] = []

    for entry in _limitation_entries(card):
        text = entry_text(entry)
        if not text:
            continue
        span = None
        if scope == SCOPE_FULLTEXT:
            embedded = entry_span(entry)
            span = locate_in_spans(text, cue_spans) or locate_in_spans(text, spans)
            if span is None and embedded and embedded.get("quote_text"):
                span = {
                    **embedded,
                    "paper_id": paper_id,
                    "locator_kind": "card_evidence_span",
                }
        candidates.append(
            {
                "gap_text": _clean(text),
                "paper_id": paper_id,
                "source": "card_limitations",
                "span": span,
                "card_field": "limitations",
                "note": entry_note(entry),
                "scope": scope,
            }
        )
    return candidates


def _span_candidates(card: dict[str, Any], used_span_ids: set[int]) -> list[dict[str, Any]]:
    """来源②：原文中命中**强局限提示词**的真实 span（未被卡片条目用掉的）。

    在真实库上实测过：不加这三道筛（章节初筛 / 强提示词 / 散文性）会抽出 484 条
    以表格数值为主的噪声，故加上 ``MAX_SPAN_GAPS_PER_PAPER`` 上限。
    """
    paper_id = int(card["paper_id"])
    scope = str(card.get("scope") or "abstract_only")
    if scope != SCOPE_FULLTEXT:
        return []
    card_text = " ".join(
        filter(None, (entry_text(entry) for entry in _limitation_entries(card)))
    )
    card_words = content_words(card_text)

    scored: list[tuple[int, int, dict[str, Any], str]] = []
    for span in _cue_spans(card):
        span_id = span.get("id")
        if span_id in used_span_ids:
            continue
        quote = _clean(str(span.get("quote_text") or ""))
        if not (MIN_GAP_CHARS <= len(quote) <= MAX_GAP_CHARS):
            continue
        cue_hits = len(CUE_RE.findall(quote))
        scored.append((cue_hits, -len(quote), span, quote))

    # 提示词命中越多越可能是真正的局限声明；同分取较短段落（更聚焦）
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    picked: list[dict[str, Any]] = []
    for cue_hits, _, span, quote in scored[:MAX_SPAN_GAPS_PER_PAPER]:
        confirmed = len(card_words & content_words(quote)) >= CONFIRM_MIN_HITS
        picked.append(
            {
                "gap_text": quote,
                "paper_id": paper_id,
                "source": "span_cue",
                "cue_hits": cue_hits,
                "span": {
                    "paper_span_id": span.get("id"),
                    "paper_id": paper_id,
                    "document_version": span.get("document_version"),
                    "section_name": span.get("section_name"),
                    "page_number": span.get("page_number"),
                    "char_start": span.get("char_start"),
                    "char_end": span.get("char_end"),
                    "quote_text": span.get("quote_text"),
                    "quote_sha256": span.get("quote_sha256"),
                    "locator_kind": "cue_span_match",
                },
                "card_field": "limitations",
                "note": (
                    f"原文命中 {cue_hits} 处局限提示词；"
                    + ("卡片 limitations 与该段落实词重叠，已交叉确认" if confirmed
                       else "卡片 limitations 未覆盖该段，来源为原文段落")
                ),
                "scope": scope,
            }
        )
    return picked


def _merge_key(text: str) -> set[str]:
    return content_words(text)


def _merge(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并语义重复的空白（实词 Jaccard >= MERGE_JACCARD）。"""
    clusters: list[dict[str, Any]] = []
    for candidate in candidates:
        words = _merge_key(candidate["gap_text"])
        target = None
        for cluster in clusters:
            inter = words & cluster["_words"]
            union = words | cluster["_words"]
            if union and len(inter) / len(union) >= MERGE_JACCARD:
                target = cluster
                break
        if target is None:
            clusters.append(
                {
                    "_words": set(words),
                    "gap_text": candidate["gap_text"],
                    "members": [candidate],
                }
            )
        else:
            target["_words"] |= words
            target["members"].append(candidate)
            if len(candidate["gap_text"]) > len(target["gap_text"]):
                target["gap_text"] = candidate["gap_text"]
    return clusters


def _novelty_hint(cluster: dict[str, Any]) -> tuple[str | None, str]:
    """规则化新颖性提示：只复述证据里出现过的实词与来源论文编号。"""
    words: list[str] = []
    for member in cluster["members"]:
        for word in sorted(content_words(member["gap_text"])):
            if word not in words:
                words.append(word)
    papers = sorted({member["paper_id"] for member in cluster["members"]})
    if not words:
        return None, "rule_based:no_content_terms"
    core = "、".join(words[:8])
    hint = (
        f"该空白由论文 {papers} 明确提出；可围绕其原文实词（{core}）"
        f"寻找组合 / 迁移 / 细化三类切入方式（提示由规则从证据实词生成，未引入新断言）"
    )
    return hint, "rule_based:content_terms"


def _raised_by(cluster: dict[str, Any], card_by_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, Any]] = set()
    for member in cluster["members"]:
        paper_id = int(member["paper_id"])
        span = member.get("span") or {}
        key = (paper_id, span.get("paper_span_id"))
        if key in seen:
            continue
        seen.add(key)
        card = card_by_id.get(paper_id, {})
        rows.append(
            {
                "paper_id": paper_id,
                "title": card.get("title"),
                "venue": card.get("venue"),
                "source": member["source"],
                "card_field": member.get("card_field"),
                "span_id": span.get("paper_span_id"),
                "section_name": span.get("section_name"),
                "page_number": span.get("page_number"),
                "quote_text": span.get("quote_text"),
                "locator_kind": span.get("locator_kind"),
                "note": member.get("note"),
                "evidence": _evidence_item(
                    paper_id=paper_id,
                    card_field=str(member.get("card_field") or "limitations"),
                    span=span if span.get("paper_span_id") else None,
                    scope=str(member.get("scope") or "abstract_only"),
                ),
                "jump_url": (
                    f"/papers/{paper_id}#span-{span.get('paper_span_id')}"
                    if span.get("paper_span_id")
                    else f"/papers/{paper_id}"
                ),
            }
        )
    return rows


def _unsolved_evidence(cluster: dict[str, Any]) -> list[dict[str, Any]]:
    """``unsolved_evidence``：真实 ``paper_span`` 列表（附录 A.3）。"""
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for member in cluster["members"]:
        span = member.get("span") or {}
        span_id = span.get("paper_span_id")
        if span_id is None or int(span_id) in seen:
            continue
        seen.add(int(span_id))
        items.append(
            {
                "paper_id": int(member["paper_id"]),
                "paper_span_id": int(span_id),
                "document_version": span.get("document_version"),
                "section_name": span.get("section_name"),
                "page_number": span.get("page_number"),
                "char_start": span.get("char_start"),
                "char_end": span.get("char_end"),
                "quote_text": span.get("quote_text"),
                "quote_sha256": span.get("quote_sha256"),
                "locator_kind": span.get("locator_kind"),
                "match_coverage": span.get("match_coverage"),
                "source": member["source"],
                "kind": "paper_span",
            }
        )
    return items


def build_gaps_payload(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """由卡片上下文构造空白清单（纯函数，便于单测）。"""
    card_by_id = {int(card["paper_id"]): card for card in cards}
    candidates: list[dict[str, Any]] = []
    for card in cards:
        candidates.extend(_card_candidates(card))
    used_span_ids = {
        int(item["span"]["paper_span_id"])
        for item in candidates
        if (item.get("span") or {}).get("paper_span_id") is not None
    }
    for card in cards:
        candidates.extend(_span_candidates(card, used_span_ids))

    usable: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for candidate in candidates:
        span = candidate.get("span") or {}
        has_span = span.get("paper_span_id") is not None
        has_card_field = bool(candidate.get("gap_text")) and bool(card_by_id.get(int(candidate["paper_id"])))
        if not (has_span or has_card_field):
            dropped.append(
                {
                    "paper_id": int(candidate["paper_id"]),
                    "source": candidate["source"],
                    "gap_text": candidate.get("gap_text", "")[:120],
                    "reason": "no_real_evidence",
                }
            )
            continue
        if len(candidate["gap_text"]) < MIN_CARD_GAP_CHARS or (
            candidate["source"] == "span_cue"
            and not (MIN_GAP_CHARS <= len(candidate["gap_text"]) <= MAX_GAP_CHARS)
        ):
            dropped.append(
                {
                    "paper_id": int(candidate["paper_id"]),
                    "source": candidate["source"],
                    "gap_text": candidate["gap_text"][:120],
                    "reason": (
                        f"too_short(<{MIN_CARD_GAP_CHARS})"
                        if len(candidate["gap_text"]) < MIN_CARD_GAP_CHARS
                        else f"length_out_of_window({MIN_GAP_CHARS}-{MAX_GAP_CHARS})"
                    ),
                }
            )
            continue
        usable.append(candidate)

    clusters = _merge(usable)
    gaps: list[dict[str, Any]] = []
    for cluster in clusters:
        hint, hint_source = _novelty_hint(cluster)
        unsolved = _unsolved_evidence(cluster)
        raised_by = _raised_by(cluster, card_by_id)
        papers = sorted({int(member["paper_id"]) for member in cluster["members"]})
        evidence_kinds = sorted({item["kind"] for item in unsolved}) or ["card_field"]
        gaps.append(
            {
                "gap_text": cluster["gap_text"],
                "raised_by_paper_ids": papers,
                "unsolved_evidence": unsolved,
                "novelty_hint": hint,
                # ---- 审计附加字段 ----
                "raised_by": raised_by,
                "evidence_kinds": evidence_kinds,
                "span_count": len(unsolved),
                "sources": sorted({member["source"] for member in cluster["members"]}),
                "merged_count": len(cluster["members"]),
                "novelty_hint_source": hint_source,
                "unsolved_scope_note": UNSOLVED_SCOPE_NOTE,
                "evidence_candidates": [
                    card_field_candidate(
                        int(member["paper_id"]),
                        "limitations",
                        paper_span_id=(member.get("span") or {}).get("paper_span_id"),
                        quote_text=(member.get("span") or {}).get("quote_text"),
                    )
                    for member in cluster["members"]
                ],
            }
        )

    gaps.sort(key=lambda item: (-len(item["raised_by_paper_ids"]), -item["span_count"], item["gap_text"]))
    capped_from = len(gaps)
    if capped_from > MAX_GAPS:
        gaps = gaps[:MAX_GAPS]

    notes: list[str] = []
    if not gaps:
        notes.append("本次聚合未抽取到可证据化的空白（不编造空白）")
    if capped_from > MAX_GAPS:
        notes.append(
            f"候选空白 {capped_from} 条，已按「提出者论文数 → 证据段数」排序截断为前 {MAX_GAPS} 条"
            "（未丢弃数据，可用 build_gaps_payload 复现完整列表）"
        )
    notes.append(UNSOLVED_SCOPE_NOTE)
    if dropped:
        notes.append(f"{len(dropped)} 条候选因无真实证据或过短被丢弃，明细见 dropped")

    return {
        "gaps": gaps,
        "gap_count": len(gaps),
        "candidate_count": capped_from,
        "capped": capped_from > MAX_GAPS,
        "dropped": dropped,
        "dropped_count": len(dropped),
        "generated_by": "deterministic:card_limitations_and_span_cues",
        "thresholds": {
            "min_hits": DEFAULT_MIN_HITS,
            "min_coverage": DEFAULT_MIN_COVERAGE,
            "merge_jaccard": MERGE_JACCARD,
            "min_gap_chars": MIN_GAP_CHARS,
            "max_gap_chars": MAX_GAP_CHARS,
            "min_prose_words": MIN_PROSE_WORDS,
            "min_prose_alpha_ratio": MIN_PROSE_ALPHA_RATIO,
            "max_span_gaps_per_paper": MAX_SPAN_GAPS_PER_PAPER,
            "max_gaps": MAX_GAPS,
        },
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "notes": notes,
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def build_gaps(session: Any, paper_ids: list[int]) -> dict[str, Any]:
    """读真实卡片与 span 并构造空白清单。"""
    cards = await load_cards(session, paper_ids)
    payload = build_gaps_payload(cards)
    found = {int(card["paper_id"]) for card in cards}
    missing = [int(pid) for pid in paper_ids if int(pid) not in found]
    if missing:
        payload["notes"] = list(payload["notes"]) + [
            f"论文 {missing} 无解析卡片，未参与空白抽取（不编造）"
        ]
    payload["paper_ids"] = [int(pid) for pid in paper_ids]
    return payload


__all__ = [
    "CONFIRM_MIN_HITS",
    "CUE_RE",
    "MAX_GAPS",
    "MAX_GAP_CHARS",
    "MAX_SPAN_GAPS_PER_PAPER",
    "MERGE_JACCARD",
    "MIN_CARD_GAP_CHARS",
    "MIN_GAP_CHARS",
    "MIN_PROSE_ALPHA_RATIO",
    "MIN_PROSE_WORDS",
    "SECTION_CUE_RE",
    "UNSOLVED_SCOPE_NOTE",
    "build_gaps",
    "build_gaps_payload",
    "looks_like_prose",
]
