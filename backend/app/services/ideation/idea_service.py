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
"""idea 读写服务（WP08-T4 的落库与读取层）。

职责分离：

- :mod:`idea_generator` 负责「生成 + 证据强制绑定」
- 本模块负责「手动创建 / 读取 / 列表 / 选中 / 证据读取」

手动 idea（附录 B.2 ``POST /ideas``，``origin='user_input'``）与 AI idea 走**同一张表、
同一套证据契约**；手动 idea 允许先无证据落库，但
:func:`app.services.ideation.evidence_binder.enforce_manual_idea` 会如实暴露
``needs_evidence=true``，UI 必须提示「绑定证据后才能进入可行性流程」
（WP08-A4：手动输入 idea 可进入同一流程）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Idea
from app.services.evidence import EvidenceError, list_evidence
from app.services.ideation.evidence_binder import (
    IDEA_OWNER_TYPE,
    bind_idea_evidence,
    enforce_manual_idea,
)

logger = logging.getLogger("sciloop.wp08.idea_service")

ORIGIN_AI = "ai_generated"
ORIGIN_USER = "user_input"
ORIGINS = (ORIGIN_AI, ORIGIN_USER)

MECHANISMS = ("combination", "transfer", "refinement")


class IdeaError(Exception):
    """idea 读写错误（API 层转 4xx）。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


async def _evidence_count(session: AsyncSession, idea_id: int) -> int:
    rows = await list_evidence(IDEA_OWNER_TYPE, int(idea_id), session=session)
    return len(rows)


async def load_idea(session: AsyncSession, idea_id: int) -> dict[str, Any] | None:
    """读单条 idea（含已绑定证据）。不存在返回 ``None``。"""
    row = (await session.execute(select(Idea).where(Idea.id == int(idea_id)))).scalars().first()
    if row is None:
        return None
    try:
        evidence = await list_evidence(IDEA_OWNER_TYPE, int(idea_id), session=session)
        evidence_errors: list[str] = []
    except EvidenceError as exc:  # pragma: no cover - 坏行容错
        evidence = []
        evidence_errors = [exc.message]
    return {
        "id": int(row.id),
        "idea_id": int(row.id),
        "project_id": row.project_id,
        "aggregation_id": row.aggregation_id,
        "origin": row.origin,
        "title": row.title,
        "content": row.content,
        "mechanism": row.mechanism,
        "novelty_note": row.novelty_note,
        "is_selected": bool(row.is_selected),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "evidences": evidence,
        "evidence_count": len(evidence),
        "evidence_ids": [int(item["evidence_id"]) for item in evidence if item.get("evidence_id")],
        "has_evidence": bool(evidence),
        "evidence_errors": evidence_errors,
        "gate": {
            "rule": "无 Evidence 的 idea 必须丢弃，不允许输出",
            "ok": bool(evidence),
            "evidence_scope": _scope_of(evidence),
        },
    }


def _scope_of(evidence: Sequence[dict[str, Any]]) -> str | None:
    """证据的范围如实披露：全部摘要级则标 ``abstract_only``。"""
    if not evidence:
        return None
    scopes = {
        str((item.get("gate") or {}).get("evidence_scope") or item.get("evidence_scope") or "")
        for item in evidence
    }
    scopes.discard("")
    if not scopes:
        return None
    if scopes == {"fulltext"}:
        return "fulltext"
    if scopes == {"abstract_only"}:
        return "abstract_only"
    return "mixed"


async def list_ideas(
    session: AsyncSession,
    *,
    project_id: int | None = None,
    aggregation_id: int | None = None,
    origin: str | None = None,
    only_with_evidence: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """idea 列表（分页），每条带证据数与证据列表。"""
    if origin is not None and origin not in ORIGINS:
        raise IdeaError(
            "invalid_origin", f"origin='{origin}' 不在受控值域内", {"allowed": list(ORIGINS)}
        )
    stmt = select(Idea).order_by(Idea.is_selected.desc().nullslast(), Idea.id.desc())
    count_stmt = select(func.count()).select_from(Idea)
    if project_id is not None:
        stmt = stmt.where(Idea.project_id == int(project_id))
        count_stmt = count_stmt.where(Idea.project_id == int(project_id))
    if aggregation_id is not None:
        stmt = stmt.where(Idea.aggregation_id == int(aggregation_id))
        count_stmt = count_stmt.where(Idea.aggregation_id == int(aggregation_id))
    if origin is not None:
        stmt = stmt.where(Idea.origin == origin)
        count_stmt = count_stmt.where(Idea.origin == origin)

    total = int((await session.execute(count_stmt)).scalar() or 0)
    start = (max(1, int(page)) - 1) * max(1, int(page_size))
    rows = (await session.execute(stmt.offset(start).limit(max(1, int(page_size))))).scalars().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = await load_idea(session, int(row.id))
        if item is None:  # pragma: no cover - 同一事务内不可能
            continue
        if only_with_evidence and not item["has_evidence"]:
            continue
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": max(1, int(page)),
        "page_size": max(1, int(page_size)),
        "returned": len(items),
        "filter": {
            "project_id": project_id,
            "aggregation_id": aggregation_id,
            "origin": origin,
            "only_with_evidence": only_with_evidence,
        },
        "evidence_policy": "无 Evidence 的 idea 不在 AI 生成结果中出现；手动 idea 以 needs_evidence 如实标注",
    }


async def create_manual_idea(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    project_id: int | None = None,
    aggregation_id: int | None = None,
    mechanism: str | None = None,
    novelty_note: str | None = None,
    evidences: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """手动创建 idea（``origin='user_input'``），可选同时绑定证据。"""
    if not str(title or "").strip():
        raise IdeaError("missing_title", "title 不能为空")
    if not str(content or "").strip():
        raise IdeaError("missing_content", "content 不能为空")
    if mechanism is not None and mechanism not in MECHANISMS:
        raise IdeaError(
            "invalid_mechanism",
            f"mechanism='{mechanism}' 不在受控值域内",
            {"allowed": list(MECHANISMS)},
        )

    row = Idea(
        project_id=project_id,
        aggregation_id=aggregation_id,
        origin=ORIGIN_USER,
        title=str(title).strip(),
        content=str(content),
        mechanism=mechanism,
        novelty_note=novelty_note,
    )
    session.add(row)
    await session.flush()
    idea_id = int(row.id)

    binding: dict[str, Any] | None = None
    if evidences:
        binding = await bind_idea_evidence(session, idea_id, evidences, replace=True)
    await session.commit()

    item = await load_idea(session, idea_id)
    status = await enforce_manual_idea(session, idea_id)
    logger.info(
        "create_manual_idea id=%s evidences=%s bound=%s",
        idea_id,
        len(evidences or []),
        (binding or {}).get("bound_ids"),
    )
    return {
        "item": item,
        "binding": binding,
        "evidence_status": status,
        "next_step": (
            "已带证据，可直接 POST /feasibility"
            if status["has_evidence"]
            else "尚无证据：POST /ideas/{id}/evidences 绑定至少 1 条证据后再进入可行性"
        ),
    }


async def bind_idea_evidences(
    session: AsyncSession,
    idea_id: int,
    candidates: Sequence[dict[str, Any]],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """给任意 idea（手动或 AI）绑定证据；idea 不存在报 404 语义错误。"""
    row = (await session.execute(select(Idea).where(Idea.id == int(idea_id)))).scalars().first()
    if row is None:
        raise IdeaError("idea_not_found", f"idea {idea_id} 不存在", {"idea_id": int(idea_id)})
    detail = await bind_idea_evidence(session, int(idea_id), candidates, replace=replace)
    await session.commit()
    item = await load_idea(session, int(idea_id))
    return {"binding": detail, "item": item}


async def select_idea(session: AsyncSession, idea_id: int) -> dict[str, Any]:
    """选中 idea（同一项目内互斥：先清空再置位）。"""
    row = (await session.execute(select(Idea).where(Idea.id == int(idea_id)))).scalars().first()
    if row is None:
        raise IdeaError("idea_not_found", f"idea {idea_id} 不存在", {"idea_id": int(idea_id)})

    cleared = 0
    if row.project_id is not None:
        siblings = (
            await session.execute(
                select(Idea).where(Idea.project_id == int(row.project_id), Idea.id != int(idea_id))
            )
        ).scalars().all()
        for sibling in siblings:
            if sibling.is_selected:
                sibling.is_selected = False
                cleared += 1
    row.is_selected = True
    await session.commit()

    item = await load_idea(session, int(idea_id))
    logger.info("select_idea id=%s project=%s cleared=%d", idea_id, row.project_id, cleared)
    return {"item": item, "cleared_siblings": cleared}


async def idea_evidences(session: AsyncSession, idea_id: int) -> dict[str, Any]:
    """idea 的证据列表（附录 B.2 ``GET /ideas/{id}/evidences``）。"""
    row = (await session.execute(select(Idea).where(Idea.id == int(idea_id)))).scalars().first()
    if row is None:
        raise IdeaError("idea_not_found", f"idea {idea_id} 不存在", {"idea_id": int(idea_id)})
    evidence = await list_evidence(IDEA_OWNER_TYPE, int(idea_id), session=session)
    return {
        "idea_id": int(idea_id),
        "idea_title": row.title,
        "items": evidence,
        "total": len(evidence),
        "evidence_count": len(evidence),
        "has_evidence": bool(evidence),
        "gate": {
            "rule": "无 Evidence 的 idea 必须丢弃，不允许输出",
            "ok": bool(evidence),
            "evidence_scope": _scope_of(evidence),
        },
    }


__all__ = [
    "MECHANISMS",
    "ORIGINS",
    "ORIGIN_AI",
    "ORIGIN_USER",
    "IdeaError",
    "bind_idea_evidences",
    "create_manual_idea",
    "idea_evidences",
    "list_ideas",
    "load_idea",
    "select_idea",
]
