# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""聚合域的卡片读取层（WP08 内部工具）。

一次把「论文元数据 + 最新解析卡片 + 可用文档 + 全部 span」读进内存，
供对比矩阵 / 方法演进 / 空白清单三个消费者复用，避免 N+1 查询。

只读、不写库、不调用 LLM。取不到的字段一律 ``None``（禁止编造）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger("sciloop.wp08.cards")

#: 卡片 8 字段（附录 A.2 / WC06 契约）
CARD_FIELDS: tuple[str, ...] = (
    "research_problem",
    "core_method",
    "key_innovation",
    "technical_route",
    "experimental_setup",
    "main_conclusions",
    "limitations",
    "transferable",
)

#: 全文证据闸门（contracts.evidence_rules.fulltext_gate）
FULLTEXT_GATE_COVERAGE = 0.60

SCOPE_FULLTEXT = "fulltext"
SCOPE_ABSTRACT_ONLY = "abstract_only"

_CARDS_SQL = """
SELECT pc.paper_id,
       pc.version,
       pc.research_problem,
       pc.core_method,
       pc.key_innovation,
       pc.technical_route,
       pc.experimental_setup,
       pc.main_conclusions,
       pc.limitations,
       pc.transferable,
       p.title,
       p.abstract,
       p.venue,
       p.published_at,
       p.citation_count,
       d.document_version,
       d.parse_status,
       d.coverage
  FROM paper_cards pc
  JOIN papers p ON p.id = pc.paper_id
  LEFT JOIN LATERAL (
       SELECT document_version, parse_status, coverage
         FROM paper_documents d
        WHERE d.paper_id = pc.paper_id
        ORDER BY (d.parse_status = 'ok') DESC, d.coverage DESC NULLS LAST, d.id DESC
        LIMIT 1) d ON TRUE
 WHERE pc.paper_id = ANY(:paper_ids)
   AND pc.version = (SELECT max(c2.version) FROM paper_cards c2
                      WHERE c2.paper_id = pc.paper_id)
"""

_SPANS_SQL = """
SELECT id, paper_id, document_version, section_name, page_number,
       char_start, char_end, quote_text, quote_sha256
  FROM paper_spans
 WHERE paper_id = ANY(:paper_ids)
 ORDER BY paper_id, id
"""


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def entry_text(entry: Any) -> str | None:
    """卡片数组条目 → 可读文本。

    条目可能是 ``str``，也可能是 ``{"limitation": "...", "evidence_span": {...}}``
    这类结构；取不到文本返回 ``None``（不编造）。
    """
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry.strip() or None
    if isinstance(entry, dict):
        for key in (
            "limitation",
            "conclusion",
            "innovation",
            "item",
            "text",
            "value",
            "route",
            "transferable",
            "name",
        ):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    return str(entry).strip() or None


def entry_span(entry: Any) -> dict[str, Any] | None:
    """条目自带（WP06 已核对过）的 ``evidence_span``；没有则 ``None``。"""
    if isinstance(entry, dict):
        span = entry.get("evidence_span")
        if isinstance(span, dict) and span.get("quote_text"):
            return span
    return None


def entry_note(entry: Any) -> str | None:
    if isinstance(entry, dict):
        note = entry.get("evidence_note")
        if isinstance(note, str) and note.strip():
            return note.strip()
    return None


def scope_of(status: Any, coverage: Any) -> str:
    """``fulltext``（parse_status='ok' 且 coverage>=0.60）或 ``abstract_only``。"""
    if str(status or "") != "ok":
        return SCOPE_ABSTRACT_ONLY
    try:
        value = float(coverage)
    except (TypeError, ValueError):
        return SCOPE_ABSTRACT_ONLY
    return SCOPE_FULLTEXT if value >= FULLTEXT_GATE_COVERAGE else SCOPE_ABSTRACT_ONLY


def coverage_tag(scope: str, coverage: Any) -> str:
    if scope == SCOPE_FULLTEXT:
        try:
            percent = round(float(coverage) * 100)
        except (TypeError, ValueError):
            return "全文可用（覆盖率未记录）"
        return f"全文可用（覆盖 {percent}%）"
    return "证据覆盖范围：仅摘要"


async def load_cards(session: Any, paper_ids: list[int]) -> list[dict[str, Any]]:
    """读取卡片上下文；顺序与入参 ``paper_ids`` 一致（无卡片的论文被跳过）。"""
    ids = [int(pid) for pid in paper_ids]
    if not ids:
        return []
    card_rows = (
        await session.execute(text(_CARDS_SQL), {"paper_ids": ids})
    ).mappings().all()
    by_id: dict[int, dict[str, Any]] = {}
    for row in card_rows:
        row_dict = dict(row)
        scope = scope_of(row_dict.get("parse_status"), row_dict.get("coverage"))
        by_id[int(row_dict["paper_id"])] = {
            **row_dict,
            "scope": scope,
            "coverage_tag": coverage_tag(scope, row_dict.get("coverage")),
            "spans": [],
            "spans_available": False,
        }

    if by_id:
        span_rows = (
            await session.execute(text(_SPANS_SQL), {"paper_ids": list(by_id)})
        ).mappings().all()
        for row in span_rows:
            row_dict = dict(row)
            entry = by_id.get(int(row_dict["paper_id"]))
            if entry is not None:
                entry["spans"].append(row_dict)
        for entry in by_id.values():
            entry["spans_available"] = bool(entry["spans"])

    ordered: list[dict[str, Any]] = []
    for pid in ids:
        entry = by_id.get(pid)
        if entry is None:
            continue
        # 全文闸门未通过时，span 只能用于「摘要级展示」，不得作为 paper_span 证据
        entry["evidence_scope"] = entry["scope"]
        ordered.append(entry)
    return ordered


async def existing_paper_ids(session: Any, paper_ids: list[int]) -> set[int]:
    """校验论文是否真实存在（禁止对不存在的论文产出行）。"""
    ids = [int(pid) for pid in paper_ids]
    if not ids:
        return set()
    rows = (
        await session.execute(
            text("SELECT id FROM papers WHERE id = ANY(:ids)"), {"ids": ids}
        )
    ).scalars().all()
    return {int(value) for value in rows}


def card_field_candidate(
    paper_id: int,
    card_field: str,
    *,
    paper_span_id: int | None = None,
    quote_text: str | None = None,
    weight: float | None = None,
) -> dict[str, Any]:
    """构造 WP13 的 ``card_field`` 证据候选（字段名必须在其 CANDIDATE_FIELDS 内）。"""
    candidate: dict[str, Any] = {
        "evidence_type": "card_field",
        "paper_id": int(paper_id),
        "card_field": card_field,
    }
    if paper_span_id is not None:
        candidate["paper_span_id"] = int(paper_span_id)
    if quote_text:
        candidate["quote_text"] = quote_text
    if weight is not None:
        candidate["weight"] = float(weight)
    return candidate


__all__ = [
    "CARD_FIELDS",
    "FULLTEXT_GATE_COVERAGE",
    "SCOPE_ABSTRACT_ONLY",
    "SCOPE_FULLTEXT",
    "card_field_candidate",
    "coverage_tag",
    "entry_note",
    "entry_span",
    "entry_text",
    "existing_paper_ids",
    "load_cards",
    "scope_of",
]
