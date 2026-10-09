"""研究节点 v2 适配层：先确认、再检索；不确认就没有检索副作用（计划书 WP-05 + §7.2）。

为什么不放在 ``agent_prompt_v2``：状态机要落事件与 Item，属于运行时能力；
``agent_prompt_v2`` 保持纯函数（不依赖仓储），渲染时才读取研究状态。

契约兼容性说明（刻意为之）：本模块**只使用 v1 已冻结的事件与 Item 类型**——
「需要确认」用 ``input/requested`` + ``turn/waiting_input`` 表达，
研究状态写在 Thread 设置里（``thread/updated`` 补丁）。
计划书 WP-01 列出的 ``research/confirmation_required`` 等新名字，
留待与 Agent 3 协调后的契约 v2 命名轮统一改名，避免此刻打断已交付的两条线。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import EventType, ItemType
from contracts.agent_v2.errors import AgentV2Error


# --------------------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------------------
class ResearchPhase(StrEnum):
    """研究节点状态（计划书 WP-05 的封闭集合）。"""

    IDLE = "idle"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMED = "confirmed"
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    SYNTHESIZING = "synthesizing"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


#: 合法迁移。表外迁移抛 :class:`IllegalResearchTransition`。
RESEARCH_TRANSITIONS: Mapping[ResearchPhase, frozenset[ResearchPhase]] = {
    ResearchPhase.IDLE: frozenset(
        {ResearchPhase.CONFIRMATION_REQUIRED, ResearchPhase.FAILED}
    ),
    ResearchPhase.CONFIRMATION_REQUIRED: frozenset(
        {ResearchPhase.CONFIRMED, ResearchPhase.IDLE, ResearchPhase.INTERRUPTED, ResearchPhase.FAILED}
    ),
    ResearchPhase.CONFIRMED: frozenset(
        {ResearchPhase.PLANNING, ResearchPhase.INTERRUPTED, ResearchPhase.FAILED}
    ),
    ResearchPhase.PLANNING: frozenset(
        {
            ResearchPhase.RETRIEVING,
            ResearchPhase.AWAITING_INPUT,
            ResearchPhase.INTERRUPTED,
            ResearchPhase.FAILED,
        }
    ),
    ResearchPhase.RETRIEVING: frozenset(
        {
            ResearchPhase.SYNTHESIZING,
            ResearchPhase.AWAITING_INPUT,
            ResearchPhase.INTERRUPTED,
            ResearchPhase.FAILED,
        }
    ),
    ResearchPhase.SYNTHESIZING: frozenset(
        {
            ResearchPhase.COMPLETED,
            ResearchPhase.RETRIEVING,
            ResearchPhase.AWAITING_INPUT,
            ResearchPhase.INTERRUPTED,
            ResearchPhase.FAILED,
        }
    ),
    ResearchPhase.AWAITING_INPUT: frozenset(
        {
            ResearchPhase.PLANNING,
            ResearchPhase.RETRIEVING,
            ResearchPhase.SYNTHESIZING,
            ResearchPhase.INTERRUPTED,
            ResearchPhase.FAILED,
        }
    ),
    ResearchPhase.COMPLETED: frozenset(),
    ResearchPhase.FAILED: frozenset(),
    ResearchPhase.INTERRUPTED: frozenset(),
}

#: 终态：不允许再迁移。
TERMINAL_RESEARCH_PHASES: frozenset[ResearchPhase] = frozenset(
    {ResearchPhase.COMPLETED, ResearchPhase.FAILED, ResearchPhase.INTERRUPTED}
)


class IllegalResearchTransition(AgentV2Error):
    """非法的研究阶段迁移。"""

    code = "illegal_research_transition"


class FabricatedCitation(AgentV2Error):
    """来源缺标识——拒绝把无法核验的来源写进结论。"""

    code = "fabricated_citation"


# --------------------------------------------------------------------------------------
# 意图识别
# --------------------------------------------------------------------------------------
#: 触发「是否进入研究」询问的词。命中即**只询问**，不自动开始。
RESEARCH_INTENT_TERMS: tuple[str, ...] = (
    "文献调研",
    "文献综述",
    "调研",
    "研究一下",
    "做个研究",
    "深入分析",
    "系统梳理",
    "综述",
    "文献",
    "论文",
    "开题",
    "找找相关研究",
    "research",
    "literature review",
    "survey",
)

#: 明确同意。
AFFIRMATIVE_TERMS: tuple[str, ...] = (
    "是",
    "是的",
    "对",
    "好",
    "好的",
    "可以",
    "开始",
    "进入研究",
    "确认",
    "同意",
    "yes",
    "ok",
    "sure",
)

#: 明确否定/取消。
NEGATIVE_TERMS: tuple[str, ...] = (
    "不",
    "不用",
    "不要",
    "算了",
    "取消",
    "先不",
    "别",
    "no",
    "cancel",
    "not now",
)

_AFFIRMATIVE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(t) for t in AFFIRMATIVE_TERMS) + r")[\s。！!,.，]*$",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(
    r"^(?:" + "|".join(re.escape(t) for t in NEGATIVE_TERMS) + r")[\s。！!,.，]*$",
    re.IGNORECASE,
)


def detect_research_intent(text: str) -> bool:
    """判断用户这句话是否在研究意图的射程内。"""
    lowered = (text or "").strip().lower()
    return any(term.lower() in lowered for term in RESEARCH_INTENT_TERMS)


def is_affirmative(text: str) -> bool:
    """是否为明确同意。**只认明确表述**——沉默、含糊、反问都不算同意。"""
    return bool(_AFFIRMATIVE_RE.match((text or "").strip()))


def is_negative(text: str) -> bool:
    """是否为明确否定/取消。"""
    return bool(_NEGATIVE_RE.match((text or "").strip()))


# --------------------------------------------------------------------------------------
# 证据
# --------------------------------------------------------------------------------------
@dataclass
class ResearchEvidence:
    """一条可核验的来源记录。"""

    source_id: str
    title: str = ""
    url: str = ""
    author: str = ""
    year: str = ""
    excerpt: str = ""
    #: 是否真的访问到；False 时必须标 uncertain
    accessible: bool = True
    uncertain: bool = False

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise FabricatedCitation(
                "evidence requires a verifiable source_id (DOI / arXiv id / URL)"
            )
        if not (self.url or self.source_id):
            raise FabricatedCitation(
                "evidence requires a locatable identifier (url or source_id)"
            )
        # 来源不可访问 → 强制标记不确定，不允许当成确证使用
        if not self.accessible:
            self.uncertain = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "year": self.year,
            "excerpt": self.excerpt,
            "accessible": self.accessible,
            "uncertain": self.uncertain,
        }


# --------------------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------------------
RESEARCH_SETTINGS_KEY = "research"

#: 研究相关 Item 统一走 PLAN 类型 + payload.kind 判别（见模块头部说明）。
ITEM_KIND_PLAN = "research_plan"
ITEM_KIND_EVIDENCE = "research_evidence"
ITEM_KIND_CONCLUSION = "research_conclusion"


@dataclass
class ResearchNode:
    """研究节点：通过仓储落事件与 Item，自身不留隐藏状态。"""

    repo: Any
    thread_id: str
    clock: Clock = field(default_factory=SystemClock)

    # ---- 读 ----
    def state(self) -> dict[str, Any]:
        settings = self.repo.state(self.thread_id).thread.settings or {}
        raw = settings.get(RESEARCH_SETTINGS_KEY) or {}
        if not isinstance(raw, dict):
            raw = {}
        return {
            "phase": str(raw.get("phase", ResearchPhase.IDLE.value)),
            "confirmed": bool(raw.get("confirmed", False)),
            "question": raw.get("question"),
            "confirmed_at": raw.get("confirmed_at"),
        }

    @property
    def phase(self) -> ResearchPhase:
        return ResearchPhase(self.state()["phase"])

    @property
    def confirmed(self) -> bool:
        return bool(self.state()["confirmed"])

    # ---- 写 ----
    def _patch_settings(self, patch: Mapping[str, Any]) -> None:
        current = dict(self.state())
        merged = {**current, **patch}
        merged.setdefault("phase", ResearchPhase.IDLE.value)
        merged.setdefault("confirmed", False)
        self.repo.emit_event(
            self.thread_id,
            EventType.THREAD_UPDATED,
            payload={"patch": {"settings": {RESEARCH_SETTINGS_KEY: merged}}},
        )

    def _transition(self, to: ResearchPhase) -> None:
        src = self.phase
        if to not in RESEARCH_TRANSITIONS[src]:
            raise IllegalResearchTransition(
                f"illegal research transition: {src.value} -> {to.value}"
            )
        self._patch_settings({"phase": to.value})

    # ---- 生命周期 ----
    def request_confirmation(
        self, turn_id: str, *, question: str, prompt: str | None = None
    ) -> Any:
        """识别到研究意图：**只询问**，把 Turn 置为等待输入，不做任何检索。"""
        self._transition_or_start(ResearchPhase.CONFIRMATION_REQUIRED, question=question)
        item = self.repo.add_item(
            self.thread_id,
            ItemType.PLAN,
            {
                "kind": ITEM_KIND_PLAN,
                "stage": ResearchPhase.CONFIRMATION_REQUIRED.value,
                "question": question,
                "needs_confirmation": True,
            },
            turn_id=turn_id,
        )
        self.repo.wait_for_input(
            self.thread_id,
            turn_id,
            prompt or f"是否进入研究？研究问题：{question}",
        )
        return item

    def _transition_or_start(self, to: ResearchPhase, *, question: str | None = None) -> None:
        if self.phase is ResearchPhase.IDLE and to is ResearchPhase.CONFIRMATION_REQUIRED:
            self._patch_settings(
                {
                    "phase": ResearchPhase.CONFIRMATION_REQUIRED.value,
                    "confirmed": False,
                    "question": question,
                }
            )
            return
        if question is not None:
            self._patch_settings({"question": question})
        self._transition(to)

    def confirm(self, turn_id: str, *, answer: str = "", by: str = "user") -> dict[str, Any]:
        """用户**明确**确认后才置 research_confirmed=true 并进入 planning。

        判定规则（刻意严格，避免「沉默当同意」）：

        - 用户带了文本回答：必须命中明确肯定词；含糊、反问、否定一律拒绝；
        - 用户在界面上明确点了确认（``by="user"`` 且无文本）：视为明确动作，放行；
        - 其它来源（``by != "user"``）且无明确回答：拒绝。
        """
        if self.phase is not ResearchPhase.CONFIRMATION_REQUIRED:
            raise IllegalResearchTransition(f"cannot confirm research in phase {self.phase.value}")
        text = (answer or "").strip()
        if text:
            if not is_affirmative(text):
                raise IllegalResearchTransition(
                    "ambiguous or negative answer does not confirm research; "
                    f"got {text!r}"
                )
        elif by != "user":
            raise IllegalResearchTransition(
                "research confirmation requires an explicit affirmative answer"
            )
        self._patch_settings(
            {
                "phase": ResearchPhase.PLANNING.value,
                "confirmed": True,
                "confirmed_at": self.clock.now_iso(),
                "confirmed_by": by,
            }
        )
        self.repo.add_item(
            self.thread_id,
            ItemType.PLAN,
            {
                "kind": ITEM_KIND_PLAN,
                "stage": ResearchPhase.PLANNING.value,
                "confirmed": True,
                "answer": answer,
            },
            turn_id=turn_id,
        )
        return self.state()

    def deny(self, turn_id: str, *, answer: str = "") -> dict[str, Any]:
        """否定/取消：回到普通聊天，绝不留下已确认状态。"""
        self._patch_settings({"phase": ResearchPhase.IDLE.value, "confirmed": False})
        self.repo.add_item(
            self.thread_id,
            ItemType.PLAN,
            {
                "kind": ITEM_KIND_PLAN,
                "stage": ResearchPhase.IDLE.value,
                "confirmed": False,
                "answer": answer,
            },
            turn_id=turn_id,
        )
        return self.state()

    def advance(self, turn_id: str, to: ResearchPhase, *, payload: Mapping[str, Any] | None = None) -> None:
        """推进到下一个阶段（表外迁移会抛错）。"""
        if not self.confirmed and to in {ResearchPhase.RETRIEVING, ResearchPhase.SYNTHESIZING}:
            raise IllegalResearchTransition(
                "cannot retrieve or synthesize before research is confirmed"
            )
        self._transition(to)
        if payload:
            self.repo.add_item(
                self.thread_id,
                ItemType.PLAN,
                {"kind": ITEM_KIND_PLAN, "stage": to.value, **dict(payload)},
                turn_id=turn_id,
            )

    def record_evidence(self, turn_id: str, evidence: ResearchEvidence) -> Any:
        """记录一条来源。不可访问的来源会被标记 uncertain，而不是被丢掉。"""
        return self.repo.add_item(
            self.thread_id,
            ItemType.PLAN,
            {"kind": ITEM_KIND_EVIDENCE, **evidence.to_dict()},
            turn_id=turn_id,
        )

    def conclusion(
        self, turn_id: str, *, text: str, citations: Mapping[str, str] | None = None
    ) -> Any:
        """记录结论与引用映射；引用必须指向**已经记录过的**来源。"""
        known = self.evidence_ids()
        for key, source_id in (citations or {}).items():
            if source_id not in known:
                raise FabricatedCitation(
                    f"citation {key!r} points to unknown source {source_id!r}; "
                    "record evidence before citing it"
                )
        return self.repo.add_item(
            self.thread_id,
            ItemType.PLAN,
            {
                "kind": ITEM_KIND_CONCLUSION,
                "text": text,
                "citations": dict(citations or {}),
                "uncertain_sources": sorted(
                    e["source_id"]
                    for e in self.evidence()
                    if e.get("uncertain") and e["source_id"] in set((citations or {}).values())
                ),
            },
            turn_id=turn_id,
        )

    # ---- 查询 ----
    def evidence(self) -> list[dict[str, Any]]:
        records = []
        for item in self.repo.state(self.thread_id).items_of_type(ItemType.PLAN):
            if item.payload.get("kind") == ITEM_KIND_EVIDENCE:
                records.append(dict(item.payload))
        return records

    def evidence_ids(self) -> set[str]:
        return {str(e.get("source_id")) for e in self.evidence()}

    def summaries(self) -> list[dict[str, Any]]:
        return [
            dict(item.payload)
            for item in self.repo.state(self.thread_id).items_of_type(ItemType.PLAN)
            if item.payload.get("kind") == ITEM_KIND_CONCLUSION
        ]


__all__ = [
    "RESEARCH_TRANSITIONS",
    "TERMINAL_RESEARCH_PHASES",
    "AFFIRMATIVE_TERMS",
    "NEGATIVE_TERMS",
    "RESEARCH_INTENT_TERMS",
    "FabricatedCitation",
    "IllegalResearchTransition",
    "ResearchEvidence",
    "ResearchNode",
    "ResearchPhase",
    "detect_research_intent",
    "is_affirmative",
    "is_negative",
]
