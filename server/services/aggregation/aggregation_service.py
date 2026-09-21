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
"""聚合编排与持久化（WP08-T1 / T2 / T3 的落库层）。

一次 ``create_aggregation`` 完成三件事并**同一事务落库**：

1. ``comparison_matrix`` —— :mod:`services.aggregation.matrix`（真实卡片字段）
2. ``method_evolution``  —— :mod:`services.aggregation.evolution`（真实卡片 + span 重叠）
3. ``gaps``              —— :mod:`services.aggregation.gap_finder`（真实 span / card_field）

写入前做两道校验，避免脏数据：

- ``paper_ids`` 数量必须落在 ``[MIN_PAPERS, MAX_PAPERS]``；
- 论文必须真实存在（``papers`` 表），不存在的 id 直接 422，不给「空矩阵」蒙混过关。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Aggregation, Gap
from services.aggregation.cards import existing_paper_ids, load_cards
from services.aggregation.evolution import build_evolution_payload
from services.aggregation.gap_finder import build_gaps_payload
from services.aggregation.matrix import MAX_PAPERS, MIN_PAPERS, build_matrix_payload

logger = logging.getLogger("sciloop.wp08.aggregation_service")


class AggregationError(Exception):
    """聚合入参或落库错误（API 层转 4xx）。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _normalize_ids(paper_ids: list[int] | tuple[int, ...]) -> list[int]:
    seen: list[int] = []
    for raw in paper_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise AggregationError(
                "invalid_paper_id", f"paper_ids 含非整数项：{raw!r}", {"paper_ids": list(paper_ids)}
            ) from exc
        if value not in seen:
            seen.append(value)
    if not MIN_PAPERS <= len(seen) <= MAX_PAPERS:
        raise AggregationError(
            "invalid_paper_count",
            f"paper_ids 数量必须在 {MIN_PAPERS}–{MAX_PAPERS} 之间，实际 {len(seen)}",
            {"min": MIN_PAPERS, "max": MAX_PAPERS, "count": len(seen)},
        )
    return seen


async def build_aggregation_payload(
    session: AsyncSession, paper_ids: list[int]
) -> dict[str, Any]:
    """只读地构造三份产物（不落库，供预览与单测复用）。"""
    ids = _normalize_ids(paper_ids)
    known = await existing_paper_ids(session, ids)
    unknown = [pid for pid in ids if pid not in known]
    if unknown:
        raise AggregationError(
            "unknown_paper_id",
            f"papers 表中不存在这些论文：{unknown}（禁止对不存在的论文产出行）",
            {"unknown_paper_ids": unknown},
        )
    cards = await load_cards(session, ids)
    without_card = [int(card["paper_id"]) for card in cards if not card.get("version")]
    matrix = build_matrix_payload(cards)
    evolution = build_evolution_payload(cards)
    gaps = build_gaps_payload(cards)
    matrix["missing"] = [
        {"paper_id": pid, "reason": "no_parsed_card"}
        for pid in ids
        if pid not in {int(card["paper_id"]) for card in cards}
    ]
    return {
        "paper_ids": ids,
        "paper_count": len(ids),
        "comparison_matrix": matrix,
        "method_evolution": evolution,
        "gaps_payload": gaps,
        "cards_used": [int(card["paper_id"]) for card in cards],
        "papers_without_card": without_card,
    }


async def create_aggregation(
    session: AsyncSession,
    paper_ids: list[int],
    *,
    project_id: int | None = None,
) -> dict[str, Any]:
    """构造并落库 ``aggregations`` + ``gaps``；返回含 id 的完整产物。"""
    payload = await build_aggregation_payload(session, paper_ids)
    aggregation = Aggregation(
        project_id=project_id,
        paper_ids=payload["paper_ids"],
        comparison_matrix=payload["comparison_matrix"],
        method_evolution=payload["method_evolution"]["relations"],
    )
    session.add(aggregation)
    await session.flush()

    gap_rows: list[Gap] = []
    for gap in payload["gaps_payload"]["gaps"]:
        row = Gap(
            aggregation_id=int(aggregation.id),
            gap_text=gap["gap_text"],
            raised_by_paper_ids=gap["raised_by_paper_ids"],
            unsolved_evidence=gap["unsolved_evidence"],
            novelty_hint=gap.get("novelty_hint"),
        )
        session.add(row)
        gap_rows.append(row)
    await session.flush()

    gaps = []
    for row, meta in zip(gap_rows, payload["gaps_payload"]["gaps"], strict=False):
        gaps.append(
            {
                **meta,
                "id": int(row.id),
                "aggregation_id": int(aggregation.id),
            }
        )
    await session.commit()

    logger.info(
        "create_aggregation id=%s papers=%s gaps=%s evolution=%s",
        aggregation.id,
        len(payload["paper_ids"]),
        len(gaps),
        len(payload["method_evolution"]["relations"]),
    )
    return {
        "id": int(aggregation.id),
        "aggregation_id": int(aggregation.id),
        "project_id": project_id,
        "paper_ids": payload["paper_ids"],
        "paper_count": payload["paper_count"],
        "comparison_matrix": payload["comparison_matrix"],
        "method_evolution": payload["method_evolution"],
        "gaps": gaps,
        "gap_count": len(gaps),
        "gaps_payload_meta": {
            key: value
            for key, value in payload["gaps_payload"].items()
            if key != "gaps"
        },
        "cards_used": payload["cards_used"],
        "papers_without_card": payload["papers_without_card"],
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "evidence_policy": "无真实 span / card_field 依据的空白一律丢弃，不编造未解决问题",
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def load_gaps(session: AsyncSession, aggregation_id: int) -> list[dict[str, Any]]:
    """读回落库的空白清单，并补齐「提出者论文 + 原文片段」可点开结构。"""
    rows = (
        await session.execute(
            select(Gap).where(Gap.aggregation_id == int(aggregation_id)).order_by(Gap.id)
        )
    ).scalars().all()
    if not rows:
        return []
    paper_ids = sorted({int(pid) for row in rows for pid in (row.raised_by_paper_ids or [])})
    cards = await load_cards(session, paper_ids) if paper_ids else []
    card_by_id = {int(card["paper_id"]): card for card in cards}

    gaps: list[dict[str, Any]] = []
    for row in rows:
        unsolved = list(row.unsolved_evidence or [])
        raised_by = [
            {
                "paper_id": int(pid),
                "title": (card_by_id.get(int(pid)) or {}).get("title"),
                "venue": (card_by_id.get(int(pid)) or {}).get("venue"),
                "coverage_tag": (card_by_id.get(int(pid)) or {}).get("coverage_tag"),
                "span_ids": [
                    int(item.get("paper_span_id"))
                    for item in unsolved
                    if int(item.get("paper_id", -1)) == int(pid) and item.get("paper_span_id")
                ],
                "jump_url": f"/papers/{int(pid)}",
            }
            for pid in (row.raised_by_paper_ids or [])
        ]
        gaps.append(
            {
                "id": int(row.id),
                "aggregation_id": int(row.aggregation_id),
                "gap_text": row.gap_text,
                "raised_by_paper_ids": list(row.raised_by_paper_ids or []),
                "unsolved_evidence": unsolved,
                "novelty_hint": row.novelty_hint,
                "raised_by": raised_by,
                "span_count": len(unsolved),
                "evidence_kinds": ["paper_span"] if unsolved else ["card_field"],
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return gaps


async def get_aggregation(session: AsyncSession, aggregation_id: int) -> dict[str, Any] | None:
    """读回聚合产物（矩阵 + 演进 + 空白清单）。行不存在返回 ``None``。"""
    row = (
        await session.execute(
            select(Aggregation).where(Aggregation.id == int(aggregation_id))
        )
    ).scalars().first()
    if row is None:
        return None

    matrix = row.comparison_matrix or {}
    gaps = await load_gaps(session, int(aggregation_id))
    paper_ids = [int(pid) for pid in (row.paper_ids or [])]
    return {
        "id": int(row.id),
        "aggregation_id": int(row.id),
        "project_id": row.project_id,
        "paper_ids": paper_ids,
        "paper_count": len(paper_ids),
        "comparison_matrix": matrix,
        "method_evolution": {
            "relations": list(row.method_evolution or []),
            "relation_count": len(row.method_evolution or []),
            "generated_by": "deterministic:card_and_span_overlap",
        },
        "gaps": gaps,
        "gap_count": len(gaps),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "dimension_count": len(matrix.get("dimensions") or []),
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def list_aggregations(
    session: AsyncSession, *, project_id: int | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """聚合列表（按 id 倒序），供 UI 选择器使用。"""
    stmt = select(Aggregation).order_by(Aggregation.id.desc()).limit(max(1, int(limit)))
    if project_id is not None:
        stmt = (
            select(Aggregation)
            .where(Aggregation.project_id == int(project_id))
            .order_by(Aggregation.id.desc())
            .limit(max(1, int(limit)))
        )
    rows = (await session.execute(stmt)).scalars().all()
    items: list[dict[str, Any]] = []
    for row in rows:
        paper_ids = [int(pid) for pid in (row.paper_ids or [])]
        matrix = row.comparison_matrix or {}
        gap_count = (
            await session.execute(
                select(Gap.id).where(Gap.aggregation_id == int(row.id))
            )
        ).scalars().all()
        items.append(
            {
                "id": int(row.id),
                "aggregation_id": int(row.id),
                "project_id": row.project_id,
                "paper_ids": paper_ids,
                "paper_count": len(paper_ids),
                "row_count": matrix.get("row_count"),
                "gap_count": len(gap_count),
                "evolution_count": len(row.method_evolution or []),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return items


async def delete_aggregation(session: AsyncSession, aggregation_id: int) -> bool:
    """删除聚合（``gaps`` 靠 ON DELETE CASCADE 一并清理）。"""
    result = await session.execute(
        delete(Aggregation).where(Aggregation.id == int(aggregation_id))
    )
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


__all__ = [
    "AggregationError",
    "build_aggregation_payload",
    "create_aggregation",
    "delete_aggregation",
    "get_aggregation",
    "list_aggregations",
    "load_gaps",
]
