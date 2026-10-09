"""三类上下文区块的构造：runtime facts / task context / research context。

铁律（计划书 WP-03 与 §7.2）：

- 只写**事实**：权限只写「已获授权的范围」这类事实，绝不写审批凭据；
- **清洗**：内部术语、审批 token、内部思考、循环次数、重试细节、调试状态一律过滤掉；
- research context **只在 research_confirmed 为真时**出现；
- 记忆进上下文前必须按作用域与长度过滤。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .blocks import BlockKind, PromptBlock

#: 绝不允许出现在提示词里的键（小写比较）。命中即丢弃，而不是脱敏——宁可少给。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "owner_token",
        "approval_token",
        "public_demo",
        "token",
        "secret",
        "api_key",
        "authorization",
        "loop_index",
        "iteration",
        "loop_count",
        "retry_count",
        "retries",
        "attempt",
        "debug",
        "debug_state",
        "internal",
        "internal_notes",
        "raw_error",
        "stack",
        "traceback",
        "call_id",
        "idempotency_key",
        "sequence",
    }
)

#: 值里出现这些片段即视为内部信息（例如误把事件流片段塞进来）。
FORBIDDEN_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[a-z]{2,4}_[0-9a-f]{23}\b"),  # 稳定 ID（th_/tu_/it_/call_/ev_…）
    re.compile(r"\btraceback\b", re.I),
    re.compile(r"\bstack trace\b", re.I),
    re.compile(r"\bowner_token\b", re.I),
)

#: 任务上下文里允许保留的键白名单（其余一律不进提示词）。
TASK_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "goal",
        "project",
        "workspace",
        "approved",          # 已批准事项的**描述**（不含凭据）
        "authorizations",
        "todos",
        "blocked",
        "errors",            # 面向用户的错误描述
        "artifacts",
        "notes",
    }
)

#: 单条记忆进上下文的最大字符数（超出截断，不整体丢弃）。
MEMORY_SNIPPET_LIMIT = 240


def _redact_value(value: str) -> str | None:
    for pattern in FORBIDDEN_VALUE_PATTERNS:
        if pattern.search(value):
            return None
    return value


def scrub_facts(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """过滤事实字典：丢弃禁用键、脱掉含内部标识的值、递归处理容器。"""
    if not raw:
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if str(key).strip().lower() in FORBIDDEN_KEYS:
            continue
        cleaned = _scrub_value(value)
        if cleaned is None:
            continue
        out[str(key)] = cleaned
    return out


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_value(value)
    if isinstance(value, Mapping):
        nested = scrub_facts(value)
        return nested or None
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            cleaned = _scrub_value(item)
            if cleaned is not None and cleaned != {}:
                items.append(cleaned)
        return items or None
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return None


def _bullets(lines: Iterable[str]) -> str:
    materialised = [f"- {line}" for line in lines if str(line).strip()]
    return "\n".join(materialised)


# --------------------------------------------------------------------------------------
# runtime facts
# --------------------------------------------------------------------------------------
def build_runtime_facts(
    *,
    model: str | None = None,
    workspace: str | None = None,
    tools: Sequence[Mapping[str, Any]] = (),
    permission_facts: Mapping[str, Any] | None = None,
    turn_status: str | None = None,
    now: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> PromptBlock:
    """运行环境事实区块。

    ``permission_facts`` 只应包含「当前对话已获授权的范围」这类**事实**；
    真实权限由 Agent 2 的执行器强制，提示词不构成授权。
    """
    lines: list[str] = []
    if model:
        lines.append(f"当前模型：{model}")
    if workspace:
        lines.append(f"工作目录：{workspace}")
    if turn_status:
        lines.append(f"当前回合状态：{turn_status}")
    if tools:
        readable = ", ".join(
            f"{tool.get('name')}({'只读' if tool.get('kind') == 'read_only' else '有副作用'})"
            for tool in tools
            if tool.get("name")
        )
        if readable:
            lines.append(f"可用工具：{readable}")
    facts = scrub_facts(permission_facts)
    if facts:
        lines.append("权限事实：" + "；".join(f"{k}={v}" for k, v in facts.items()))
    if now:
        lines.append(f"当前时间：{now}")
    lines.extend(_plain_lines(scrub_facts(extra)))

    return PromptBlock(
        kind=BlockKind.RUNTIME_FACTS,
        title="运行环境",
        body=_bullets(lines),
        facts={
            "model": model,
            "workspace": workspace,
            "tool_count": len(tools),
            "permission_facts": facts,
        },
    )


def _plain_lines(mapping: Mapping[str, Any]) -> list[str]:
    out = []
    for key, value in mapping.items():
        if isinstance(value, (list, tuple)):
            rendered = "、".join(str(v) for v in value)
        else:
            rendered = str(value)
        if rendered.strip():
            out.append(f"{key}：{rendered}")
    return out


# --------------------------------------------------------------------------------------
# task context
# --------------------------------------------------------------------------------------
def select_memories(
    records: Iterable[Any],
    *,
    scopes: Sequence[str] = (),
    max_items: int = 8,
    snippet_limit: int = MEMORY_SNIPPET_LIMIT,
) -> list[str]:
    """按作用域与长度过滤记忆，返回可直接进提示词的片段。

    - 只保留指定作用域（空表示不过滤）且未删除的记录；
    - 超长内容**截断**而不是丢弃（信息仍有价值）；
    - 超过 ``max_items`` 直接截断列表（防止记忆把上下文挤爆）。
    """
    wanted = {str(s) for s in scopes}
    out: list[str] = []
    for record in records:
        scope = getattr(record, "scope", None)
        scope_value = scope.value if hasattr(scope, "value") else str(scope)
        if wanted and scope_value not in wanted:
            continue
        if getattr(record, "deleted", False):
            continue
        text = _redact_value(str(getattr(record, "text", "")).strip())
        if not text:
            continue
        out.append(text[:snippet_limit] + ("…" if len(text) > snippet_limit else ""))
        if len(out) >= max_items:
            break
    return out


def build_task_context(
    *,
    goal: str | None = None,
    project: str | None = None,
    approved: Sequence[str] = (),
    todos: Sequence[str] = (),
    blocked: Sequence[str] = (),
    errors: Sequence[str] = (),
    memories: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> PromptBlock:
    """任务上下文区块：目标、项目、已批准事项、待办、阻塞、错误、必要记忆。"""
    lines: list[str] = []
    if goal:
        lines.append(f"目标：{goal}")
    if project:
        lines.append(f"项目：{project}")
    if approved:
        lines.append("已获批准：")
        lines.append(_bullets(approved))
    if todos:
        lines.append("待办：")
        lines.append(_bullets(todos))
    if blocked:
        lines.append("阻塞：")
        lines.append(_bullets(blocked))
    if errors:
        lines.append("已知问题：")
        lines.append(_bullets(errors))
    if memories:
        lines.append("相关记忆：")
        lines.append(_bullets(memories))
    lines.extend(_plain_lines(scrub_facts(extra)))

    facts = scrub_facts(
        {
            "goal": goal,
            "project": project,
            "approved": list(approved),
            "todos": list(todos),
            "blocked": list(blocked),
            "errors": list(errors),
            "memory_count": len(list(memories)),
        }
    )
    facts = {k: v for k, v in facts.items() if k in TASK_CONTEXT_KEYS or k == "memory_count"}
    return PromptBlock(
        kind=BlockKind.TASK_CONTEXT,
        title="任务上下文",
        body="\n".join(part for part in lines if part),
        facts=facts,
    )


# --------------------------------------------------------------------------------------
# research context
# --------------------------------------------------------------------------------------
RESEARCH_RULES = """\
当前处于已确认的科研任务中，遵循以下要求：

- 只使用真实可核验的来源，逐条给出可追溯标识（标题、作者、年份、DOI/arXiv 号或 URL）；
- 引用必须与结论一一对应，不要把没读过的文献写进引用；
- 来源无法访问或信息不完整时，明确标注为不确定，并说明缺什么；
- 绝不编造论文、作者、期刊、链接、数字或实验结果；
- 区分「已经证实」与「推测」；推测必须显式标注。
"""


def build_research_context(
    *,
    research_confirmed: bool,
    phase: str | None = None,
    question: str | None = None,
    sources: Sequence[str] = (),
) -> PromptBlock:
    """研究上下文区块。

    **只有 ``research_confirmed`` 为真时才产出内容**——未确认的回合里这一块必须为空，
    否则等于在用户还没同意前就把模型推向检索（违反 §7.2「研究意图先确认」）。
    """
    if not research_confirmed:
        return PromptBlock(
            kind=BlockKind.RESEARCH_CONTEXT,
            title="科研任务",
            body="",
            facts={"research_confirmed": False},
        )
    lines = [RESEARCH_RULES.strip()]
    if phase:
        lines.append(f"当前阶段：{phase}")
    if question:
        lines.append(f"研究问题：{question}")
    if sources:
        lines.append("已知来源：")
        lines.append(_bullets(sources))
    return PromptBlock(
        kind=BlockKind.RESEARCH_CONTEXT,
        title="科研任务",
        body="\n\n".join(lines),
        facts={
            "research_confirmed": True,
            "phase": phase,
            "question": question,
            "source_count": len(list(sources)),
        },
    )


__all__ = [
    "FORBIDDEN_KEYS",
    "FORBIDDEN_VALUE_PATTERNS",
    "MEMORY_SNIPPET_LIMIT",
    "RESEARCH_RULES",
    "build_research_context",
    "build_runtime_facts",
    "build_task_context",
    "scrub_facts",
    "select_memories",
]
