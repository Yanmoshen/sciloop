# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``survey`` 环节：文献调研（WP09-T3，附录 D.1）。

输入：``Taskbook.research_question`` / ``target_datasets`` / ``metrics``
输出：``{queries[], selected_papers[], coverage_note, gaps[]}``
校验：``selected_papers`` 每条必须存在于 ``papers`` 表；``queries.length >= 1``
决策点：**D1 检索策略**（策略动作由 engine 调 WP10 判定；本环节只提出候选动作）

实现口径（不编造数据）：

- 候选论文池**只来自本地 ``papers`` 表真实记录**（WP03 已入库的 arXiv/OpenAlex 数据）
- LLM 只能在候选池范围内挑选与撰写检索式/空白说明；越界 id 一律丢弃并如实记录
- 全文可用性用 ``paper_documents`` 真实 ``parse_status`` / ``coverage`` 统计，不做假设
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, or_, select

from services.pipeline.stages.base import (
    StageContext,
    StageResult,
    StageValidationError,
    call_llm,
    result_cost_of,
    unknown_cost_notes,
)

logger = logging.getLogger("sciloop.pipeline.stage.survey")

CANDIDATE_POOL_SIZE = 40
ABSTRACT_SNIPPET = 420
MAX_KEYWORDS = 12

#: 关键词提取时的停用词（英文常见虚词 + 任务书模板词）
_STOPWORDS = frozenset(
    ["a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with", "without", "by", "is", "are", "be", "as", "at", "from", "into", "over", "under", "using", "use", "used", "via", "towards", "toward", "new", "novel", "approach", "method", "methods", "model", "models", "study", "research", "paper", "survey", "review", "towards", "based", "large", "language", "llm", "llms"]
)

SURVEY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {"type": "string", "minLength": 2},
        },
        "fields": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
        "paper_limit": {"type": "integer", "minimum": 1, "maximum": 50},
        "selected_paper_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 30,
            "items": {"type": "integer"},
        },
        "coverage_note": {"type": "string", "minLength": 1},
        "gaps": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "gap_text": {"type": "string", "minLength": 1},
                    "raised_by_paper_ids": {"type": "array", "items": {"type": "integer"}},
                    "unsolved_evidence": {"type": "string"},
                    "novelty_hint": {"type": "string"},
                },
                "required": ["gap_text"],
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["queries", "selected_paper_ids", "coverage_note"],
}

_SYSTEM_PROMPT = (
    "你是科研辅助系统的「文献调研（survey）」模块。你的输出将作为结构化数据被程序消费。\n"
    "必须严格遵守：\n"
    "1) 只输出合法 JSON，不要任何解释性文字；\n"
    "2) selected_paper_ids 只能从用户给出的候选论文清单中挑选，禁止发明新的 paper_id；\n"
    "3) 检索式与空白说明必须基于候选清单中真实存在的论文，不确定时写 \"unknown\"，禁止编造标题、"
    "引用数、会议或结论；\n"
    "4) 禁止生成任何指向投稿的表述。"
)


async def run(ctx: StageContext) -> StageResult:  # noqa: C901 - 环节主流程，按步骤顺序可读性优先
    taskbook = ctx.taskbook_payload
    if not taskbook.get("research_question"):
        raise StageValidationError(
            "survey 环节要求任务书 research_question 非空（附录 D.1 输入契约）",
            level="L2",
            stage="survey",
        )

    await ctx.progress(10, "读取任务书并构建本地候选论文池")
    pool, pool_note = await _build_candidate_pool(ctx, taskbook)
    if not pool:
        raise StageValidationError(
            "本地 papers 表为空：survey 无候选论文可供调研（L2：需先执行论文抓取或更换检索式）",
            level="L2",
            stage="survey",
            detail={"papers_total": 0},
        )
    await ctx.progress(30, f"候选论文 {len(pool)} 篇，调用 LLM 拟定检索策略（D1）")

    messages = _build_messages(taskbook, pool, ctx)
    result = await call_llm(
        ctx,
        messages,
        json_schema=SURVEY_SCHEMA,
        purpose="survey_selection",
        temperature=0.2,
    )
    payload = result.parsed if isinstance(result.parsed, dict) else None
    if payload is None:
        # schema 校验通过但解析为空属于格式类问题 → L1 自动重试
        raise StageValidationError(
            "survey LLM 未返回可解析的 JSON（L1：格式解析失败，可自动重试）",
            level="L1",
            stage="survey",
            detail={"model_ref": result.model_ref, "content_head": (result.content or "")[:400]},
        )

    queries = _clean_str_list(payload.get("queries"))
    if not queries:
        raise StageValidationError(
            "survey 产出 queries 为空（附录 D.1 校验：queries.length >= 1）",
            level="L1",
            stage="survey",
        )

    pool_by_id = {int(item["paper_id"]): item for item in pool}
    requested = [int(i) for i in payload.get("selected_paper_ids") or [] if _is_int(i)]
    selected, dropped = _resolve_selected(requested, pool_by_id)
    await ctx.progress(70, f"LLM 选定 {len(selected)}/{len(requested)} 篇候选（越界 {len(dropped)}）")

    if not selected:
        raise StageValidationError(
            "survey 选定论文为空（L2：检索结果不可用，需更换检索式后重试）",
            level="L2",
            stage="survey",
            detail={"requested_ids": requested, "pool_size": len(pool)},
        )

    selected, existence_issues = await _verify_papers_exist(ctx, selected)
    if not selected:
        raise StageValidationError(
            "survey 选定论文均无法在 papers 表中查到（L2：数据缺失，禁止编造）",
            level="L2",
            stage="survey",
            detail={"issues": existence_issues},
        )

    gaps, gaps_dropped = _resolve_gaps(payload.get("gaps"), pool_by_id, selected)
    fulltext = await _fulltext_stats(ctx, [item["paper_id"] for item in selected])
    coverage_note = _coverage_note(
        payload_note=str(payload.get("coverage_note") or "").strip(),
        pool_note=pool_note,
        pool_size=len(pool),
        selected=selected,
        fulltext=fulltext,
        dropped_count=len(dropped) + len(gaps_dropped),
    )

    notes = list(unknown_cost_notes(result))
    if dropped:
        notes.append(f"LLM 返回的越界 paper_id 已丢弃（不在候选池内）：{dropped}")
    if gaps_dropped:
        notes.append(f"引用了越界 paper_id 的 gaps 已丢弃：{gaps_dropped}")
    if existence_issues:
        notes.append(f"papers 表存在性校验剔除的记录：{existence_issues}")

    await ctx.progress(95, "survey 产出校验通过，写库")

    output: dict[str, Any] = {
        "queries": queries,
        "selected_papers": selected,
        "coverage_note": coverage_note,
        "gaps": gaps,
        # ---- 以下为审计/展示附加字段（不改变附录 D.1 必需字段） ----
        "fields": _clean_str_list(payload.get("fields")),
        "paper_limit": _clean_paper_limit(payload.get("paper_limit")),
        "candidate_pool_size": len(pool),
        "papers_total": pool_note.get("papers_total"),
        "fulltext": fulltext,
        "decision": {
            "decision_point": "D1",
            "chosen": "retrieval_strategy",
            "rationale": str(payload.get("rationale") or payload.get("coverage_note") or "").strip()
            or "由 LLM 基于任务书与本地候选池提出检索策略（策略动作由风险策略引擎判定）",
            "action_domain": {
                "queries": queries,
                "fields": _clean_str_list(payload.get("fields")),
                "paper_limit": _clean_paper_limit(payload.get("paper_limit")),
            },
        },
        "model_ref": result.model_ref,
        "llm_prompt_hash": result.prompt_hash,
        "is_replay": bool(result.is_replay),
        "generated_at": _utc_now(),
    }
    return StageResult(
        output=output,
        cost_usd=result_cost_of(result),
        verdict=None,
        metrics={"selected_papers": len(selected), "queries": len(queries)},
        quality_ok=True,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# 候选池：只从本地 papers 表取真实记录
# --------------------------------------------------------------------------- #
async def _build_candidate_pool(
    ctx: StageContext, taskbook: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from db.models import Paper

    session = ctx.session
    total = int((await session.execute(select(func.count(Paper.id)))).scalar_one() or 0)

    keywords = _keywords(taskbook)
    stmt = select(Paper)
    if keywords:
        title_hits = [Paper.title.ilike(f"%{kw}%") for kw in keywords]
        abstract_hits = [Paper.abstract.ilike(f"%{kw}%") for kw in keywords]
        stmt = stmt.where(or_(*title_hits, *abstract_hits))
    stmt = stmt.order_by(
        Paper.rank_score.desc().nullslast(),
        Paper.citation_count.desc().nullslast(),
        Paper.id.desc(),
    ).limit(80 if keywords else CANDIDATE_POOL_SIZE)

    rows = list((await session.execute(stmt)).scalars().all())
    matched_by = "keyword" if keywords else "rank_score_fallback"
    if keywords:
        rows.sort(key=lambda p: _match_score(p, keywords), reverse=True)
    rows = rows[:CANDIDATE_POOL_SIZE]

    pool = [
        {
            "paper_id": int(p.id),
            "external_id": p.external_id,
            "title": p.title,
            "venue": p.venue,
            "published_at": p.published_at.isoformat() if p.published_at else None,
            "citation_count": p.citation_count,
            "rank_score": float(p.rank_score) if p.rank_score is not None else None,
            "influence_score": float(p.influence_score) if p.influence_score is not None else None,
            "abstract_snippet": (p.abstract or "")[:ABSTRACT_SNIPPET] or None,
            "match_score": _match_score(p, keywords) if keywords else None,
        }
        for p in rows
    ]
    note = {
        "papers_total": total,
        "matched_by": matched_by,
        "keywords": keywords,
        "pool_size": len(pool),
        "fallback_used": None if keywords else "rank_score",
    }
    if not keywords:
        note["fallback_reason"] = "任务书未提取到可用关键词，按 rank_score 取候选（如实记录，非编造）"
    return pool, note


def _keywords(taskbook: dict[str, Any]) -> list[str]:
    text_parts = [str(taskbook.get("research_question") or "")]
    for value in taskbook.get("target_datasets") or []:
        text_parts.append(str(value))
    for value in taskbook.get("metrics") or []:
        text_parts.append(str(value))
    raw = " ".join(text_parts)

    tokens: list[str] = []
    for chunk in _split_tokens(raw):
        token = chunk.strip().lower()
        if len(token) < 3 or token.isdigit() or token in _STOPWORDS:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens[:MAX_KEYWORDS]


def _split_tokens(text: str) -> list[str]:
    import re

    # 英文/数字词 + 中文连续片段（中文按 2-4 字滑窗，避免分词依赖）
    pieces = re.findall(r"[A-Za-z][A-Za-z0-9\-_]{2,}|\d+\.?\d*|[\u4e00-\u9fff]{2,}", text)
    expanded: list[str] = []
    for piece in pieces:
        if piece[0].isascii():
            expanded.append(piece)
        else:
            if len(piece) <= 4:
                expanded.append(piece)
            else:
                expanded.extend(piece[i : i + 3] for i in range(0, len(piece) - 2, 2))
    return expanded


def _match_score(paper: Any, keywords: list[str]) -> int:
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    score = 0
    for kw in keywords:
        if kw in title:
            score += 3
        if kw in abstract:
            score += 1
    return score


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def _resolve_selected(
    requested: list[int], pool_by_id: dict[int, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[int]]:
    selected: list[dict[str, Any]] = []
    dropped: list[int] = []
    seen: set[int] = set()
    for paper_id in requested:
        if paper_id in seen:
            continue
        seen.add(paper_id)
        item = pool_by_id.get(paper_id)
        if item is None:
            dropped.append(paper_id)
            continue
        selected.append(
            {
                "paper_id": item["paper_id"],
                "external_id": item["external_id"],
                "title": item["title"],
                "venue": item["venue"],
                "published_at": item["published_at"],
                "citation_count": item["citation_count"],
                "rank_score": item["rank_score"],
                "relevance_note": "LLM 在本地候选池内选定（D1 检索策略）",
                "source": "papers_table",
            }
        )
    return selected, dropped


async def _verify_papers_exist(
    ctx: StageContext, selected: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """附录 D.1 硬校验：``selected_papers`` 每条必须存在于 ``papers`` 表。"""
    from db.models import Paper

    ids = [item["paper_id"] for item in selected]
    rows = (
        await ctx.session.execute(select(Paper.id, Paper.title).where(Paper.id.in_(ids)))
    ).all()
    existing = {int(row[0]): row[1] for row in rows}
    kept: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for item in selected:
        real_title = existing.get(item["paper_id"])
        if real_title is None:
            issues.append({"paper_id": item["paper_id"], "reason": "paper_not_found_in_papers"})
            continue
        if real_title != item["title"]:
            item["title_source"] = "papers_table"
            item["title_snapshot"] = item["title"]
            item["title"] = real_title
        item["verified_in_papers"] = True
        kept.append(item)
    return kept, issues


def _resolve_gaps(
    raw_gaps: Any,
    pool_by_id: dict[int, dict[str, Any]],
    selected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int]]:
    selected_ids = {item["paper_id"] for item in selected}
    gaps: list[dict[str, Any]] = []
    dropped_ids: list[int] = []
    for raw in raw_gaps or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("gap_text") or "").strip()
        if not text:
            continue
        raised = [int(i) for i in raw.get("raised_by_paper_ids") or [] if _is_int(i)]
        valid = [i for i in raised if i in pool_by_id and i in selected_ids]
        dropped_ids.extend([i for i in raised if i not in valid])
        gaps.append(
            {
                "gap_text": text,
                "raised_by_paper_ids": valid,
                "unsolved_evidence": str(raw.get("unsolved_evidence") or "").strip() or None,
                "novelty_hint": str(raw.get("novelty_hint") or "").strip() or None,
                "evidence_scope": "abstract_only" if valid else "insufficient",
                "note": None
                if valid
                else "LLM 未给出可核验的 raised_by_paper_ids，已标 insufficient（禁止编造证据）",
            }
        )
    return gaps, dropped_ids


async def _fulltext_stats(ctx: StageContext, paper_ids: list[int]) -> dict[str, Any]:
    """真实统计全文可用性（附录 contracts.evidence_rules.fulltext_gate）。"""
    from db.models import PaperDocument

    if not paper_ids:
        return {"papers_checked": 0, "fulltext_ok": 0, "fulltext_gate_threshold": 0.60}
    rows = (
        await ctx.session.execute(
            select(PaperDocument.paper_id, PaperDocument.parse_status, PaperDocument.coverage)
            .where(PaperDocument.paper_id.in_(paper_ids))
            .order_by(PaperDocument.coverage.desc().nullslast())
        )
    ).all()
    best: dict[int, tuple[str | None, float | None]] = {}
    for paper_id, status, coverage in rows:
        key = int(paper_id)
        if key in best:
            continue
        best[key] = (status, float(coverage) if coverage is not None else None)
    ok = sum(1 for s, c in best.values() if s == "ok" and (c or 0.0) >= 0.60)
    return {
        "papers_checked": len(paper_ids),
        "documents_found": len(best),
        "fulltext_ok": ok,
        "fulltext_missing": len([i for i in paper_ids if i not in best]),
        "fulltext_gate_threshold": 0.60,
        "evidence_scope": "fulltext" if ok else "abstract_only",
    }


def _coverage_note(
    *,
    payload_note: str,
    pool_note: dict[str, Any],
    pool_size: int,
    selected: list[dict[str, Any]],
    fulltext: dict[str, Any],
    dropped_count: int,
) -> str:
    parts = [
        f"候选池来自本地 papers 表真实记录（共 {pool_note.get('papers_total')} 篇，"
        f"本次候选 {pool_size} 篇，匹配方式={pool_note.get('matched_by')}）；"
        f"LLM 从中选定 {len(selected)} 篇。",
    ]
    if pool_note.get("fallback_used"):
        parts.append(f"注意：{pool_note.get('fallback_reason')}")
    parts.append(
        f"全文证据可用性：{fulltext.get('fulltext_ok', 0)}/{fulltext.get('papers_checked', 0)} 篇"
        f"满足 parse_status=ok 且 coverage>={fulltext.get('fulltext_gate_threshold')}，"
        f"其余仅支持摘要级证据（未编造覆盖度）。"
    )
    if dropped_count:
        parts.append(f"越界/不合规条目 {dropped_count} 条已丢弃并留痕。")
    if payload_note:
        parts.append(f"模型说明：{payload_note}")
    return " ".join(parts)


def _build_messages(
    taskbook: dict[str, Any], pool: list[dict[str, Any]], ctx: StageContext
) -> list[dict[str, Any]]:
    user = {
        "taskbook": taskbook,
        "candidate_papers": [
            {
                "paper_id": item["paper_id"],
                "title": item["title"],
                "venue": item["venue"],
                "published_at": item["published_at"],
                "citation_count": item["citation_count"],
                "abstract_snippet": item["abstract_snippet"],
            }
            for item in pool
        ],
        "instruction": (
            "请完成：1) 给出 1-8 条可直接用于 arXiv/OpenAlex 的检索式 queries；2) 给出 fields 领域标签；"
            "3) 给出 paper_limit（1-50）；4) 从 candidate_papers 中挑选与任务书最相关的 paper_id 列表"
            "（selected_paper_ids，必须来自候选清单）；5) 写 coverage_note（说明覆盖情况与不足，"
            "不得夸大）；6) 给出 gaps 空白清单（gap_text 必填，raised_by_paper_ids 只能取已选中的 id）。"
        ),
        "iteration": ctx.iteration,
        "attempt": ctx.attempt,
    }
    if ctx.degrade:
        user["degrade_hint"] = (
            "上一轮检索不达标，本轮按降级策略执行（L2 换检索式/缩小范围）："
            f"{ctx.degrade}。请在 queries 中体现该调整。"
        )
    if ctx.hints:
        user["human_hint"] = ctx.hints
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _json_dumps(user)},
    ]


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_paper_limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 20
    return max(1, min(50, number))


def _is_int(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _json_dumps(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class SurveyStage:
    """``survey`` 环节处理器。"""

    name = "survey"
    decision_point = "D1"

    async def run(self, ctx: StageContext) -> StageResult:
        return await run(ctx)


STAGE = SurveyStage()

__all__ = ["SURVEY_SCHEMA", "STAGE", "SurveyStage", "run"]
