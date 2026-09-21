# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排层的持久化与论文库检索。

职责边界
--------
- **状态表**：``research_node_runs`` / ``research_node_transitions``（本层专有）。
- **业务实体**：一律复用既有表，不新建 —— 证据落 ``evidences``、
  假设落 ``ideas``、可行性落 ``feasibilities``、实验协议落 ``taskbooks``。
- **论文库检索**：只读 ``papers`` / ``paper_cards`` / ``paper_spans``。
  本层**不触发新解析**（解析由既有 ``parse_fulltext`` 定时任务负责）；
  命中但未解析的论文如实标为「未解析」，不假装读过。

幂等
----
``upsert_node_run`` 用 ``ON CONFLICT (project_id, node, entry_index) DO UPDATE``：
同一节点同一次进入只有一行，重复提交不会产生第二条 ``running``，也不会重复计成本。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.paper import Paper, PaperCard, PaperSpan
from app.db.models.research import ResearchNodeRun, ResearchNodeTransition
from app.services.research.rules import LibraryFacts

logger = logging.getLogger("sciloop.research.store")

__all__ = [
    "count_revisits",
    "count_total_reverts",
    "delete_node_run",
    "fetch_valid_paper_ids",
    "get_node_run",
    "get_project_settings",
    "list_node_runs",
    "list_transitions",
    "library_facts",
    "library_overview",
    "merge_project_settings",
    "record_transition",
    "row_to_dict",
    "search_library",
    "upsert_node_run",
]


def _row_to_dict(row: Any) -> dict[str, Any]:
    """把 ORM 行摊平成可序列化字典（Decimal → float）。"""

    data: dict[str, Any] = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        if value is not None and hasattr(value, "as_tuple"):  # Decimal
            value = float(value)
        data[column.name] = value
    return data


#: 公开别名（编排层需要把 ORM 行摊平后再序列化）
row_to_dict = _row_to_dict


# --------------------------------------------------------------------------- #
# 节点实例
# --------------------------------------------------------------------------- #
async def upsert_node_run(
    session: AsyncSession,
    *,
    project_id: int,
    node: str,
    entry_index: int,
    **fields: Any,
) -> dict[str, Any]:
    """写入 / 更新一条节点实例（幂等）。返回落库后的行。"""

    values = {"project_id": project_id, "node": node, "entry_index": entry_index, **fields}
    stmt = pg_insert(ResearchNodeRun).values(**values)
    update_cols = {
        key: getattr(stmt.excluded, key)
        for key in values
        if key not in ("project_id", "node", "entry_index")
    }
    update_cols["updated_at"] = func.now()
    stmt = stmt.on_conflict_do_update(
        index_elements=["project_id", "node", "entry_index"], set_=update_cols
    ).returning(ResearchNodeRun)
    result = await session.execute(stmt)
    await session.commit()
    return _row_to_dict(result.scalar_one())


async def get_node_run(
    session: AsyncSession, *, project_id: int, node: str, entry_index: int
) -> dict[str, Any] | None:
    stmt = select(ResearchNodeRun).where(
        ResearchNodeRun.project_id == project_id,
        ResearchNodeRun.node == node,
        ResearchNodeRun.entry_index == entry_index,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return _row_to_dict(row) if row is not None else None


async def delete_node_run(
    session: AsyncSession, *, project_id: int, node: str, entry_index: int
) -> None:
    """删除一条节点实例（回退时清掉本次进入，让下次进入成为新的 entry_index）。"""

    stmt = select(ResearchNodeRun).where(
        ResearchNodeRun.project_id == project_id,
        ResearchNodeRun.node == node,
        ResearchNodeRun.entry_index == entry_index,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()


async def list_node_runs(session: AsyncSession, *, project_id: int) -> list[dict[str, Any]]:
    """该项目全部节点实例（按节点、进入次序）。"""

    stmt = (
        select(ResearchNodeRun)
        .where(ResearchNodeRun.project_id == project_id)
        .order_by(ResearchNodeRun.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 迁移记录
# --------------------------------------------------------------------------- #
async def record_transition(
    session: AsyncSession,
    *,
    project_id: int,
    from_node: str | None,
    to_node: str,
    kind: str,
    trigger: str,
    reason: str,
    required_carried: dict[str, Any] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    """闸门 G3：写迁移留痕。**无条件执行**——这是不是可选项。"""

    row = ResearchNodeTransition(
        project_id=project_id,
        from_node=from_node,
        to_node=to_node,
        kind=kind,
        trigger=trigger,
        reason=reason[:4000],
        required_carried=required_carried,
        budget_snapshot=budget_snapshot,
        actor=actor,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _row_to_dict(row)


async def list_transitions(
    session: AsyncSession, *, project_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = (
        select(ResearchNodeTransition)
        .where(ResearchNodeTransition.project_id == project_id)
        .order_by(ResearchNodeTransition.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def count_total_reverts(session: AsyncSession, *, project_id: int) -> int:
    """整条链已发生的回退次数（闸门 G2）。"""

    stmt = select(func.count()).select_from(ResearchNodeTransition).where(
        ResearchNodeTransition.project_id == project_id,
        ResearchNodeTransition.kind == "revert",
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def count_revisits(session: AsyncSession, *, project_id: int, node: str) -> int:
    """某节点被回退进入的次数（闸门 G2）。"""

    stmt = select(func.count()).select_from(ResearchNodeTransition).where(
        ResearchNodeTransition.project_id == project_id,
        ResearchNodeTransition.kind == "revert",
        ResearchNodeTransition.to_node == node,
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


# --------------------------------------------------------------------------- #
# 论文库（只读）
# --------------------------------------------------------------------------- #
async def fetch_valid_paper_ids(session: AsyncSession, ids: list[int]) -> set[int]:
    """校验引用：返回真正存在于 ``papers`` 的编号集合（规则 R6）。"""

    wanted = sorted({int(i) for i in ids if i is not None})
    if not wanted:
        return set()
    stmt = select(Paper.id).where(Paper.id.in_(wanted))
    return {int(r) for r in (await session.execute(stmt)).scalars().all()}


async def library_facts(session: AsyncSession, ids: list[int]) -> LibraryFacts:
    """把「引用的论文是否真实、其卡片字段与原文片段是否真实存在」一次查清。

    这是规则 R2（可定位来源真实）与 R6（论文存在）的**事实来源**。
    只看模型填了什么字段名是不够的：模型写 ``core_method`` 时那张卡片可能根本不存在，
    证据链就会变成「看着齐全、实际指向空处」。
    """

    from app.services.research.rules import CARD_FIELDS, LibraryFacts

    wanted = sorted({int(i) for i in ids if i is not None})
    facts = LibraryFacts(locators_known=True)
    if not wanted:
        return facts

    facts.paper_ids = await fetch_valid_paper_ids(session, wanted)
    if not facts.paper_ids:
        return facts

    card_stmt = (
        select(PaperCard)
        .where(PaperCard.paper_id.in_(sorted(facts.paper_ids)))
        .order_by(PaperCard.paper_id, PaperCard.version.desc())
    )
    for card in (await session.execute(card_stmt)).scalars().all():
        pid = int(card.paper_id)
        if pid in facts.card_fields:
            continue  # 已取到最高版本
        fields = {
            name for name in CARD_FIELDS if _non_empty(getattr(card, name, None))
        }
        facts.card_fields[pid] = frozenset(fields)

    span_stmt = select(PaperSpan.id, PaperSpan.paper_id).where(
        PaperSpan.paper_id.in_(sorted(facts.paper_ids))
    )
    spans: dict[int, set[int]] = {}
    for span_id, pid in (await session.execute(span_stmt)).all():
        spans.setdefault(int(pid), set()).add(int(span_id))
    facts.span_ids = {pid: frozenset(ids_) for pid, ids_ in spans.items()}

    return facts


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


async def library_overview(session: AsyncSession, *, limit: int = 20) -> dict[str, Any]:
    """论文库概览：给提示词用的「实际可访问的材料清单」。

    只报告真实存在的材料；**不把「库里没有」表述成「该方向不存在」**。
    """

    total = int(
        (await session.execute(select(func.count()).select_from(Paper))).scalar_one() or 0
    )
    parsed = int(
        (
            await session.execute(
                select(func.count()).select_from(Paper).where(Paper.is_parsed.is_(True))
            )
        ).scalar_one()
        or 0
    )
    card_count = int(
        (await session.execute(select(func.count()).select_from(PaperCard))).scalar_one() or 0
    )
    span_count = int(
        (await session.execute(select(func.count()).select_from(PaperSpan))).scalar_one() or 0
    )
    sample_stmt = (
        select(Paper.id, Paper.title, Paper.is_parsed)
        .order_by(Paper.influence_score.desc().nullslast(), Paper.id.desc())
        .limit(max(1, min(limit, 50)))
    )
    sample = [
        {"paper_id": int(pid), "title": title, "parsed": bool(is_parsed)}
        for pid, title, is_parsed in (await session.execute(sample_stmt)).all()
    ]
    return {
        "paper_total": total,
        "paper_parsed": parsed,
        "paper_unparsed": max(0, total - parsed),
        "card_count": card_count,
        "span_count": span_count,
        "sample": sample,
    }


async def search_library(
    session: AsyncSession, *, query: str, limit: int = 12
) -> list[dict[str, Any]]:
    """关键词语义无关检索：``papers.title/abstract`` + 解析卡片字段。

    首版**不引入向量召回**（成本与迁移代价高）：用关键词匹配，
    命中即带 ``card_field`` 与可定位片段；未解析的如实标注。
    """

    terms = [t for t in _tokenize(query) if len(t) >= 2][:8]
    if not terms:
        return []

    patterns = [f"%{t}%" for t in terms]
    conditions = []
    for pattern in patterns:
        conditions.append(Paper.title.ilike(pattern))
        conditions.append(Paper.abstract.ilike(pattern))

    # 解析卡片正文也参与匹配：只搜标题/摘要会漏掉「已解析且方法描述里提到关键词」的论文
    card_conditions = []
    for pattern in patterns:
        card_conditions.append(PaperCard.research_problem.ilike(pattern))
        card_conditions.append(PaperCard.core_method.ilike(pattern))
    card_ids = [
        int(pid)
        for pid in (
            await session.execute(
                select(PaperCard.paper_id)
                .where(or_(*card_conditions))
                .distinct()
                .limit(max(1, limit * 2))
            )
        )
        .scalars()
        .all()
    ]
    if card_ids:
        conditions.append(Paper.id.in_(card_ids))

    # 候选集放大到 200 行再在 Python 里按**相关度**重排。
    # 为什么不靠 SQL 的 ORDER BY 取前 N：全库 389 篇、影响力分普遍为 NULL 时，
    # ORDER BY id DESC 只会捞出「最新的一批」，真正相关但入库较早的论文（低 id）
    # 会被直接截断掉——实测把一篇明显相关的老论文挤出了货架。
    paper_stmt = (
        select(Paper.id, Paper.title, Paper.abstract, Paper.is_parsed, Paper.influence_score)
        .where(or_(*conditions))
        .order_by(Paper.id.desc())
        .limit(200)
    )
    rows = list((await session.execute(paper_stmt)).all())
    if not rows:
        return []

    paper_ids = [int(r[0]) for r in rows]
    card_map: dict[int, dict[str, Any]] = {}
    card_stmt = (
        select(PaperCard)
        .where(PaperCard.paper_id.in_(paper_ids))
        .order_by(PaperCard.paper_id, PaperCard.version.desc())
    )
    for card in (await session.execute(card_stmt)).scalars().all():
        card_map.setdefault(int(card.paper_id), _card_brief(card))

    span_stmt = (
        select(PaperSpan.paper_id, func.count())
        .where(PaperSpan.paper_id.in_(paper_ids))
        .group_by(PaperSpan.paper_id)
    )
    span_count = {int(pid): int(cnt) for pid, cnt in (await session.execute(span_stmt)).all()}

    # 排序：**相关度优先**，卡片与解析状态只作同分时的加成。
    # 相关度 = 标题命中×3 + 摘要命中×1 + 卡片正文命中×2（按命中的检索词计数）。
    # 只有带卡片的论文才能提供 `card_field` 这类可定位来源，但「有卡片」不能压过
    # 「不相关」——否则模型只能对着不相关的材料硬写，证据链看着齐全其实是空的。
    def sort_key(r: Any) -> tuple[int, int, int, float, int]:
        pid = int(r[0])
        card_text = _card_text(card_map.get(pid))
        score = _relevance_score(terms, str(r[1] or ""), str(r[2] or ""), card_text)
        return (
            -score,
            0 if pid in card_map else 1,
            0 if r[3] else 1,
            -(float(r[4]) if r[4] is not None else 0.0),
            -pid,
        )

    rows.sort(key=sort_key)
    rows = rows[: max(1, min(limit, 30))]

    hits: list[dict[str, Any]] = []
    for pid, title, abstract, is_parsed, influence in rows:
        pid = int(pid)
        hits.append(
            {
                "paper_id": pid,
                "title": title,
                "abstract_excerpt": (abstract or "")[:400],
                "is_parsed": bool(is_parsed),
                "span_count": span_count.get(pid, 0),
                "influence_score": float(influence) if influence is not None else None,
                "card": card_map.get(pid),
            }
        )
    return hits


def _card_text(card: dict[str, Any] | None) -> str:
    """把解析卡片压成一段可匹配的小写文本（用于相关度打分）。"""

    if not card:
        return ""
    parts: list[str] = []
    for value in card.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(v) for v in value)
        elif isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
    return " ".join(parts).lower()


def _relevance_score(terms: list[str], title: str, abstract: str, card_text: str) -> int:
    """相关度代理分：标题命中权重最高，其次卡片正文，最后摘要。"""

    title_l = title.lower()
    abstract_l = abstract.lower()
    score = 0
    for term in terms:
        token = term.lower()
        if token in title_l:
            score += 3
        if token in card_text:
            score += 2
        if token in abstract_l:
            score += 1
    return score


def _card_brief(card: PaperCard) -> dict[str, Any]:
    """解析卡片摘要：字段名 + 截断内容（供模型引用时填 ``card_field``）。"""

    def cut(value: Any) -> Any:
        if isinstance(value, str):
            return value[:300]
        if isinstance(value, list):
            return [str(v)[:200] for v in value[:3]]
        if isinstance(value, dict):
            return {k: str(v)[:200] for k, v in list(value.items())[:4]}
        return value

    return {
        "card_id": int(card.id),
        "version": int(card.version),
        "research_problem": cut(card.research_problem),
        "core_method": cut(card.core_method),
        "key_innovation": cut(card.key_innovation),
        "technical_route": cut(card.technical_route),
        "experimental_setup": cut(card.experimental_setup),
        "main_conclusions": cut(card.main_conclusions),
        "limitations": cut(card.limitations),
        "transferable": cut(card.transferable),
    }


def _tokenize(query: str) -> list[str]:
    """把研究问题切成检索词：空白/标点切分 + 保留原串。"""

    import re

    raw = re.split(r"[\s,，。；;：:、/\\|()（）\[\]“”\"'?？!！]+", query or "")
    tokens = [t.strip() for t in raw if t and t.strip()]
    # 中文长句没有空格时，补 2–4 字的滑窗片段，避免整句当作一个词
    extra: list[str] = []
    for token in tokens:
        if len(token) > 6 and not token.isascii():
            extra.extend(token[i : i + 3] for i in range(0, len(token) - 2, 3))
    return tokens + extra


# --------------------------------------------------------------------------- #
# 项目级设置（复用 projects.settings JSONB，不加表）
# --------------------------------------------------------------------------- #
async def get_project_settings(session: AsyncSession, project_id: int) -> dict[str, Any]:
    from app.db.models.project import Project

    stmt = select(Project.settings).where(Project.id == project_id)
    value = (await session.execute(stmt)).scalar_one_or_none()
    return dict(value) if isinstance(value, dict) else {}


async def merge_project_settings(
    session: AsyncSession, project_id: int, patch: dict[str, Any]
) -> dict[str, Any]:
    """局部更新 ``projects.settings``（读改写，字段级合并）。"""

    from app.db.models.project import Project

    current = await get_project_settings(session, project_id)
    current.update(patch)
    stmt = select(Project).where(Project.id == project_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise LookupError(f"项目 {project_id} 不存在")
    row.settings = current
    await session.commit()
    return current
