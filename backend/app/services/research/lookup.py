# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话里的本地数据查询：**确定性、只读、参数白名单**。

设计口径
--------
- **模型不参与取数**：分类与参数提取在 ``intent`` 层完成，这里只按固定语句查库。
  模型只负责把查到的结果**讲成人话**——所以不存在「模型自己拼 SQL / 乱传参数」的风险。
- **只读**：本模块不开任何写路径。
- **不写研究链**：查询只落对话轮次；研究链只记节点执行，职责清楚。
- **平台是开源的、可以装在自己机器上**：所以允许在任意对话里查任何本地数据
  （论文、项目、产出、研究链），不按对话/项目做隔离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.aggregation import Evidence, Idea
from app.db.models.feasibility import Feasibility, Taskbook
from app.db.models.paper import Paper, PaperCard
from app.db.models.project import Project
from app.db.models.review import PaperDraft
from app.services.research import orchestrator, store

__all__ = ["LookupResult", "TOPICS", "run_lookup"]

#: 支持的查询主题（与 intent.QUERY_TOPICS 的键一致）
TOPICS: tuple[str, ...] = ("papers", "projects", "artifacts", "chain")

_PAGE_LIMIT = 20


@dataclass
class LookupResult:
    """一次查询的结果卡片。

    ``columns`` / ``rows`` 直接给前端渲染表格；``summary`` 是给模型与用户的一句话。
    """

    topic: str
    title: str
    summary: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    total: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "title": self.title,
            "summary": self.summary,
            "columns": self.columns,
            "rows": self.rows,
            "total": self.total,
            "params": self.params,
            "extra": self.extra,
        }

    def to_prompt_context(self) -> str:
        """给模型的上下文：**只给事实**，让它据此措辞，不要它再编。"""

        head = f"查询主题：{self.title}\n命中 {self.total} 条\n一句话概括：{self.summary}"
        body = "\n".join(" | ".join(str(c) for c in row) for row in self.rows[:20])
        return f"{head}\n详细结果（最多 20 行）：\n{body}" if body else head


def _extract_terms(text: str) -> str:
    """从用户那句话里剥掉查询动词与主题词，剩下的当检索词。"""

    from app.services.research import intent as intent_mod

    stripped = text
    for word in (*intent_mod.QUERY_ACTIONS, *intent_mod.GUIDE_WORDS):
        stripped = stripped.replace(word, " ")
    for words in intent_mod.QUERY_TOPICS.values():
        for word in words:
            stripped = stripped.replace(word, " ")
    import re

    stripped = re.sub(r"[\s，。！？、,.!?：:~的了下吧呢吗]+", " ", stripped)
    return stripped.strip()[:120]


# --------------------------------------------------------------------------- #
# ① 论文
# --------------------------------------------------------------------------- #
async def _lookup_papers(
    session: AsyncSession, *, text: str, conversation_id: str
) -> LookupResult:
    query = _extract_terms(text) or text
    hits = await store.search_library(session, query=query, limit=_PAGE_LIMIT)
    rows = [
        [
            hit["paper_id"],
            (hit.get("title") or "")[:70],
            "有卡片" if hit.get("card") else ("有片段" if hit.get("span_count") else "仅摘要"),
            hit.get("span_count", 0),
        ]
        for hit in hits
    ]
    total_papers = int(
        (await session.execute(select(func.count()).select_from(Paper))).scalar_one() or 0
    )
    cards = int(
        (await session.execute(select(func.count()).select_from(PaperCard))).scalar_one() or 0
    )
    with_card = sum(1 for hit in hits if hit.get("card"))
    return LookupResult(
        topic="papers",
        title=f"论文库检索：{query[:40]}",
        summary=(
            f"命中 {len(hits)} 篇（其中带解析卡片 {with_card} 篇）。"
            f"全库 {total_papers} 篇、解析卡片 {cards} 张。"
            + ("" if with_card else "本轮命中都没有解析卡片，只能按「仅摘要」引用。")
        ),
        columns=["论文编号", "标题", "可核验程度", "原文片段数"],
        rows=rows,
        total=len(hits),
        params={"query": query},
        extra={"library_total": total_papers, "card_count": cards},
    )


# --------------------------------------------------------------------------- #
# ② 项目
# --------------------------------------------------------------------------- #
async def _lookup_projects(
    session: AsyncSession, *, text: str, conversation_id: str
) -> LookupResult:
    keyword = _extract_terms(text)
    stmt = select(Project.id, Project.name, Project.status, Project.archived).order_by(
        Project.id.desc()
    )
    if keyword:
        stmt = stmt.where(Project.name.ilike(f"%{keyword}%"))
    stmt = stmt.limit(_PAGE_LIMIT)
    rows = [
        [int(pid), name or "", status or "", "已归档" if archived else "在列"]
        for pid, name, status, archived in (await session.execute(stmt)).all()
    ]
    total = int((await session.execute(select(func.count()).select_from(Project))).scalar_one() or 0)
    return LookupResult(
        topic="projects",
        title="项目" + (f"（按「{keyword}」筛选）" if keyword else ""),
        summary=f"平台共有 {total} 个项目，本次列出 {len(rows)} 个。",
        columns=["项目编号", "名称", "状态", "归档"],
        rows=rows,
        total=len(rows),
        params={"keyword": keyword},
        extra={"all_projects": total},
    )


# --------------------------------------------------------------------------- #
# ③ 产出（证据 / idea / 可行性 / 任务书 / 草稿）
# --------------------------------------------------------------------------- #
async def _lookup_artifacts(
    session: AsyncSession, *, text: str, conversation_id: str
) -> LookupResult:
    evidence_rows = (
        await session.execute(
            select(Evidence.id, Evidence.paper_id, Evidence.card_field, Evidence.paper_span_id)
            .where(Evidence.owner_type == "research_node")
            .order_by(Evidence.id.desc())
            .limit(_PAGE_LIMIT)
        )
    ).all()
    idea_rows = (
        await session.execute(
            select(Idea.id, Idea.origin, Idea.title)
            .order_by(Idea.id.desc())
            .limit(_PAGE_LIMIT)
        )
    ).all()
    feas_count = int(
        (await session.execute(select(func.count()).select_from(Feasibility))).scalar_one() or 0
    )
    taskbook_rows = (
        await session.execute(
            select(Taskbook.id, Taskbook.status, Taskbook.project_id, Taskbook.updated_at)
            .order_by(Taskbook.id.desc())
            .limit(8)
        )
    ).all()
    draft_count = int(
        (await session.execute(select(func.count()).select_from(PaperDraft))).scalar_one() or 0
    )

    rows: list[list[Any]] = []
    for eid, paper_id, card_field, span_id in evidence_rows:
        source = card_field or (f"片段 {span_id}" if span_id else "（无来源）")
        rows.append(["证据", f"#{eid}", f"论文 {paper_id}", source])
    for iid, origin, title in idea_rows[:5]:
        rows.append(["假设", f"#{iid}", origin or "", (title or "")[:50]])
    for tid, status, pid, _updated in taskbook_rows[:5]:
        rows.append(["任务书", f"#{tid}", status or "", f"项目 {pid}" if pid else "无项目"])

    return LookupResult(
        topic="artifacts",
        title="平台产出汇总",
        summary=(
            f"证据 {len(evidence_rows)} 条（最近）、假设 {len(idea_rows)} 条（最近）、"
            f"可行性报告 {feas_count} 份、任务书 {len(taskbook_rows)} 份（最近）、论文草稿 {draft_count} 份。"
        ),
        columns=["类型", "编号", "来源/归属", "说明"],
        rows=rows,
        total=len(rows),
        extra={
            "evidence_recent": len(evidence_rows),
            "ideas_recent": len(idea_rows),
            "feasibilities": feas_count,
            "taskbooks_recent": len(taskbook_rows),
            "drafts": draft_count,
        },
    )


# --------------------------------------------------------------------------- #
# ④ 研究链（默认查**本对话**的链）
# --------------------------------------------------------------------------- #
async def _lookup_chain(
    session: AsyncSession, *, text: str, conversation_id: str
) -> LookupResult:
    state = await orchestrator.chain_state(session, conversation_id=conversation_id)
    rows = [
        [
            item["label"],
            item["display_status"],
            item["entry_index"],
            f"{item['retry_count']}/{item['max_retry']}",
            f"${item['cost_usd']:.4f}",
        ]
        for item in state["nodes"]
    ]
    latest = state["transitions"][:5]
    transitions = "\n".join(
        f"- {t['kind']}（{t['trigger']}）：{t['from_node']} → {t['to_node']}"
        for t in latest
    )
    return LookupResult(
        topic="chain",
        title="本对话的研究链",
        summary=(
            f"当前节点「{state['nodes'][[n['node'] for n in state['nodes']].index(state['current_node'])]['label']}」，"
            f"已回退 {state['total_reverts']}/{state['max_total_reverts']} 次，"
            f"迁移记录 {len(state['transitions'])} 条。"
            + ("" if state["has_chain"] else "本对话还没有开研究链。")
        ),
        columns=["节点", "状态", "第几次进入", "重试", "已花费用"],
        rows=rows,
        total=len(state["transitions"]),
        extra={"has_chain": state["has_chain"], "transitions_text": transitions},
    )


_HANDLERS = {
    "papers": _lookup_papers,
    "projects": _lookup_projects,
    "artifacts": _lookup_artifacts,
    "chain": _lookup_chain,
}


async def run_lookup(
    session: AsyncSession, *, topic: str, text: str, conversation_id: str
) -> LookupResult:
    """按主题执行一次确定性查询。未知主题回落到论文检索。"""

    handler = _HANDLERS.get(topic) or _lookup_papers
    return await handler(session, text=text, conversation_id=conversation_id)
