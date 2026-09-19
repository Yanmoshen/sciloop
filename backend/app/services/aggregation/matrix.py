# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""多篇论文聚合：对比矩阵（WP08-T1）。

维度固定的 6 类语义（研究问题 / 核心方法 / 数据集 / baseline / 指标 / 结论），
外加技术路线与关键创新，共 8 列。**所有单元格内容都直接来自 ``paper_cards``
真实字段**，取不到就是 ``missing=True``（禁止编造）。

每个单元格尽量挂载可展开证据：

- ``card_field`` 证据：指向卡片字段出处（论文 + 字段名）
- ``paper_span`` 证据：仅当该论文走通全文闸门（``parse_status='ok'`` 且
  ``coverage>=0.60``）时，用 :mod:`app.services.aggregation.span_locator`
  在真实 ``paper_spans`` 中定位原文段落；定位不到则不挂 span（不编造引用）
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from app.services.aggregation.cards import (
    SCOPE_FULLTEXT,
    card_field_candidate,
    entry_note,
    entry_span,
    entry_text,
    load_cards,
    scope_of,
)
from app.services.aggregation.span_locator import locate_in_spans

logger = logging.getLogger("sciloop.wp08.matrix")

MIN_PAPERS = 2
MAX_PAPERS = 20

#: 对比维度（``sub_key`` 用于 ``experimental_setup`` 这类嵌套结构）
DIMENSIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "research_problem",
        "label": "研究问题",
        "card_field": "research_problem",
        "sub_key": None,
        "kind": "text",
    },
    {
        "key": "core_method",
        "label": "核心方法",
        "card_field": "core_method",
        "sub_key": None,
        "kind": "text",
    },
    {
        "key": "technical_route",
        "label": "技术路线",
        "card_field": "technical_route",
        "sub_key": None,
        "kind": "list",
    },
    {
        "key": "key_innovation",
        "label": "关键创新",
        "card_field": "key_innovation",
        "sub_key": None,
        "kind": "list",
    },
    {
        "key": "datasets",
        "label": "数据集",
        "card_field": "experimental_setup",
        "sub_key": "datasets",
        "kind": "list",
    },
    {
        "key": "baselines",
        "label": "Baseline",
        "card_field": "experimental_setup",
        "sub_key": "baselines",
        "kind": "list",
    },
    {
        "key": "metrics",
        "label": "评价指标",
        "card_field": "experimental_setup",
        "sub_key": "metrics",
        "kind": "list",
    },
    {
        "key": "main_conclusions",
        "label": "主要结论",
        "card_field": "main_conclusions",
        "sub_key": None,
        "kind": "list",
    },
)

#: 结论性字段（对它们做原文定位，证据价值最高）
LOCATABLE_KEYS: frozenset[str] = frozenset({"main_conclusions", "core_method", "key_innovation"})


def _field_value(card: dict[str, Any], dimension: dict[str, Any]) -> Any:
    raw = card.get(dimension["card_field"])
    sub_key = dimension["sub_key"]
    if sub_key:
        if isinstance(raw, dict):
            return raw.get(sub_key)
        return None
    return raw


def _evidence_item(
    *,
    paper_id: int,
    card_field: str,
    span: dict[str, Any] | None,
    scope: str,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": "card_field",
        "label": f"卡片字段 {card_field}（论文 {paper_id}）",
        "paper_id": int(paper_id),
        "card_field": card_field,
        "scope": scope,
        "candidate": card_field_candidate(paper_id, card_field),
        "paper_span_id": None,
        "quote_text": None,
        "section_name": None,
        "locator_kind": None,
        "match_coverage": None,
        "jump_url": f"/papers/{int(paper_id)}",
    }
    if span is not None:
        item["kind"] = "paper_span"
        item["label"] = (
            f"原文段落（{span.get('section_name') or '未标注章节'}）"
            f"· {span.get('locator_kind')}"
        )
        item["paper_span_id"] = span.get("paper_span_id")
        item["quote_text"] = span.get("quote_text")
        item["section_name"] = span.get("section_name")
        item["locator_kind"] = span.get("locator_kind")
        item["match_coverage"] = span.get("match_coverage")
        item["document_version"] = span.get("document_version")
        item["char_start"] = span.get("char_start")
        item["char_end"] = span.get("char_end")
        item["jump_url"] = f"/papers/{int(paper_id)}#span-{span.get('paper_span_id')}"
        candidate = card_field_candidate(paper_id, card_field)
        if span.get("paper_span_id") is not None:
            candidate["paper_span_id"] = int(span["paper_span_id"])
            item["candidate"] = candidate
    return item


def _cell(
    card: dict[str, Any],
    dimension: dict[str, Any],
    *,
    locate: bool,
) -> dict[str, Any]:
    paper_id = int(card["paper_id"])
    card_field = dimension["card_field"]
    scope = str(card.get("scope") or "abstract_only")
    raw = _field_value(card, dimension)
    spans = card.get("spans") or []
    can_locate = locate and scope == SCOPE_FULLTEXT and bool(spans)

    items: list[str] = []
    evidences: list[dict[str, Any]] = []
    locator_notes: list[str] = []

    if dimension["kind"] == "text":
        text = raw.strip() if isinstance(raw, str) and raw.strip() else None
        if text is None:
            return {
                "text": None,
                "items": [],
                "missing": True,
                "card_field": card_field,
                "evidence": [],
                "note": "卡片该字段为空，未编造内容",
            }
        if can_locate:
            span = locate_in_spans(text, spans)
            if span is not None:
                evidences.append(
                    _evidence_item(
                        paper_id=paper_id, card_field=card_field, span=span, scope=scope
                    )
                )
            else:
                locator_notes.append("原文段落未定位到（阈值未达），仅保留卡片字段证据")
        if not evidences:
            evidences.append(
                _evidence_item(
                    paper_id=paper_id, card_field=card_field, span=None, scope=scope
                )
            )
        return {
            "text": text,
            "items": [],
            "missing": False,
            "card_field": card_field,
            "evidence": evidences,
            "note": "；".join(locator_notes) or None,
        }

    raw_items = raw if isinstance(raw, list) else ([raw] if raw else [])
    unlocated: list[str] = []
    for entry in raw_items:
        text = entry_text(entry)
        if not text:
            continue
        items.append(text)
        span = None
        if can_locate and dimension["key"] in LOCATABLE_KEYS:
            span = locate_in_spans(text, spans)
            if span is None:
                embedded = entry_span(entry)
                if embedded and embedded.get("quote_text"):
                    unlocated.append(text[:40])
        else:
            embedded = entry_span(entry)
            if embedded and embedded.get("quote_text"):
                span = {
                    **embedded,
                    "paper_id": paper_id,
                    "locator_kind": "card_evidence_span",
                }
        if span is None and dimension["key"] in LOCATABLE_KEYS:
            note = entry_note(entry)
            if note:
                locator_notes.append(f"卡片标注 {note}")
        evidences.append(
            _evidence_item(
                paper_id=paper_id, card_field=card_field, span=span, scope=scope
            )
        )

    if not items:
        return {
            "text": None,
            "items": [],
            "missing": True,
            "card_field": card_field,
            "evidence": [],
            "note": "卡片该字段为空，未编造内容",
        }
    return {
        "text": None,
        "items": items,
        "missing": False,
        "card_field": card_field,
        "item_count": len(items),
        "evidence": evidences,
        "note": "；".join(dict.fromkeys(locator_notes)) or None,
    }


def build_matrix_payload(cards: list[dict[str, Any]], *, locate: bool = True) -> dict[str, Any]:
    """由卡片上下文构造对比矩阵（纯函数，便于单测）。"""
    rows: list[dict[str, Any]] = []
    for card in cards:
        paper_id = int(card["paper_id"])
        values: dict[str, Any] = {}
        for dimension in DIMENSIONS:
            values[dimension["key"]] = _cell(card, dimension, locate=locate)
        rows.append(
            {
                "paper_id": paper_id,
                "title": card.get("title"),
                "venue": card.get("venue"),
                "published_at": (
                    card["published_at"].isoformat()
                    if isinstance(card.get("published_at"), datetime.date)
                    else card.get("published_at")
                ),
                "citation_count": card.get("citation_count"),
                "card_version": card.get("version"),
                "scope": card.get("scope"),
                "coverage": (
                    float(card["coverage"]) if card.get("coverage") is not None else None
                ),
                "coverage_tag": card.get("coverage_tag"),
                "values": values,
            }
        )
    return {
        "dimensions": [dict(dimension) for dimension in DIMENSIONS],
        "rows": rows,
        "row_count": len(rows),
        "column_count": len(DIMENSIONS),
        "generated_by": "deterministic:paper_cards",
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def build_matrix(
    session: Any,
    paper_ids: list[int],
    *,
    locate: bool = True,
) -> dict[str, Any]:
    """读真实卡片并构造矩阵；同时给出缺失论文（不编造行）。"""
    cards = await load_cards(session, paper_ids)
    found = {int(card["paper_id"]) for card in cards}
    missing = [
        {"paper_id": int(pid), "reason": "no_parsed_card"}
        for pid in paper_ids
        if int(pid) not in found
    ]
    payload = build_matrix_payload(cards, locate=locate)
    payload["missing"] = missing
    payload["paper_ids"] = [int(pid) for pid in paper_ids]
    if missing:
        payload["notes"] = [
            f"{len(missing)} 篇论文缺少解析卡片，已在矩阵中标注缺失而非填充占位内容"
        ]
    return payload


__all__ = [
    "DIMENSIONS",
    "LOCATABLE_KEYS",
    "MAX_PAPERS",
    "MIN_PAPERS",
    "build_matrix",
    "build_matrix_payload",
    "scope_of",
]
