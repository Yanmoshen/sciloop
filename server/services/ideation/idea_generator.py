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
"""idea 生成与证据强制绑定（WP08-T4）。

两种生成模式（**模式永远如实标注，不冒充 LLM**）
------------------------------------------------
``llm``
    走 WP02 :func:`llm.adapter.chat`（结构化输出 + 每次调用写 ``llm_call_logs``）。
    prompt 中把候选证据编成 **编号引用表** ``E1/E2/...``，模型只能回引用号；
    未在表中的引用号一律丢弃（从机制上杜绝「模型编造 paper_span_id」）。
``template``
    不调用 LLM 的**规则合成**：用 Gap 原文实词 + 机制模板拼出 idea，
    ``generation_mode='deterministic_template'`` 并写进 ``novelty_note``，
    前端据此显示「模板合成（未调用 LLM）」。

无论哪种模式，**证据接地候选（Gap 的真实 span / card_field）都会绑定**，
最后统一跑 :func:`services.ideation.evidence_binder.enforce_evidence_or_drop`：
``evidences`` 为空的 idea **直接丢弃**（删除刚插入的行）并记 warning，
API 回显 ``discarded`` 明细供审计。
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Idea
from services.aggregation.aggregation_service import load_gaps
from services.cost import check_cost
from services.ideation.evidence_binder import (
    bind_idea_evidence,
    enforce_evidence_or_drop,
    gap_candidates,
)

logger = logging.getLogger("sciloop.wp08.idea_generator")

#: 四个创新方向（2026-09-26 研究者口径：**固定四个方向**，每个方向产出 idea）
#: ⚠️ ``combination`` / ``transfer`` / ``refinement`` 是 2026-09-26 之前就有的值，
#: 存量数据仍在用 → **只能新增、不能改名**；``paradigm`` 是本次新增的第四个方向。
MECHANISMS: tuple[str, ...] = ("refinement", "transfer", "combination", "paradigm")

#: mechanism → 方向中文名（提示词与界面共用同一份口径，避免两处漂移）
MECHANISM_LABELS: dict[str, str] = {
    "refinement": "方法迭代型",
    "transfer": "场景迁移型",
    "combination": "技术融合型",
    "paradigm": "范式拓展型",
}

#: 每个方向的定位（写进提示词，让模型知道这个方向该产出什么层次的创新）
MECHANISM_BRIEFS: dict[str, str] = {
    "refinement": "针对原论文的明确缺陷、瓶颈或局限性做定向改进（增量创新）",
    "transfer": "把原论文的核心方法迁移到未覆盖的任务、领域或模态（拓展创新）",
    "combination": "与其他领域的前沿技术交叉融合（交叉创新）",
    "paradigm": "基于方法内核向上拓展，提出新的研究框架或技术体系（范式创新）",
}

ORIGIN_AI = "ai_generated"
ORIGIN_USER = "user_input"

MODE_LLM = "llm"
MODE_TEMPLATE = "template"
MODE_AUTO = "auto"

#: 生成阶段使用的 stage 标签（写入 llm_call_logs.stage）
LLM_STAGE = "plan"
LLM_PURPOSE = "idea_generation"

#: 单次生成最多消费的证据引用数（防止 prompt 膨胀）
MAX_REFS = 24
MAX_IDEAS = 20

_IDEA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ideas"],
    "properties": {
        "ideas": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "content", "mechanism", "gap_index", "evidence_ref_ids"],
                "properties": {
                    "title": {"type": "string", "description": "一句话 idea 标题"},
                    "content": {
                        "type": "string",
                        "description": "研究方案描述：做什么、怎么做、为什么能补该空白",
                    },
                    "mechanism": {"type": "string", "enum": list(MECHANISMS)},
                    "gap_index": {
                        "type": "integer",
                        "description": "该 idea 针对的 Gap 序号（0 基，见 prompt 中的空白列表）",
                    },
                    "novelty_note": {"type": "string", "description": "相对已有工作的差异点"},
                    "evidence_ref_ids": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": "只能从 prompt 给出的编号引用表 E1/E2/... 中选取",
                    },
                },
            },
        }
    },
}

SYSTEM_PROMPT = (
    "你是严谨的科研选题助手。给定若干条**有原文证据支撑**的研究空白（Gap），"
    "按**四个固定创新方向**产出可执行的研究 idea。硬性要求：\n"
    "1) mechanism 只能取下列四个之一，且必须与该方向的定位一致：\n"
    "   refinement＝方法迭代型（针对原论文的缺陷做定向改进，增量创新）；\n"
    "   transfer＝场景迁移型（把核心方法迁到未覆盖的任务/领域/模态，拓展创新）；\n"
    "   combination＝技术融合型（与其他领域的前沿技术交叉融合，交叉创新）；\n"
    "   paradigm＝范式拓展型（向上提出新的研究框架或技术体系，范式创新）；\n"
    "2) 请求里点名的**每个方向都要有产出**，且不同方向的 idea 不得内容重叠；\n"
    "3) 每条 idea 必须针对明确的 Gap（用 gap_index 指明）；\n"
    "4) evidence_ref_ids 只能从材料中给出的编号引用表里选，**禁止编造编号**；\n"
    "5) 不得虚构数据、指标、数据集或引用；材料里没有的信息写「材料未提供」；\n"
    "6) 只输出 JSON，符合给定 schema。"
)


class IdeaGenerationError(Exception):
    """生成失败（API 层转 4xx/5xx）。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def build_prompt(
    gaps: Sequence[Mapping[str, Any]],
    count: int = 1,
    directions: Sequence[str] | None = None,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """构造 prompt 与「编号引用表 → 候选证据」映射（纯函数，便于离线核对）。

    ``directions`` 给定时按「**每个方向各 ``count`` 条**」产出（四方向固定口径）；
    不给定则沿用旧口径：总共 ``count`` 条、方向不限。
    """
    refs: dict[str, dict[str, Any]] = {}
    targets = [key for key in (directions or ()) if key in MECHANISMS]
    if targets:
        head: list[str] = [
            f"请针对下列 {len(targets)} 个创新方向，**每个方向各产出 {count} 条** idea：",
            "",
        ]
        head += [
            f"- {MECHANISM_LABELS[key]}（mechanism={key}）：{MECHANISM_BRIEFS[key]}"
            for key in targets
        ]
        head += ["", "不同方向的 idea 不得在内容上重叠。", "", "## 研究空白（Gap）"]
    else:
        head = [f"请产出 {count} 条研究 idea，覆盖下列空白（可跨条组合）。", "", "## 研究空白（Gap）"]
    lines: list[str] = list(head)
    ref_seq = 0
    for index, gap in enumerate(gaps):
        lines.append(f"[Gap {index}] （提出者论文 {gap.get('raised_by_paper_ids')}）{gap.get('gap_text')}")
        for span in gap.get("unsolved_evidence") or []:
            if ref_seq >= MAX_REFS:
                break
            ref_seq += 1
            ref_id = f"E{ref_seq}"
            candidate: dict[str, Any] = {
                "evidence_type": "paper_span",
                "paper_span_id": span.get("paper_span_id"),
                "paper_id": span.get("paper_id"),
                "weight": 1.0,
            }
            if span.get("quote_text"):
                candidate["quote_text"] = span["quote_text"]
            refs[ref_id] = candidate
            lines.append(
                f"    {ref_id} = 原文段落（论文 {span.get('paper_id')}，"
                f"章节 {span.get('section_name')}）：{str(span.get('quote_text') or '')[:280]}"
            )
        for ref in gap.get("raised_by") or []:
            if ref_seq >= MAX_REFS:
                break
            evidence = ref.get("evidence") or {}
            candidate = evidence.get("candidate")
            if not isinstance(candidate, Mapping):
                continue
            ref_seq += 1
            ref_id = f"E{ref_seq}"
            refs[ref_id] = dict(candidate)
            lines.append(
                f"    {ref_id} = 卡片字段 {candidate.get('card_field')}"
                f"（论文 {candidate.get('paper_id')}）：{str(ref.get('quote_text') or '')[:200]}"
            )

    lines += [
        "",
        "## 编号引用表（evidence_ref_ids 只能取这里的编号）",
        "、".join(refs) or "（本聚合无可用证据引用）",
        "",
        "## 输出要求",
        "JSON：{ideas:[{title, content, mechanism, gap_index, novelty_note, evidence_ref_ids}]}",
    ]
    return "\n".join(lines), refs


def _template_ideas(
    gaps: Sequence[Mapping[str, Any]],
    count: int = 1,
    directions: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """规则合成：不调用 LLM，用 Gap 原文实词 + 机制模板拼装（如实标注模式）。

    ``(gap, mechanism)`` 组合唯一：同一方向在 Gap 之间轮转取素材，组合用尽即停。
    ``directions`` 给定时只出这些方向、每个方向 ``count`` 条；不给定时沿用旧口径（共 ``count`` 条）。
    """
    targets = [key for key in (directions or ()) if key in MECHANISMS] or list(MECHANISMS)
    want = len(targets) * int(count) if directions else int(count)
    ideas: list[dict[str, Any]] = []
    used: set[tuple[int, str]] = set()
    for slot in range(max(1, len(gaps)) * len(targets)):
        if len(ideas) >= want:
            break
        mechanism = targets[slot % len(targets)]
        gap_index = (slot // len(targets)) % max(1, len(gaps))
        if (gap_index, mechanism) in used:
            continue
        used.add((gap_index, mechanism))
        gap = gaps[gap_index]
        gap_text = str(gap.get("gap_text") or "").strip()
        papers = gap.get("raised_by_paper_ids") or []
        if mechanism == "combination":
            plan = "把该空白与相邻问题的已有做法组合：取其可用组件，补齐空白所缺的一环，先做最小对照实验。"
        elif mechanism == "transfer":
            plan = "把其他任务上已成熟的机制迁移过来，替换掉当前受限于该空白的默认做法，并设计迁移前后对照。"
        elif mechanism == "paradigm":
            plan = "不满足于替换组件：把该空白当成更大的框架缺口，先写清分层接口与评价口径，再做最小可验证原型。"
        else:
            plan = "不更换整体框架，只针对该空白做细粒度改进，控制变量以隔离改进来源。"
        ideas.append(
            {
                "title": f"[模板] 针对论文 {papers} 提出空白的{MECHANISM_LABELS[mechanism]}方案",
                "content": f"问题（来自原文证据）：{gap_text}\n做法：{plan}\n"
                f"验证：以提出者论文的实验设置为基线，先跑最小可行实验，再逐步放大。",
                "mechanism": mechanism,
                "gap_index": gap_index,
                "novelty_note": (
                    "模板合成（未调用 LLM）：内容由 Gap 原文与机制模板拼装，"
                    "新颖性尚未经模型或人工评估，需研究者核验"
                ),
                "evidence_ref_ids": [],
            }
        )
    return ideas


async def _llm_ideas(
    gaps: Sequence[Mapping[str, Any]],
    count: int,
    *,
    directions: Sequence[str] | None = None,
    model_ref: str | None,
    project_id: int | None,
    temperature: float,
    max_tokens: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """调用 WP02 ``chat`` 生成 idea；返回 ``(ideas, llm_meta)``。"""
    from llm.adapter import chat  # 局部导入：避免无 LLM 场景下引入导入期依赖

    prompt, refs = build_prompt(gaps, count, directions)
    if not refs:
        raise IdeaGenerationError(
            "no_evidence_available",
            "该聚合没有任何可用证据引用，拒绝对无证据素材生成 idea（不允许输出无证据 idea）",
            {"gap_count": len(gaps)},
        )
    check = await check_cost(project_id, estimated_usd=0.02)
    if not check.ok:
        raise IdeaGenerationError(
            "cost_guardrail",
            check.reason or "成本护栏拒绝本次调用",
            check.to_dict() if hasattr(check, "to_dict") else None,
        )

    result = await chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        model_ref,
        temperature,
        max_tokens,
        _IDEA_SCHEMA,
        project_id=project_id,
        stage=LLM_STAGE,
        purpose=LLM_PURPOSE,
    )
    parsed = result.parsed if isinstance(result.parsed, Mapping) else None
    if not parsed or not isinstance(parsed.get("ideas"), list):
        raise IdeaGenerationError(
            "llm_invalid_output",
            "LLM 未返回符合 schema 的 ideas 数组",
            {"model_ref": result.model_ref, "finish_reason": result.finish_reason},
        )
    ideas = [dict(item) for item in parsed["ideas"]]
    llm_meta = {
        "model_ref": result.model_ref,
        "provider": result.provider,
        "model_id": result.model_id,
        "resolved_from": result.resolved_from,
        "cost_usd": result.cost_usd,
        "cost_unknown_reason": result.cost_unknown_reason,
        "usage": dict(result.usage.__dict__) if hasattr(result.usage, "__dict__") else None,
        "duration_ms": result.duration_ms,
        "attempts": result.attempts,
        "is_replay": result.is_replay,
        "prompt_hash": result.prompt_hash,
        "ref_table_size": len(refs),
    }
    return ideas, llm_meta


def candidates_for_idea(
    idea: Mapping[str, Any],
    gaps: Sequence[Mapping[str, Any]],
    refs: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把模型给的引用号映射为真实候选；未在表中的编号如实计入 ``invalid_refs``。"""
    candidates: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for ref_id in idea.get("evidence_ref_ids") or []:
        key = str(ref_id).strip()
        candidate = refs.get(key)
        if candidate is None:
            invalid.append({"ref_id": key, "reason": "not_in_ref_table（疑似编造引用号）"})
            continue
        candidates.append(dict(candidate))

    gap_index = idea.get("gap_index")
    if isinstance(gap_index, int) and 0 <= gap_index < len(gaps):
        candidates.extend(gap_candidates(gaps[gap_index]))
    return candidates, invalid


async def generate_ideas(
    session: AsyncSession,
    *,
    aggregation_id: int,
    count: int = 5,
    directions: Sequence[str] | None = None,
    project_id: int | None = None,
    mode: str = MODE_AUTO,
    model_ref: str | None = None,
    temperature: float = 0.4,
    # 不设输出上限（原先 2000；推理类模型的推理与正文共用同一份预算，限死会让正文被截断）
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """生成 idea → 绑定证据 → **丢弃无证据条目** → 落库。

    2026-09-26 起支持**四方向固定口径**：``directions`` 给出要产出的方向（不传＝沿用
    「共 count 条、方向不限」的旧口径）；给出时 ``count`` 的含义变为此前**每个方向各几条**
    ——「对某个方向不满意、再来一批备选」就是只传那一个方向。
    """
    if mode not in (MODE_LLM, MODE_TEMPLATE, MODE_AUTO):
        raise IdeaGenerationError(
            "invalid_mode", f"mode='{mode}' 不在 {MODE_LLM}/{MODE_TEMPLATE}/{MODE_AUTO} 内"
        )
    targets = tuple(dict.fromkeys(str(item) for item in directions)) if directions else None
    if targets:
        unknown = [key for key in targets if key not in MECHANISMS]
        if unknown:
            raise IdeaGenerationError(
                "invalid_direction",
                f"未知的创新方向：{', '.join(unknown)}",
                {"allowed": list(MECHANISMS), "labels": MECHANISM_LABELS},
            )
        total = len(targets) * int(count)
    else:
        total = int(count)
    if not 1 <= int(count) <= MAX_IDEAS or not 1 <= total <= MAX_IDEAS:
        raise IdeaGenerationError(
            "invalid_count",
            f"count 必须在 1–{MAX_IDEAS} 之间"
            f"（四方向模式下 count 是「每个方向几条」，总数不得超过 {MAX_IDEAS}）",
            {"count": count, "total": total},
        )

    gaps = await load_gaps(session, int(aggregation_id))
    if not gaps:
        raise IdeaGenerationError(
            "no_gaps",
            f"聚合 {aggregation_id} 没有空白记录：请先用 POST /aggregations 生成空白清单",
            {"aggregation_id": int(aggregation_id)},
        )

    llm_meta: dict[str, Any] | None = None
    llm_error: dict[str, Any] | None = None
    generation_mode = MODE_TEMPLATE
    refs: dict[str, dict[str, Any]] = {}
    raw_ideas: list[dict[str, Any]] = []

    if mode in (MODE_LLM, MODE_AUTO):
        try:
            raw_ideas, llm_meta = await _llm_ideas(
                gaps,
                int(count),
                directions=targets,
                model_ref=model_ref,
                project_id=project_id,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            generation_mode = MODE_LLM
            _, refs = build_prompt(gaps, int(count), targets)
        except Exception as exc:  # noqa: BLE001 - 需要区分「未配 LLM」与「真失败」
            detail = getattr(exc, "detail", None)
            llm_error = {
                "type": type(exc).__name__,
                "code": getattr(exc, "code", "llm_unavailable"),
                "message": str(exc),
                "detail": detail,
            }
            logger.warning("idea LLM 生成失败，按 mode=%s 处理：%s", mode, exc)
            if mode == MODE_LLM:
                raise IdeaGenerationError(
                    "llm_unavailable",
                    f"mode=llm 要求可用模型，本次调用失败：{exc}",
                    llm_error,
                ) from exc

    if generation_mode == MODE_TEMPLATE:
        raw_ideas = _template_ideas(gaps, int(count), targets)

    created_ids: list[int] = []
    ref_audit: list[dict[str, Any]] = []
    for created, raw in enumerate(raw_ideas):
        if created >= total:
            break
        candidate_idea = dict(raw)
        candidates, invalid_refs = candidates_for_idea(candidate_idea, gaps, refs)
        idea = Idea(
            project_id=project_id,
            aggregation_id=int(aggregation_id),
            origin=ORIGIN_AI,
            title=str(candidate_idea.get("title") or "未命名 idea")[:2000],
            content=str(candidate_idea.get("content") or ""),
            mechanism=(
                str(candidate_idea.get("mechanism"))
                if candidate_idea.get("mechanism") in MECHANISMS
                else None
            ),
            novelty_note=(
                str(candidate_idea.get("novelty_note"))
                if candidate_idea.get("novelty_note")
                else None
            ),
        )
        session.add(idea)
        await session.flush()
        idea_id = int(idea.id)

        binding = await bind_idea_evidence(session, idea_id, candidates)
        ref_audit.append(
            {
                "idea_id": idea_id,
                "gap_index": candidate_idea.get("gap_index"),
                "submitted": binding.get("submitted"),
                "bound_ids": binding.get("bound_ids"),
                "rejected_total": binding.get("rejected_total"),
                "invalid_refs": invalid_refs,
            }
        )
        if invalid_refs:
            logger.warning(
                "idea#%s 含 %d 个非法证据引用号（不在引用表内），已丢弃：%s",
                idea_id,
                len(invalid_refs),
                [item["ref_id"] for item in invalid_refs],
            )
        created_ids.append(idea_id)

    await session.commit()

    gate = await enforce_evidence_or_drop(session, created_ids, context="generate")
    dropped_ids = {int(item["idea_id"]) for item in gate["dropped"]}
    if dropped_ids:
        await session.execute(delete(Idea).where(Idea.id.in_(list(dropped_ids))))
        await session.commit()
        logger.warning(
            "已删除 %d 条无 Evidence 的 idea：%s", len(dropped_ids), sorted(dropped_ids)
        )

    from services.ideation.idea_service import load_idea  # 局部导入避免循环

    items = []
    for idea_id in gate["kept"]:
        item = await load_idea(session, int(idea_id))
        if item is not None:
            items.append(item)

    return {
        "aggregation_id": int(aggregation_id),
        "project_id": project_id,
        "requested_count": total,
        #: 本次要求产出的方向（null＝旧口径「方向不限」）
        "requested_directions": list(targets) if targets else None,
        "generated_count": len(items),
        "created_count": len(created_ids),
        "discarded_count": len(dropped_ids),
        "generation_mode": generation_mode,
        "llm": llm_meta,
        "llm_error": llm_error,
        "evidence_policy": "无 Evidence 的 idea 必须丢弃，不允许输出（服务端强制）",
        "discarded": gate["dropped"],
        "evidence_audit": ref_audit,
        "items": items,
        "used_gaps": [
            {
                "id": gap.get("id"),
                "gap_text": gap.get("gap_text"),
                "raised_by_paper_ids": gap.get("raised_by_paper_ids"),
                "span_count": gap.get("span_count"),
            }
            for gap in gaps
        ],
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "compliance_note": "本内容由 AI 辅助生成，需研究者自行核验",
    }


async def load_idea_ids_for_aggregation(
    session: AsyncSession, aggregation_id: int
) -> list[int]:
    rows = (
        await session.execute(
            select(Idea.id).where(Idea.aggregation_id == int(aggregation_id)).order_by(Idea.id)
        )
    ).scalars().all()
    return [int(value) for value in rows]


__all__ = [
    "LLM_PURPOSE",
    "LLM_STAGE",
    "MAX_IDEAS",
    "MAX_REFS",
    "MECHANISM_BRIEFS",
    "MECHANISM_LABELS",
    "MECHANISMS",
    "MODE_AUTO",
    "MODE_LLM",
    "MODE_TEMPLATE",
    "ORIGIN_AI",
    "ORIGIN_USER",
    "IdeaGenerationError",
    "build_prompt",
    "candidates_for_idea",
    "generate_ideas",
    "load_idea_ids_for_aggregation",
]
