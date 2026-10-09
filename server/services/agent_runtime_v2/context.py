"""上下文装配：把事件/Item 映射成模型消息（Agent 1 / WP-03）。

两条硬规则：

1. **压缩生效时不得再喂全量历史**。存在生效摘要时，上下文 = 摘要 + 摘要覆盖面之后的 Item。
   原始历史仍完整保留在 events.jsonl 里，只是不进模型请求。
2. **工具结果按 ``call_id`` 配对**。``tool_result`` 消息必须携带 ``tool_call_id``，
   否则模型无法把结果对应回调用。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from contracts.agent_v2.enums import ItemType
from contracts.agent_v2.models import Item

#: 进上下文的 Item 类型（其余类型只用于审计，不喂模型）。
CONTEXT_ITEM_TYPES: frozenset[ItemType] = frozenset(
    {
        ItemType.USER_INPUT,
        ItemType.ASSISTANT_TEXT,
        ItemType.TOOL_CALL,
        ItemType.TOOL_RESULT,
        ItemType.PLAN,
        ItemType.SUBAGENT_RESULT,
        ItemType.APPROVAL,
    }
)


def item_to_message(item: Item) -> dict[str, Any] | None:
    """单个 Item -> 模型消息；不可进上下文的类型返回 None。"""
    payload = item.payload or {}
    itype = item.type

    if itype is ItemType.USER_INPUT:
        return {"role": "user", "content": str(payload.get("text", ""))}

    if itype is ItemType.ASSISTANT_TEXT:
        text = str(payload.get("text", ""))
        if not text:
            return None
        return {"role": "assistant", "content": text}

    if itype is ItemType.TOOL_CALL:
        name = str(payload.get("name", ""))
        arguments = payload.get("arguments") or {}
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": item.call_id or "",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        }

    if itype is ItemType.TOOL_RESULT:
        status = str(payload.get("status", ""))
        body: dict[str, Any] = {"status": status}
        if payload.get("output") is not None:
            body["output"] = payload["output"]
        if payload.get("error") is not None:
            body["error"] = payload["error"]
        return {
            "role": "tool",
            "tool_call_id": item.call_id or "",
            "content": json.dumps(body, ensure_ascii=False),
        }

    if itype is ItemType.PLAN:
        steps = payload.get("steps") or payload.get("text") or ""
        return {"role": "assistant", "content": f"[计划] {steps}"}

    if itype is ItemType.SUBAGENT_RESULT:
        summary = payload.get("summary") or payload.get("text") or ""
        child = payload.get("child_thread_id") or item.subagent_thread_id or ""
        status = payload.get("status", "completed")
        return {
            "role": "user",
            "content": f"[子 Agent {child} 结果：{status}] {summary}",
        }

    if itype is ItemType.APPROVAL:
        action = payload.get("action") or {}
        decision = payload.get("decision")
        label = f"审批结果：{decision}" if decision else "已提交审批请求，等待研究者决定"
        return {"role": "user", "content": f"[审批] {label} {json.dumps(action, ensure_ascii=False)}"}

    return None


def compaction_covered_until(state: Any) -> int | None:
    """当前生效摘要覆盖到的事件序号；无摘要返回 None。"""
    active = state.active_compaction() if hasattr(state, "active_compaction") else None
    if not active:
        return None
    covered = active.get("covered_until")
    return int(covered) if covered is not None else None


def build_context(
    state: Any,
    *,
    system_prompt: str | None = None,
    include_reasoning: bool = False,
) -> list[dict[str, Any]]:
    """从线程状态装配模型上下文。

    :param include_reasoning: 默认不把推理片段喂回模型（避免污染上下文，
        也与主流供应商的用法一致）。
    """
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    covered_until = compaction_covered_until(state)
    items: Iterable[Item] = (state.items[i] for i in state.item_order)

    if covered_until is not None:
        active = state.active_compaction() or {}
        summary_text = str(active.get("summary") or "")
        messages.append(
            {
                "role": "system",
                "content": (
                    f"[压缩摘要，覆盖事件 1..{covered_until}，原始历史仍可审计]\n{summary_text}"
                ),
            }
        )

    for item in items:
        if covered_until is not None and item.sequence <= covered_until:
            continue
        if not include_reasoning and item.type is ItemType.REASONING:
            continue
        if item.type not in CONTEXT_ITEM_TYPES:
            continue
        message = item_to_message(item)
        if message is not None:
            messages.append(message)
    return messages


def estimate_tokens(messages: Iterable[dict[str, Any]], *, per_message_overhead: int = 4) -> int:
    """粗略 token 估算（约 4 字符 1 token）。

    只用于「是否触发压缩」的判定，不用于计费；真实用量以 ``model/usage`` 事件为准。
    """
    total = 0
    for message in messages:
        total += per_message_overhead
        content = message.get("content")
        if isinstance(content, str):
            total += max(1, len(content) // 4)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                total += max(1, len(json.dumps(call, ensure_ascii=False)) // 4)
    return total


def transcript(messages: Iterable[dict[str, Any]], *, limit_chars: int = 20000) -> str:
    """把消息序列渲染成可压缩的文本抄本。"""
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if isinstance(content, str) and content:
            lines.append(f"{role}: {content}")
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                fn = (call or {}).get("function") or {}
                lines.append(
                    f"tool_call {fn.get('name')}: {fn.get('arguments')} (id={(call or {}).get('id')})"
                )
    text = "\n".join(lines)
    return text[-limit_chars:] if len(text) > limit_chars else text


__all__ = [
    "CONTEXT_ITEM_TYPES",
    "item_to_message",
    "build_context",
    "compaction_covered_until",
    "estimate_tokens",
    "transcript",
]
