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
"""全库 / 单篇知识 JSON 导出（EasyPaper §5.4 形状，SciLoop 真实数据来源）。

数据来源（**全部为 SciLoop 真实表，禁止编造**）
----------------------------------------------
``metadata``            ← ``papers``（+ ``paper_identities`` 取 arXiv id）
``structure``           ← ``paper_documents`` + ``paper_spans``
``findings``            ← ``draft_claims`` + ``evidences``（``owner_type='draft_claim'``）
``methods/datasets/metrics`` ← ``paper_cards`` 8 字段（该论文**最新版本**）
``flashcards``/``annotations`` ← SciLoop 无对应域，恒为 ``[]`` 并在 ``notes`` 说明
``global_entities``/``global_relationships`` ← 由 ``paper_cards`` 与 ``evidences`` 派生，
                                                每条带 ``derived_from``

分页与上限
----------
- 单次导出最多 ``limit`` 篇（端点限制 ≤1000，默认 200）；论文按 id 升序稳定取数。
- **每篇论文的构造参数在「全库导出」与「单篇导出」中完全一致**
  （``SPAN_LIMIT_PER_PAPER`` / ``QUOTE_TEXT_CHARS``），因此全库 ``papers[i]``
  与单篇导出的节点内容逐字节一致；超出 span 上限时 ``spans_truncated=true``
  并如实返回 ``spans_total``。
- 批量查询按 ``EXPORT_BATCH_SIZE`` 分批（contracts.subagent_sla.batch_size=50），
  不把整库一次性读进内存。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import bindparam
from sqlalchemy import text as sql_text

from services.export.mapping import (
    CLAIM_OWNER_TYPE,
    FULLTEXT_GATE_COVERAGE,
    SCHEMA_VERSION,
    decode_json,
    iso,
    locator_note,
    mapping_notes,
    paper_year,
    real_value,
    string_list,
    to_float,
    to_int,
)

logger = logging.getLogger("sciloop.export.knowledge_json")

#: 批量取数分片大小（contracts.subagent_sla.batch_size）
EXPORT_BATCH_SIZE = 50
#: 单篇论文导出的 span 上限（全库/单篇共用 → 节点内容一致）
SPAN_LIMIT_PER_PAPER = 50
#: 单条 quote_text 的字符上限（quote_sha256 才是权威定位依据，截断如实标记）
QUOTE_TEXT_CHARS = 400
#: 派生实体/关系上限（超出即截断并如实标记）
MAX_ENTITIES = 2000
MAX_RELATIONSHIPS = 4000
#: 单篇 findings / evidences 上限
MAX_FINDINGS = 500
MAX_EVIDENCES_PER_CLAIM = 50

_PAPER_COLUMNS = (
    "id, source, external_id, doi, title, abstract, authors, published_at, venue, "
    "venue_source, venue_level, citation_count, citation_velocity, code_url, pdf_url, "
    "rank_score, influence_score, score_coverage, is_parsed, created_at, updated_at"
)


# --------------------------------------------------------------------------- #
# 数据访问（只读）
# --------------------------------------------------------------------------- #
class ExportRepository:
    """导出专用只读仓储（异步会话；全部为批量查询 + 分片）。"""

    def __init__(self, session: Any) -> None:
        self.session = session

    async def _fetch(
        self, sql: str, params: Mapping[str, Any], *, expanding: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        statement = sql_text(sql)
        if expanding:
            statement = statement.bindparams(
                *(bindparam(name, expanding=True) for name in expanding)
            )
        result = await self.session.execute(statement, dict(params))
        return [dict(row) for row in result.mappings().all()]

    async def count_papers(self) -> int:
        rows = await self._fetch("SELECT count(*) AS total FROM papers", {})
        return int(rows[0]["total"]) if rows else 0

    async def list_papers(
        self, *, limit: int, paper_ids: Sequence[int] | None = None
    ) -> list[dict[str, Any]]:
        if paper_ids is not None and not paper_ids:
            return []
        if paper_ids:
            return await self._fetch(
                f"SELECT {_PAPER_COLUMNS} FROM papers WHERE id IN :ids ORDER BY id LIMIT :limit",
                {"ids": list(paper_ids), "limit": int(limit)},
                expanding=("ids",),
            )
        return await self._fetch(
            f"SELECT {_PAPER_COLUMNS} FROM papers ORDER BY id LIMIT :limit",
            {"limit": int(limit)},
        )

    async def get_paper(self, paper_id: int) -> dict[str, Any] | None:
        rows = await self._fetch(
            f"SELECT {_PAPER_COLUMNS} FROM papers WHERE id = :pid LIMIT 1", {"pid": int(paper_id)}
        )
        return rows[0] if rows else None

    async def identities_by_paper(self, paper_ids: Sequence[int]) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for chunk in _chunks(paper_ids):
            rows = await self._fetch(
                "SELECT paper_id, id_type, id_value, is_primary FROM paper_identities "
                "WHERE paper_id IN :ids ORDER BY is_primary DESC NULLS LAST, id",
                {"ids": list(chunk)},
                expanding=("ids",),
            )
            for row in rows:
                grouped.setdefault(int(row["paper_id"]), []).append(row)
        return grouped

    async def documents_by_paper(
        self, paper_ids: Sequence[int]
    ) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for chunk in _chunks(paper_ids):
            rows = await self._fetch(
                """
                SELECT id, paper_id, document_version, source_type, source_url, parser,
                       parser_version, page_count, text_sha256, char_count, locatable_chars,
                       coverage, parse_status, parse_error, parsed_at
                  FROM paper_documents
                 WHERE paper_id IN :ids
                 ORDER BY paper_id, parsed_at DESC NULLS LAST, id DESC
                """,
                {"ids": list(chunk)},
                expanding=("ids",),
            )
            for row in rows:
                grouped.setdefault(int(row["paper_id"]), []).append(row)
        return grouped

    async def spans_by_paper(
        self, paper_ids: Sequence[int], *, per_paper_limit: int
    ) -> dict[int, dict[str, Any]]:
        """每篇论文取前 ``per_paper_limit`` 条 span（按 char_start 升序）+ 总数。"""
        grouped: dict[int, dict[str, Any]] = {}
        for chunk in _chunks(paper_ids):
            rows = await self._fetch(
                """
                SELECT t.id, t.paper_id, t.document_version, t.section_name, t.page_number,
                       t.bbox, t.char_start, t.char_end, t.quote_text, t.quote_sha256,
                       t.total_count
                  FROM (
                        SELECT s.id, s.paper_id, s.document_version, s.section_name,
                               s.page_number, s.bbox, s.char_start, s.char_end, s.quote_text,
                               s.quote_sha256,
                               row_number() OVER (
                                   PARTITION BY s.paper_id ORDER BY s.char_start, s.id
                               ) AS rn,
                               count(*) OVER (PARTITION BY s.paper_id) AS total_count
                          FROM paper_spans s
                         WHERE s.paper_id IN :ids
                       ) t
                 WHERE t.rn <= :cap
                 ORDER BY t.paper_id, t.rn
                """,
                {"ids": list(chunk), "cap": int(per_paper_limit)},
                expanding=("ids",),
            )
            for row in rows:
                bucket = grouped.setdefault(
                    int(row["paper_id"]), {"items": [], "total": int(row["total_count"] or 0)}
                )
                bucket["items"].append(row)
        return grouped

    async def latest_cards_by_paper(
        self, paper_ids: Sequence[int]
    ) -> dict[int, dict[str, Any]]:
        grouped: dict[int, dict[str, Any]] = {}
        for chunk in _chunks(paper_ids):
            rows = await self._fetch(
                """
                SELECT DISTINCT ON (paper_id)
                       id, paper_id, version, research_problem, core_method, key_innovation,
                       technical_route, experimental_setup, main_conclusions, limitations,
                       transferable, llm_call_log_id, created_at
                  FROM paper_cards
                 WHERE paper_id IN :ids
                 ORDER BY paper_id, version DESC
                """,
                {"ids": list(chunk)},
                expanding=("ids",),
            )
            for row in rows:
                grouped[int(row["paper_id"])] = row
        return grouped

    async def claims_and_evidences(
        self, paper_ids: Sequence[int]
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
        """按 ``evidences.paper_id`` 归属：论文 → 相关 Claim / 该论文下的证据行。"""
        claims: dict[int, list[dict[str, Any]]] = {}
        evidences: dict[int, list[dict[str, Any]]] = {}
        for chunk in _chunks(paper_ids):
            evidence_rows = await self._fetch(
                """
                SELECT e.id, e.owner_id AS claim_id, e.evidence_type, e.paper_id,
                       e.paper_span_id, e.card_field, e.metric_name, e.experiment_run_id,
                       e.experiment_passport_id, e.decision_log_id, e.quote_text, e.weight,
                       s.document_version, s.section_name, s.page_number, s.char_start,
                       s.char_end, s.bbox, s.quote_sha256
                  FROM evidences e
                  LEFT JOIN paper_spans s ON s.id = e.paper_span_id
                 WHERE e.owner_type = :owner_type AND e.paper_id IN :ids
                 ORDER BY e.id
                """,
                {"owner_type": CLAIM_OWNER_TYPE, "ids": list(chunk)},
                expanding=("ids",),
            )
            for row in evidence_rows:
                evidences.setdefault(int(row["paper_id"]), []).append(row)

            claim_ids = sorted({int(row["claim_id"]) for row in evidence_rows})
            if not claim_ids:
                continue
            claim_rows = await self._fetch(
                """
                SELECT id, draft_id, section_heading, claim_text, is_factual, support_status,
                       status_reason, evidence_count
                  FROM draft_claims
                 WHERE id IN :ids
                 ORDER BY id
                """,
                {"ids": claim_ids},
                expanding=("ids",),
            )
            # 每条 Claim 可能因不同证据关联到多篇论文：按论文分别挂载
            papers_of_claim: dict[int, set[int]] = {}
            for row in evidence_rows:
                papers_of_claim.setdefault(int(row["claim_id"]), set()).add(int(row["paper_id"]))
            for row in claim_rows:
                claim = dict(row)
                for paper_id in papers_of_claim.get(int(row["id"]), set()):
                    claims.setdefault(paper_id, []).append(claim)
        return claims, evidences


def _chunks(values: Sequence[int], size: int = EXPORT_BATCH_SIZE) -> list[list[int]]:
    ordered = list(dict.fromkeys(int(value) for value in values))
    return [ordered[index : index + size] for index in range(0, len(ordered), size)]


# --------------------------------------------------------------------------- #
# 单篇论文知识
# --------------------------------------------------------------------------- #
def _arxiv_id(paper: Mapping[str, Any], identities: Sequence[Mapping[str, Any]]) -> str | None:
    for row in identities:
        if str(row.get("id_type")) == "arxiv":
            return real_value(row.get("id_value"))
    if str(paper.get("source")) == "arxiv":
        return real_value(paper.get("external_id"))
    return None


def _metadata(paper: Mapping[str, Any], identities: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    arxiv_id = _arxiv_id(paper, identities)
    doi = real_value(paper.get("doi"))
    authors = paper.get("authors")
    decoded_authors = authors if isinstance(authors, list) else decode_json(authors, [])
    pdf_url = real_value(paper.get("pdf_url"))
    canonical: str | None = None
    if doi:
        canonical = f"https://doi.org/{doi}"
    elif arxiv_id:
        canonical = f"https://arxiv.org/abs/{arxiv_id}"
    return {
        "paper_id": int(paper["id"]),
        "title": real_value(paper.get("title")),
        "abstract": real_value(paper.get("abstract")),
        "authors": (
            decoded_authors if isinstance(decoded_authors, list) else None
        ),
        "year": paper_year(paper.get("published_at")),
        "published_at": iso(paper.get("published_at")),
        "doi": doi,
        "arxiv_id": arxiv_id,
        "venue": real_value(paper.get("venue")),
        "venue_source": real_value(paper.get("venue_source")),
        "venue_level": to_int(paper.get("venue_level")),
        "source": real_value(paper.get("source")),
        "external_id": real_value(paper.get("external_id")),
        "citation_count": to_int(paper.get("citation_count")),
        "citation_velocity": to_float(paper.get("citation_velocity")),
        "url": pdf_url,
        "canonical_url": canonical,
        "canonical_url_note": (
            None if canonical is None else "由 DOI 或 arXiv id 拼装（papers 无 landing_url 列）"
        ),
        "code_url": real_value(paper.get("code_url")),
        "rank_score": to_float(paper.get("rank_score")),
        "influence_score": to_float(paper.get("influence_score")),
        "score_coverage": to_float(paper.get("score_coverage")),
        "is_parsed": None if paper.get("is_parsed") is None else bool(paper.get("is_parsed")),
        "created_at": iso(paper.get("created_at")),
        "updated_at": iso(paper.get("updated_at")),
        "source_table": "papers",
    }


def _primary_document(documents: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """最能支撑证据的一条 ``paper_documents``（全文闸门优先，其次 coverage，再 parsed_at）。"""
    if not documents:
        return None

    def sort_key(row: Mapping[str, Any]) -> tuple[int, float, str, int]:
        coverage = to_float(row.get("coverage")) or 0.0
        allowed = str(row.get("parse_status")) == "ok" and coverage >= FULLTEXT_GATE_COVERAGE
        return (
            1 if allowed else 0,
            coverage,
            iso(row.get("parsed_at")) or "",
            to_int(row.get("id")) or 0,
        )

    return max(documents, key=sort_key)


def _documents_payload(documents: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": to_int(row.get("id")),
            "document_version": real_value(row.get("document_version")),
            "source_type": real_value(row.get("source_type")),
            "source_url": real_value(row.get("source_url")),
            "parser": real_value(row.get("parser")),
            "parser_version": real_value(row.get("parser_version")),
            "page_count": to_int(row.get("page_count")),
            "char_count": to_int(row.get("char_count")),
            "locatable_chars": to_int(row.get("locatable_chars")),
            "coverage": to_float(row.get("coverage")),
            "parse_status": real_value(row.get("parse_status")),
            "parse_error": real_value(row.get("parse_error")),
            "text_sha256": real_value(row.get("text_sha256")),
            "parsed_at": iso(row.get("parsed_at")),
            "source_table": "paper_documents",
        }
        for row in documents
    ]


def _spans_payload(
    bucket: Mapping[str, Any] | None, *, quote_chars: int | None
) -> tuple[list[dict[str, Any]], int]:
    if not bucket:
        return [], 0
    items: list[dict[str, Any]] = []
    for row in bucket.get("items") or []:
        quote = row.get("quote_text")
        truncated = False
        if quote_chars is not None and quote is not None and len(str(quote)) > quote_chars:
            quote = str(quote)[:quote_chars]
            truncated = True
        items.append(
            {
                "span_id": to_int(row.get("id")),
                "document_version": real_value(row.get("document_version")),
                "section_name": real_value(row.get("section_name")),
                "page_number": to_int(row.get("page_number")),
                "char_start": to_int(row.get("char_start")),
                "char_end": to_int(row.get("char_end")),
                "bbox": decode_json(row.get("bbox"), None),
                "quote_text": quote,
                "quote_text_truncated": truncated,
                "quote_sha256": real_value(row.get("quote_sha256")),
                "source_table": "paper_spans",
            }
        )
    return items, int(bucket.get("total") or 0)


def _sections_payload(spans: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for span in spans:
        section = str(span.get("section_name") or "unknown")
        node = grouped.setdefault(
            section,
            {
                "section_name": section,
                "span_count": 0,
                "page_numbers": set(),
                "char_start": None,
                "char_end": None,
            },
        )
        node["span_count"] += 1
        if span.get("page_number") is not None:
            node["page_numbers"].add(int(span["page_number"]))
        start, end = span.get("char_start"), span.get("char_end")
        if start is not None:
            node["char_start"] = start if node["char_start"] is None else min(node["char_start"], start)
        if end is not None:
            node["char_end"] = end if node["char_end"] is None else max(node["char_end"], end)
    return [
        {
            "section_name": node["section_name"],
            "span_count": node["span_count"],
            "page_numbers": sorted(node["page_numbers"]),
            "char_start": node["char_start"],
            "char_end": node["char_end"],
            "derived_from": "paper_spans.section_name 聚合",
        }
        for node in sorted(grouped.values(), key=lambda item: item["section_name"])
    ]


def _structure_payload(
    documents: Sequence[Mapping[str, Any]],
    spans: Sequence[Mapping[str, Any]],
    spans_total: int,
    *,
    quote_chars: int | None,
) -> dict[str, Any]:
    primary = _primary_document(documents)
    parse_status = real_value(primary.get("parse_status")) if primary else None
    coverage = to_float(primary.get("coverage")) if primary else None
    spans_allowed = bool(
        parse_status == "ok" and coverage is not None and coverage >= FULLTEXT_GATE_COVERAGE
    )
    return {
        "primary_document_version": real_value(primary.get("document_version")) if primary else None,
        "parse_status": parse_status,
        "coverage": coverage,
        "spans_allowed": spans_allowed,
        "evidence_scope": "fulltext" if spans_allowed else "abstract_only",
        "fulltext_gate_threshold": FULLTEXT_GATE_COVERAGE,
        "documents": _documents_payload(documents),
        "sections": _sections_payload(spans),
        "spans": list(spans),
        "spans_total": int(spans_total),
        "spans_returned": len(spans),
        "spans_truncated": spans_total > len(spans),
        "quote_text_limit": quote_chars,
        "locator_note": locator_note(),
        "note": (
            "structure 来源为 paper_documents + paper_spans；sections 由 span 的 section_name 聚合派生；"
            "quote_sha256 为权威定位依据，字符偏移仅作辅助"
        ),
    }


def _card_payload(card: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if card is None:
        return None
    setup = decode_json(card.get("experimental_setup"), {})
    meta = setup.get("evidence_meta") if isinstance(setup, Mapping) else None
    return {
        "card_id": to_int(card.get("id")),
        "version": to_int(card.get("version")),
        "research_problem": real_value(card.get("research_problem")),
        "core_method": real_value(card.get("core_method")),
        "key_innovation": decode_json(card.get("key_innovation"), []),
        "technical_route": decode_json(card.get("technical_route"), []),
        "experimental_setup": setup if isinstance(setup, Mapping) else None,
        "main_conclusions": decode_json(card.get("main_conclusions"), []),
        "limitations": decode_json(card.get("limitations"), []),
        "transferable": decode_json(card.get("transferable"), []),
        "llm_call_log_id": to_int(card.get("llm_call_log_id")),
        "available_scope": meta.get("available_scope") if isinstance(meta, Mapping) else None,
        "created_at": iso(card.get("created_at")),
        "source_table": "paper_cards",
    }


def _methods_payload(card: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """``methods`` ← 卡片 ``core_method`` / ``key_innovation`` / ``technical_route``。"""
    if card is None:
        return []
    card_id = to_int(card.get("card_id"))
    methods: list[dict[str, Any]] = []
    core_method = real_value(card.get("core_method"))
    if core_method:
        methods.append(
            {
                "method_id": f"card:{card_id}:core_method",
                "kind": "core_method",
                "text": core_method,
                "derived_from": {
                    "table": "paper_cards",
                    "row_id": card_id,
                    "field": "core_method",
                },
            }
        )
    for index, item in enumerate(card.get("key_innovation") or []):
        if not isinstance(item, Mapping):
            continue
        point = real_value(item.get("point"))
        if point is None:
            continue
        methods.append(
            {
                "method_id": f"card:{card_id}:key_innovation:{index}",
                "kind": "key_innovation",
                "text": point,
                "evidence_quote": real_value(item.get("evidence_quote")),
                "derived_from": {
                    "table": "paper_cards",
                    "row_id": card_id,
                    "field": f"key_innovation[{index}].point",
                },
            }
        )
    for index, item in enumerate(card.get("technical_route") or []):
        if not isinstance(item, Mapping):
            continue
        step = real_value(item.get("step"))
        if step is None:
            continue
        methods.append(
            {
                "method_id": f"card:{card_id}:technical_route:{index}",
                "kind": "technical_route_step",
                "text": step,
                "description": real_value(item.get("description")),
                "derived_from": {
                    "table": "paper_cards",
                    "row_id": card_id,
                    "field": f"technical_route[{index}].step",
                },
            }
        )
    return methods


def _named_items(
    card: Mapping[str, Any] | None, field: str, *, entity_type: str
) -> list[dict[str, Any]]:
    """``datasets`` / ``metrics`` / ``baselines`` ← 卡片 ``experimental_setup.<field>``。"""
    if card is None:
        return []
    setup = card.get("experimental_setup")
    if not isinstance(setup, Mapping):
        return []
    card_id = to_int(card.get("card_id"))
    items: list[dict[str, Any]] = []
    for index, name in enumerate(string_list(setup.get(field))):
        items.append(
            {
                f"{entity_type}_id": f"card:{card_id}:{field}:{index}",
                "name": name,
                "entity_type": entity_type,
                "card_id": card_id,
                "derived_from": {
                    "table": "paper_cards",
                    "row_id": card_id,
                    "field": f"experimental_setup.{field}[{index}]",
                },
                "confidence": None,
                "confidence_note": "paper_cards 无 confidence 列，如实置 null",
            }
        )
    return items


def _evidence_item(row: Mapping[str, Any], *, paper_id: int) -> dict[str, Any]:
    location = {
        "document_version": real_value(row.get("document_version")),
        "section_name": real_value(row.get("section_name")),
        "page_number": to_int(row.get("page_number")),
        "char_start": to_int(row.get("char_start")),
        "char_end": to_int(row.get("char_end")),
        "bbox": decode_json(row.get("bbox"), None),
        "quote_sha256": real_value(row.get("quote_sha256")),
    }
    return {
        "evidence_id": to_int(row.get("id")),
        "claim_id": to_int(row.get("claim_id")),
        "evidence_type": real_value(row.get("evidence_type")),
        "paper_id": int(paper_id),
        "paper_span_id": to_int(row.get("paper_span_id")),
        "card_field": real_value(row.get("card_field")),
        "metric_name": real_value(row.get("metric_name")),
        "experiment_run_id": to_int(row.get("experiment_run_id")),
        "experiment_passport_id": to_int(row.get("experiment_passport_id")),
        "decision_log_id": to_int(row.get("decision_log_id")),
        "quote_text": real_value(row.get("quote_text")),
        "weight": to_float(row.get("weight")),
        "location": location,
        "location_rule": "hash_first",
        "source_table": "evidences",
    }


def _findings_payload(
    claims: Sequence[Mapping[str, Any]],
    evidences: Sequence[Mapping[str, Any]],
    *,
    paper_id: int,
) -> list[dict[str, Any]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in evidences:
        grouped.setdefault(int(row["claim_id"]), []).append(_evidence_item(row, paper_id=paper_id))
    findings: list[dict[str, Any]] = []
    for claim in claims[:MAX_FINDINGS]:
        claim_id = int(claim["id"])
        status = str(claim.get("support_status") or "insufficient")
        findings.append(
            {
                "claim_id": claim_id,
                "draft_id": to_int(claim.get("draft_id")),
                "section_heading": real_value(claim.get("section_heading")),
                "claim_text": real_value(claim.get("claim_text")),
                "is_factual": bool(claim.get("is_factual")),
                "support_status": status,
                "status": status,
                "status_reason": real_value(claim.get("status_reason")),
                "evidence_count": to_int(claim.get("evidence_count")),
                "evidences": grouped.get(claim_id, [])[:MAX_EVIDENCES_PER_CLAIM],
                "source_table": "draft_claims",
            }
        )
    return findings


def build_paper_knowledge(
    paper: Mapping[str, Any],
    *,
    identities: Sequence[Mapping[str, Any]] = (),
    documents: Sequence[Mapping[str, Any]] = (),
    span_bucket: Mapping[str, Any] | None = None,
    card: Mapping[str, Any] | None = None,
    claims: Sequence[Mapping[str, Any]] = (),
    evidences: Sequence[Mapping[str, Any]] = (),
    include_claims: bool = True,
) -> dict[str, Any]:
    """构造单篇论文的完整知识对象（EasyPaper §5.4 形状）。

    ``entities`` / ``relationships`` 为该论文视角的**派生视图**（每条带 ``derived_from``），
    与 ``global_entities`` / ``global_relationships`` 同源同算法（后者跨论文合并）。
    """
    paper_id = int(paper["id"])
    spans, spans_total = _spans_payload(span_bucket, quote_chars=QUOTE_TEXT_CHARS)
    card_node = _card_payload(card)
    notes = [
        "flashcards 恒为 []：SciLoop 无闪卡/间隔重复（SRS）域，不存在可导出的真实数据",
        "annotations 恒为 []：SciLoop 无用户批注表",
        locator_note(),
        "global_entities / global_relationships 是派生视图（非独立表），见顶层 @meta.mapping_notes",
    ]
    if not include_claims:
        notes.append("include_claims=false：findings 未导出（未伪造任何 Claim）")
    if card_node is None:
        notes.append("该论文暂无 paper_cards 记录：methods/datasets/metrics 输出 []，不推断")
    node: dict[str, Any] = {
        "metadata": _metadata(paper, identities),
        "structure": _structure_payload(
            documents, spans, spans_total, quote_chars=QUOTE_TEXT_CHARS
        ),
        "entities": [],
        "relationships": [],
        "findings": _findings_payload(claims, evidences, paper_id=paper_id)
        if include_claims
        else [],
        "methods": _methods_payload(card_node),
        "datasets": _named_items(card_node, "datasets", entity_type="dataset"),
        "metrics": _named_items(card_node, "metrics", entity_type="metric"),
        "baselines": _named_items(card_node, "baselines", entity_type="baseline"),
        "flashcards": [],
        "annotations": [],
        "card": card_node,
        "notes": notes,
    }
    per_entities, _ = derive_entities([node])
    per_relationships, _ = derive_relationships([node], per_entities)
    node["entities"] = per_entities
    node["relationships"] = per_relationships
    return node


async def load_paper_knowledge(
    repository: ExportRepository,
    paper: Mapping[str, Any],
    *,
    include_claims: bool = True,
) -> dict[str, Any]:
    """加载单篇论文的全部依赖并构造知识对象（与全库导出逐篇参数一致）。"""
    paper_id = int(paper["id"])
    identities = await repository.identities_by_paper([paper_id])
    documents = await repository.documents_by_paper([paper_id])
    spans = await repository.spans_by_paper([paper_id], per_paper_limit=SPAN_LIMIT_PER_PAPER)
    cards = await repository.latest_cards_by_paper([paper_id])
    claims, evidences = (
        await repository.claims_and_evidences([paper_id])
        if include_claims
        else ({}, {})
    )
    return build_paper_knowledge(
        paper,
        identities=identities.get(paper_id, []),
        documents=documents.get(paper_id, []),
        span_bucket=spans.get(paper_id),
        card=cards.get(paper_id),
        claims=claims.get(paper_id, []),
        evidences=evidences.get(paper_id, []),
        include_claims=include_claims,
    )


# --------------------------------------------------------------------------- #
# 全库导出
# --------------------------------------------------------------------------- #
async def load_library(
    repository: ExportRepository,
    *,
    limit: int,
    paper_ids: Sequence[int] | None,
    include_claims: bool,
) -> dict[str, Any]:
    """按 ``limit`` 取一批论文并构造全库知识导出载荷。"""
    papers = await repository.list_papers(limit=limit, paper_ids=paper_ids)
    ids = [int(row["id"]) for row in papers]
    identities = await repository.identities_by_paper(ids)
    documents = await repository.documents_by_paper(ids)
    spans = await repository.spans_by_paper(ids, per_paper_limit=SPAN_LIMIT_PER_PAPER)
    cards = await repository.latest_cards_by_paper(ids)
    claims, evidences = (
        await repository.claims_and_evidences(ids) if include_claims else ({}, {})
    )

    payloads: list[dict[str, Any]] = []
    for paper in papers:
        paper_id = int(paper["id"])
        payloads.append(
            build_paper_knowledge(
                paper,
                identities=identities.get(paper_id, []),
                documents=documents.get(paper_id, []),
                span_bucket=spans.get(paper_id),
                card=cards.get(paper_id),
                claims=claims.get(paper_id, []),
                evidences=evidences.get(paper_id, []),
                include_claims=include_claims,
            )
        )
    return {"papers": payloads, "paper_ids": ids}


async def load_metadata_list(
    repository: ExportRepository, *, limit: int, paper_ids: Sequence[int] | None
) -> list[dict[str, Any]]:
    """轻量路径：只读 ``papers`` + ``paper_identities`` 生成书目元数据（BibTeX / CSL-JSON 用）。"""
    papers = await repository.list_papers(limit=limit, paper_ids=paper_ids)
    identities = await repository.identities_by_paper([int(row["id"]) for row in papers])
    return [
        _metadata(paper, identities.get(int(paper["id"]), [])) for paper in papers
    ]


def source_counts(papers: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """导出内容中各类真实来源的条目计数（便于核对，不含任何估算）。"""
    counters = {
        "papers": len(papers),
        "documents": 0,
        "spans": 0,
        "cards": 0,
        "findings": 0,
        "evidences": 0,
    }
    for paper in papers:
        structure = paper.get("structure") or {}
        counters["documents"] += len(structure.get("documents") or [])
        counters["spans"] += int(structure.get("spans_returned") or 0)
        if paper.get("card"):
            counters["cards"] += 1
        counters["findings"] += len(paper.get("findings") or [])
        for finding in paper.get("findings") or []:
            counters["evidences"] += len(finding.get("evidences") or [])
    return counters


# --------------------------------------------------------------------------- #
# 由真实数据派生全局实体与关系（禁止凭空造）
# --------------------------------------------------------------------------- #
def _slugify(text: Any, *, limit: int = 64) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in str(text or ""))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-").lower()[:limit] or "unknown"


def derive_entities(papers: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """从卡片结构的命名列表派生实体；同名同类型跨论文合并。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    source_fields = (("dataset", "datasets"), ("baseline", "baselines"), ("metric", "metrics"))
    for paper in papers:
        paper_id = int((paper.get("metadata") or {}).get("paper_id") or 0)
        for entity_type, field in source_fields:
            for item in paper.get(field) or []:
                name = real_value(item.get("name"))
                if name is None:
                    continue
                key = (entity_type, str(name).lower())
                node = merged.setdefault(
                    key,
                    {
                        "entity_id": f"{entity_type}:{_slugify(name)}",
                        "name": str(name),
                        "entity_type": entity_type,
                        "paper_ids": [],
                        "paper_count": 0,
                        "derived_from": [],
                        "confidence": None,
                        "confidence_source": None,
                        "note": (
                            "派生实体：来自 paper_cards.experimental_setup 的命名列表，"
                            "非独立实体表；paper_cards 无 confidence 列故置 null"
                        ),
                    },
                )
                if paper_id and paper_id not in node["paper_ids"]:
                    node["paper_ids"].append(paper_id)
                    node["paper_count"] = len(node["paper_ids"])
                node["derived_from"].append(item.get("derived_from"))
    entities = sorted(
        merged.values(), key=lambda node: (node["entity_type"], node["name"].lower())
    )
    truncated = len(entities) > MAX_ENTITIES
    return entities[:MAX_ENTITIES], truncated


def derive_relationships(
    papers: Sequence[Mapping[str, Any]], entities: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """派生关系：论文→数据集/基线/指标（来自卡片），Claim→论文（来自证据行）。"""
    entity_index = {
        (str(node["entity_type"]), str(node["name"]).lower()): node["entity_id"]
        for node in entities
    }
    relationships: list[dict[str, Any]] = []
    source_fields = (
        ("dataset", "datasets", "paper_uses_dataset"),
        ("baseline", "baselines", "paper_uses_baseline"),
        ("metric", "metrics", "paper_reports_metric"),
    )
    for paper in papers:
        metadata = paper.get("metadata") or {}
        paper_id = int(metadata.get("paper_id") or 0)
        title = real_value(metadata.get("title"))
        for entity_type, field, relation in source_fields:
            for item in paper.get(field) or []:
                name = real_value(item.get("name"))
                if name is None:
                    continue
                relationships.append(
                    {
                        "from": {"type": "paper", "id": paper_id, "name": title},
                        "to": {
                            "type": entity_type,
                            "id": entity_index.get((entity_type, str(name).lower())),
                            "name": str(name),
                            "derived": True,
                        },
                        "relation": relation,
                        "paper_id": paper_id,
                        "derived_from": [item.get("derived_from")],
                        "confidence": None,
                        "confidence_source": None,
                    }
                )
        for finding in paper.get("findings") or []:
            status = str(finding.get("support_status") or "insufficient")
            relation = (
                "claim_supported_by_paper" if status == "supported" else "claim_cites_paper"
            )
            for evidence in finding.get("evidences") or []:
                relationships.append(
                    {
                        "from": {
                            "type": "claim",
                            "id": finding.get("claim_id"),
                            "name": (real_value(finding.get("claim_text")) or "")[:120],
                        },
                        "to": {"type": "paper", "id": paper_id, "name": title},
                        "relation": relation,
                        "paper_id": paper_id,
                        "derived_from": [
                            {
                                "table": "evidences",
                                "row_id": evidence.get("evidence_id"),
                                "paper_id": paper_id,
                                "field": "paper_id",
                            }
                        ],
                        "confidence": evidence.get("weight"),
                        "confidence_source": "evidences.weight",
                    }
                )
    truncated = len(relationships) > MAX_RELATIONSHIPS
    kept = relationships[:MAX_RELATIONSHIPS]
    for index, node in enumerate(kept, start=1):
        node["relationship_id"] = f"rel-{index:06d}"
    return kept, truncated


def export_meta(
    *,
    limit: int,
    paper_ids: Sequence[int] | None,
    include_claims: bool,
    papers_total: int,
    papers_exported: int,
    entities_truncated: bool,
    relationships_truncated: bool,
    exported_at: str,
) -> dict[str, Any]:
    """导出顶层 ``@meta``（映射声明与口径）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
        "generator": "services.export",
        "filters": {
            "limit": int(limit),
            "paper_ids": list(paper_ids) if paper_ids else None,
            "include_claims": bool(include_claims),
        },
        "papers_total_in_db": int(papers_total),
        "papers_exported": int(papers_exported),
        "truncated": int(papers_exported) < int(papers_total) or bool(paper_ids),
        "entities_truncated": bool(entities_truncated),
        "relationships_truncated": bool(relationships_truncated),
        "limits": {
            "max_papers_per_export": 1000,
            "spans_per_paper": SPAN_LIMIT_PER_PAPER,
            "quote_text_chars": QUOTE_TEXT_CHARS,
            "max_entities": MAX_ENTITIES,
            "max_relationships": MAX_RELATIONSHIPS,
            "batch_size": EXPORT_BATCH_SIZE,
        },
        "mapping_notes": mapping_notes(),
    }


__all__ = [
    "EXPORT_BATCH_SIZE",
    "MAX_ENTITIES",
    "MAX_RELATIONSHIPS",
    "QUOTE_TEXT_CHARS",
    "SPAN_LIMIT_PER_PAPER",
    "ExportRepository",
    "build_paper_knowledge",
    "derive_entities",
    "derive_relationships",
    "export_meta",
    "load_library",
    "load_metadata_list",
    "load_paper_knowledge",
    "source_counts",
]
