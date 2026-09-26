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

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.paper import Paper, PaperCard, PaperSpan
from db.models.research import ResearchNodeRun, ResearchNodeTransition
from services.research.rules import LibraryFacts

logger = logging.getLogger("sciloop.research.store")

#: 候选集上限：**只作防御性护栏**，不用来「选优」。
#: 本库仅 389 篇，命中通常几十到几百行；真正的排序在 Python 里按相关度做。
CANDIDATE_CAP = 2000

#: 每篇命中论文附带的代表性原文片段数（供模型填 `paper_span_id`）
SPAN_SAMPLES_PER_PAPER = 4

__all__ = [
    "count_revisits",
    "count_total_reverts",
    "delete_node_run",
    "fetch_valid_paper_ids",
    "get_node_run",
    "get_project_settings",
    "list_node_runs",
    "list_orphan_node_runs",
    "list_transitions",
    "library_facts",
    "library_overview",
    "merge_project_settings",
    "reap_stale_running_runs",
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
# 节点实例（链的身份是**对话**，project_id 只是可空元信息）
# --------------------------------------------------------------------------- #
async def upsert_node_run(
    session: AsyncSession,
    *,
    conversation_id: str,
    node: str,
    entry_index: int,
    project_id: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """写入 / 更新一条节点实例（幂等）。返回落库后的行。

    冲突目标是 ``(conversation_id, node, entry_index)`` 上的**部分唯一索引**
    （``WHERE conversation_id IS NOT NULL``），所以这里必须带上 ``index_where``，
    否则 PostgreSQL 匹配不到索引、直接报错。
    """

    values = {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "node": node,
        "entry_index": entry_index,
        **fields,
    }
    stmt = pg_insert(ResearchNodeRun).values(**values)
    update_cols = {
        key: getattr(stmt.excluded, key)
        for key in values
        if key not in ("conversation_id", "node", "entry_index")
    }
    update_cols["updated_at"] = func.now()
    stmt = stmt.on_conflict_do_update(
        index_elements=["conversation_id", "node", "entry_index"],
        index_where=text("conversation_id IS NOT NULL"),
        set_=update_cols,
    ).returning(ResearchNodeRun)
    result = await session.execute(stmt)
    await session.commit()
    return _row_to_dict(result.scalar_one())


async def get_node_run(
    session: AsyncSession, *, conversation_id: str, node: str, entry_index: int
) -> dict[str, Any] | None:
    stmt = select(ResearchNodeRun).where(
        ResearchNodeRun.conversation_id == conversation_id,
        ResearchNodeRun.node == node,
        ResearchNodeRun.entry_index == entry_index,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return _row_to_dict(row) if row is not None else None


async def delete_node_run(
    session: AsyncSession, *, conversation_id: str, node: str, entry_index: int
) -> None:
    """删除一条节点实例（回退时清掉本次进入，让下次进入成为新的 entry_index）。"""

    stmt = select(ResearchNodeRun).where(
        ResearchNodeRun.conversation_id == conversation_id,
        ResearchNodeRun.node == node,
        ResearchNodeRun.entry_index == entry_index,
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.commit()


async def list_node_runs(
    session: AsyncSession, *, conversation_id: str
) -> list[dict[str, Any]]:
    """该对话全部节点实例（按进入次序）。"""

    stmt = (
        select(ResearchNodeRun)
        .where(ResearchNodeRun.conversation_id == conversation_id)
        .order_by(ResearchNodeRun.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def list_orphan_node_runs(
    session: AsyncSession, *, limit: int = 30
) -> list[dict[str, Any]]:
    """0009 之前的历史链（``conversation_id IS NULL``）——界面标「无对话归属（早期记录）」。"""

    stmt = (
        select(ResearchNodeRun)
        .where(ResearchNodeRun.conversation_id.is_(None))
        .order_by(ResearchNodeRun.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def reap_stale_running_runs(session: AsyncSession) -> int:
    """把**残留的「运行中」节点**如实收尾，返回处理条数（2026-09-26）。

    为什么需要：进程重启或对话中断之后，那些"当时正在跑"的节点会永远停在 ``running``
    —— 界面上就是一条**假的「运行中」**（实测库里积了 4 条，且 ``updated_at`` 等于
    ``created_at``，说明它们启动后就再没动过）。跑它们的进程早就没了。

    口径：
    - 状态改成 ``failed``（内部六态里没有"中断"这一态，"没跑完就停了"最贴近它）；
    - **原因写进 ``payload``**（``reaped_reason``），**不删除记录** —— 记录本身是历史，
      删了就再也查不出"这一步曾经跑过、被中断了"；
    - 只在**服务启动时**调用：此刻任何 ``running`` 必然是上一代进程留下的。
    """

    result = await session.execute(
        text(
            """
            UPDATE research_node_runs
               SET status = 'failed',
                   payload = COALESCE(payload, '{}'::jsonb)
                             || '{"reaped_reason": "服务重启：上一代进程没跑完，状态已如实收尾"}'::jsonb,
                   updated_at = now()
             WHERE status = 'running'
            """
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# 迁移记录
# --------------------------------------------------------------------------- #
async def record_transition(
    session: AsyncSession,
    *,
    conversation_id: str,
    from_node: str | None,
    to_node: str,
    kind: str,
    trigger: str,
    reason: str,
    project_id: int | None = None,
    required_carried: dict[str, Any] | None = None,
    budget_snapshot: dict[str, Any] | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    """闸门 G3：写迁移留痕。**无条件执行**——这不是可选项。"""

    row = ResearchNodeTransition(
        conversation_id=conversation_id,
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
    session: AsyncSession, *, conversation_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = (
        select(ResearchNodeTransition)
        .where(ResearchNodeTransition.conversation_id == conversation_id)
        .order_by(ResearchNodeTransition.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def count_total_reverts(session: AsyncSession, *, conversation_id: str) -> int:
    """整条链已发生的回退次数（闸门 G2）。"""

    stmt = select(func.count()).select_from(ResearchNodeTransition).where(
        ResearchNodeTransition.conversation_id == conversation_id,
        ResearchNodeTransition.kind == "revert",
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def count_revisits(session: AsyncSession, *, conversation_id: str, node: str) -> int:
    """某节点被回退进入的次数（闸门 G2）。"""

    stmt = select(func.count()).select_from(ResearchNodeTransition).where(
        ResearchNodeTransition.conversation_id == conversation_id,
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

    from services.research.rules import CARD_FIELDS, LibraryFacts

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
    命中即带可定位来源（解析卡片字段 + 可引用的原文片段）；未解析的如实标注。

    候选集的两条硬规则（都是实机踩出来的）
    ------------------------------------
    1. **绝不按 id 倒序截断候选集**：解析卡片恰好是给**最早入库**的论文做的，
       而那批论文 id 最低。实测 276 篇命中里，唯一带卡片的 5 篇（id 123–128）
       全部落在 ``ORDER BY id DESC LIMIT 200`` 的截断线之外（该批最小 id 227），
       于是节点永远拿不到可核验来源、R2 必然驳回。
    2. **带卡片的命中一律保底保留**：即使候选上限将来被触及也不能丢掉它们。
    """

    terms = [t for t in _tokenize(query) if len(t) >= 2][:8]
    if not terms:
        return []

    patterns = [f"%{t}%" for t in terms]
    conditions = []
    for pattern in patterns:
        conditions.append(Paper.title.ilike(pattern))
        conditions.append(Paper.abstract.ilike(pattern))

    # 解析卡片正文也参与匹配：只搜标题/摘要会漏掉「已解析且方法描述里提到关键词」的论文。
    # 这里**不设 limit**：这批论文是唯一能提供 card_field 这种可定位来源的，
    # 截断它们等于把可核验的材料丢掉（旧写法 `limit(limit*2)` 同样有这个毛病）。
    card_conditions = []
    for pattern in patterns:
        card_conditions.append(PaperCard.research_problem.ilike(pattern))
        card_conditions.append(PaperCard.core_method.ilike(pattern))
    card_ids = [
        int(pid)
        for pid in (
            await session.execute(
                select(PaperCard.paper_id).where(or_(*card_conditions)).distinct()
            )
        )
        .scalars()
        .all()
    ]
    if card_ids:
        conditions.append(Paper.id.in_(card_ids))

    paper_stmt = (
        select(Paper.id, Paper.title, Paper.abstract, Paper.is_parsed, Paper.influence_score)
        .where(or_(*conditions))
        .limit(CANDIDATE_CAP)
    )
    rows = list((await session.execute(paper_stmt)).all())
    if not rows:
        return []

    # 保底：把命中的卡片论文补回来（正常路径下它们已在 rows 里，这里只防上限被触及）
    present = {int(r[0]) for r in rows}
    missing_card = [pid for pid in card_ids if pid not in present]
    if missing_card:
        extra_stmt = select(
            Paper.id, Paper.title, Paper.abstract, Paper.is_parsed, Paper.influence_score
        ).where(Paper.id.in_(missing_card))
        rows.extend((await session.execute(extra_stmt)).all())

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

    # 给最终入选的论文附上**可引用的原文片段**。
    # 契约允许用 `paper_span_id` 当可定位来源，但模型必须先知道合法 id 才能引用它 ——
    # 不提供就等于这条路走不通：实测命中的 12 篇里 0 篇带卡片、8 篇带片段，
    # 于是 8 篇本可核验的论文因为「拿不到 id」而全部不可引用，R2 数学上无法满足。
    top_ids = [int(r[0]) for r in rows]
    spans_map = await _span_samples(session, top_ids)

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
                "spans": spans_map.get(pid, []),
            }
        )
    return hits


async def _span_samples(
    session: AsyncSession, paper_ids: list[int], *, per_paper: int = SPAN_SAMPLES_PER_PAPER
) -> dict[int, list[dict[str, Any]]]:
    """每篇论文取前 ``per_paper`` 个原文片段（id + 章节 + 页码 + 引文节选）。

    取「前几个」而不是随机取样：同一 query 下结果稳定，便于复现与核对。
    """

    if not paper_ids:
        return {}
    stmt = (
        select(
            PaperSpan.id,
            PaperSpan.paper_id,
            PaperSpan.section_name,
            PaperSpan.page_number,
            func.left(PaperSpan.quote_text, 180),
        )
        .where(PaperSpan.paper_id.in_(paper_ids))
        .order_by(PaperSpan.paper_id, PaperSpan.id)
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for span_id, pid, section, page, quote in (await session.execute(stmt)).all():
        bucket = out.setdefault(int(pid), [])
        if len(bucket) < per_paper:
            bucket.append(
                {
                    "paper_span_id": int(span_id),
                    "section_name": section,
                    "page_number": page,
                    "quote_text": quote,
                }
            )
    return out


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
    from db.models.project import Project

    stmt = select(Project.settings).where(Project.id == project_id)
    value = (await session.execute(stmt)).scalar_one_or_none()
    return dict(value) if isinstance(value, dict) else {}


async def merge_project_settings(
    session: AsyncSession, project_id: int, patch: dict[str, Any]
) -> dict[str, Any]:
    """局部更新 ``projects.settings``（读改写，字段级合并）。"""

    from db.models.project import Project

    current = await get_project_settings(session, project_id)
    current.update(patch)
    stmt = select(Project).where(Project.id == project_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise LookupError(f"项目 {project_id} 不存在")
    row.settings = current
    await session.commit()
    return current
