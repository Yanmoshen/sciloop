"""Recap：压缩摘要的结构化字段与状态标记（计划书 WP-06）。

两条硬要求：

1. 摘要字段固定为 ``goal / decisions / authorizations / completed / in_progress /
   blocked / errors / important_paths / next_steps / research_state / memory_refs``；
2. **每条事实必须带状态标记**，取值 ``proposed / queued / implemented / tested /
   published / installed``——「把计划当完成」是最危险的失真，因此
   ``proposed``/``queued`` 的条目不允许出现在 ``completed`` 里（构造期即断言）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from contracts.agent_v2.errors import AgentV2Error


class RecapViolation(AgentV2Error):
    """摘要结构不合法（把计划当完成、字段缺失等）。"""

    code = "recap_violation"


class RecapStatus(StrEnum):
    """事实的推进状态。``proposed``/``queued`` 属于**尚未完成**。"""

    PROPOSED = "proposed"
    QUEUED = "queued"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    PUBLISHED = "published"
    INSTALLED = "installed"


#: 视为「尚未完成」的状态。
UNFINISHED_STATUSES: frozenset[RecapStatus] = frozenset(
    {RecapStatus.PROPOSED, RecapStatus.QUEUED}
)

#: 摘要的固定字段顺序（渲染与解析都以它为准）。
RECAP_FIELDS: tuple[str, ...] = (
    "goal",
    "decisions",
    "authorizations",
    "completed",
    "in_progress",
    "blocked",
    "errors",
    "important_paths",
    "next_steps",
    "research_state",
    "memory_refs",
)

#: 字段 -> 章节标题（中英并列，解析时两种都认）。
FIELD_TITLES: Mapping[str, str] = {
    "goal": "目标 / goal",
    "decisions": "已定决策 / decisions",
    "authorizations": "已获授权 / authorizations",
    "completed": "已完成 / completed",
    "in_progress": "进行中 / in_progress",
    "blocked": "阻塞 / blocked",
    "errors": "错误 / errors",
    "important_paths": "重要路径 / important_paths",
    "next_steps": "下一步 / next_steps",
    "research_state": "研究状态 / research_state",
    "memory_refs": "记忆引用 / memory_refs",
}


@dataclass
class RecapFact:
    """一条带状态标记的事实。"""

    text: str
    status: RecapStatus = RecapStatus.IMPLEMENTED

    def __post_init__(self) -> None:
        text = (self.text or "").strip()
        if not text:
            raise RecapViolation("recap fact must not be empty")
        self.text = text
        if not isinstance(self.status, RecapStatus):
            try:
                self.status = RecapStatus(self.status)
            except ValueError as exc:
                allowed = sorted(m.value for m in RecapStatus)
                raise RecapViolation(
                    f"{self.status!r} is not a valid recap status; allowed: {allowed}"
                ) from exc

    def render(self) -> str:
        return f"- [{self.status.value}] {self.text}"

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "status": self.status.value}


@dataclass
class Recap:
    """结构化摘要。标量字段为字符串，列表字段为 :class:`RecapFact` 或普通字符串。"""

    goal: str = ""
    decisions: list[str] = field(default_factory=list)
    authorizations: list[str] = field(default_factory=list)
    completed: list[RecapFact] = field(default_factory=list)
    in_progress: list[RecapFact] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    important_paths: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    research_state: str = ""
    memory_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 「把计划当完成」必须当场失败，而不是等到渲染出来才被发现
        offenders = [
            fact.text for fact in self.completed if fact.status in UNFINISHED_STATUSES
        ]
        if offenders:
            raise RecapViolation(
                "completed must not contain unfinished facts "
                f"(proposed/queued): {offenders}"
            )

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "decisions": list(self.decisions),
            "authorizations": list(self.authorizations),
            "completed": [f.to_dict() for f in self.completed],
            "in_progress": [f.to_dict() for f in self.in_progress],
            "blocked": list(self.blocked),
            "errors": list(self.errors),
            "important_paths": list(self.important_paths),
            "next_steps": list(self.next_steps),
            "research_state": self.research_state,
            "memory_refs": list(self.memory_refs),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Recap:
        def facts(key: str) -> list[RecapFact]:
            out = []
            for item in raw.get(key) or []:
                if isinstance(item, Mapping):
                    out.append(RecapFact(str(item.get("text", "")), item.get("status")))
                else:
                    out.append(RecapFact(str(item)))
            return out

        def strings(key: str) -> list[str]:
            return [str(x) for x in (raw.get(key) or []) if str(x).strip()]

        return cls(
            goal=str(raw.get("goal") or ""),
            decisions=strings("decisions"),
            authorizations=strings("authorizations"),
            completed=facts("completed"),
            in_progress=facts("in_progress"),
            blocked=strings("blocked"),
            errors=strings("errors"),
            important_paths=strings("important_paths"),
            next_steps=strings("next_steps"),
            research_state=str(raw.get("research_state") or ""),
            memory_refs=strings("memory_refs"),
        )

    # ---- 渲染 ----
    def render(self) -> str:
        """渲染成人类可读摘要（也是压缩事件里 ``summary`` 的正文）。"""
        parts: list[str] = []
        if self.goal:
            parts.append(f"### {FIELD_TITLES['goal']}\n{self.goal}")
        for scalar_key in ("decisions", "authorizations", "blocked", "errors",
                           "important_paths", "next_steps", "memory_refs"):
            values = getattr(self, scalar_key)
            if values:
                body = "\n".join(f"- {v}" for v in values)
                parts.append(f"### {FIELD_TITLES[scalar_key]}\n{body}")
        for fact_key in ("completed", "in_progress"):
            values = getattr(self, fact_key)
            if values:
                body = "\n".join(f.render() for f in values)
                parts.append(f"### {FIELD_TITLES[fact_key]}\n{body}")
        if self.research_state:
            parts.append(f"### {FIELD_TITLES['research_state']}\n{self.research_state}")
        return "\n\n".join(parts)

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.render()) // 4)


# --------------------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------------------
def _build_title_index() -> dict[str, str]:
    """标题索引：整条标题、中文单边、英文单边都能命中。"""
    index: dict[str, str] = {}
    for field_name, title in FIELD_TITLES.items():
        index[title] = field_name
        for part in title.split(" / "):
            part = part.strip()
            if part:
                index[part] = field_name
    return index


_TITLE_TO_FIELD: dict[str, str] = _build_title_index()


def _field_for(label: str) -> str | None:
    """把一行可能的标题解析成字段名；不是标题返回 None。"""
    label = label.strip().strip("#").strip()
    if not label:
        return None
    if label in _TITLE_TO_FIELD:
        return _TITLE_TO_FIELD[label]
    for part in re.split(r"[/|]", label):
        part = part.strip()
        if part in _TITLE_TO_FIELD:
            return _TITLE_TO_FIELD[part]
    return None

_STATUS_RE = re.compile(
    r"^\s*[-*]\s*(?:\[(?P<bracket>[a-z]+)\]\s*)?(?P<text>.+?)\s*$", re.IGNORECASE
)
_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(?P<title>[^:\n#]{1,40}?)\s*[:：]?\s*$")


def parse_recap(text: str) -> Recap:
    """把摘要文本解析成 :class:`Recap`。

    认 ``### 标题`` 与 ``标题:`` 两种写法，标题中英皆可；条目认 ``- [status] 内容``
    与 ``- 内容``。无法识别的标题会被忽略（内容不回填，避免猜错语义）。
    """
    buckets: dict[str, list[str]] = {key: [] for key in RECAP_FIELDS}
    current: str | None = None
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip().lstrip("#").strip()
        key_guess = stripped.split(":")[0].split("：")[0].strip()
        field_name = _field_for(key_guess)
        if field_name is not None:
            current = field_name
            continue
        if current is None:
            continue
        buckets[current].append(line)

    def scalars(key: str) -> list[str]:
        out = []
        for line in buckets[key]:
            match = _STATUS_RE.match(line)
            item = match.group("text") if match else line.strip()
            item = item.strip("-* ").strip()
            if item:
                out.append(item)
        return out

    def facts(key: str) -> list[RecapFact]:
        out = []
        for line in buckets[key]:
            match = _STATUS_RE.match(line)
            if match is None:
                continue
            status = match.group("bracket")
            try:
                out.append(RecapFact(match.group("text"), status or RecapStatus.IMPLEMENTED))
            except RecapViolation:
                # 状态标记非法时不猜：降级为通用文本，交给上层人工修正
                out.append(RecapFact(match.group("text"), RecapStatus.IMPLEMENTED))
        return out

    goal_lines = scalars("goal")
    research_lines = scalars("research_state")
    return Recap(
        goal=" ".join(goal_lines),
        decisions=scalars("decisions"),
        authorizations=scalars("authorizations"),
        completed=facts("completed"),
        in_progress=facts("in_progress"),
        blocked=scalars("blocked"),
        errors=scalars("errors"),
        important_paths=scalars("important_paths"),
        next_steps=scalars("next_steps"),
        research_state=" ".join(research_lines),
        memory_refs=scalars("memory_refs"),
    )


def coerce_recap(value: Any) -> Recap:
    """把「模型产出的文本」或「已是 Recap/dict」统一成 :class:`Recap`。"""
    if isinstance(value, Recap):
        return value
    if isinstance(value, Mapping):
        return Recap.from_dict(value)
    return parse_recap(str(value or ""))


def build_recap(
    *,
    goal: str = "",
    completed: Iterable[Any] = (),
    in_progress: Iterable[Any] = (),
    decisions: Iterable[str] = (),
    authorizations: Iterable[str] = (),
    blocked: Iterable[str] = (),
    errors: Iterable[str] = (),
    important_paths: Iterable[str] = (),
    next_steps: Iterable[str] = (),
    research_state: str = "",
    memory_refs: Iterable[str] = (),
) -> Recap:
    """以调用方给出的字段构造摘要（测试与程序化压缩使用）。"""

    def to_facts(items: Iterable[Any]) -> list[RecapFact]:
        out = []
        for item in items:
            if isinstance(item, RecapFact):
                out.append(item)
            elif isinstance(item, Mapping):
                out.append(RecapFact(str(item.get("text", "")), item.get("status")))
            else:
                out.append(RecapFact(str(item)))
        return out

    return Recap(
        goal=goal,
        decisions=[str(x) for x in decisions],
        authorizations=[str(x) for x in authorizations],
        completed=to_facts(completed),
        in_progress=to_facts(in_progress),
        blocked=[str(x) for x in blocked],
        errors=[str(x) for x in errors],
        important_paths=[str(x) for x in important_paths],
        next_steps=[str(x) for x in next_steps],
        research_state=research_state,
        memory_refs=[str(x) for x in memory_refs],
    )


#: 要求模型按结构输出的提示词（压缩时使用）。
RECAP_INSTRUCTIONS = """\
请输出结构化摘要，只保留仍然影响后续工作的事实，不要复述原始对话。
必须使用下面这些标题（中英任一即可），没有内容就省略该节：

目标 / goal
已定决策 / decisions
已获授权 / authorizations
已完成 / completed
进行中 / in_progress
阻塞 / blocked
错误 / errors
重要路径 / important_paths
下一步 / next_steps
研究状态 / research_state
记忆引用 / memory_refs

completed 与 in_progress 下的每条必须带状态标记，写在方括号里，取值只能是：
[proposed] [queued] [implemented] [tested] [published] [installed]

**只把已经真实发生的事写进 completed**；计划、待办、尚未执行的想法必须写进
in_progress 并标 [proposed] 或 [queued]。不确定的信息标注为不确定，不要补全或编造。
"""


__all__ = [
    "FIELD_TITLES",
    "RECAP_FIELDS",
    "RECAP_INSTRUCTIONS",
    "UNFINISHED_STATUSES",
    "Recap",
    "RecapFact",
    "RecapStatus",
    "RecapViolation",
    "build_recap",
    "coerce_recap",
    "parse_recap",
]
