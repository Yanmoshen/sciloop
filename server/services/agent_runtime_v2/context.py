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

    :param include_reasoning: 保留兼容参数。推理永远不作为正文消息；真实
        供应商返回的推理仅作为 assistant 的协议字段回传。
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

    visible = [i for i in items if covered_until is None or i.sequence > covered_until]
    # Codex normalizes the prompt before every provider request: one output per
    # call id, with the first durable output winning.  A duplicate output must
    # not replace the result that was actually observed first after a resume.
    results: dict[str | None, Item] = {}
    for item in visible:
        if item.type is ItemType.TOOL_RESULT and item.call_id not in results:
            results[item.call_id] = item
    consumed: set[str] = set()
    response: dict[str, Any] | None = None
    response_key: tuple[Any, Any] | None = None

    def flush() -> None:
        nonlocal response, response_key
        if response is None:
            return
        calls = response.get("tool_calls") or []
        if response.get("content") or calls:
            messages.append(response)
            for call in calls:
                call_id = call["id"]
                result = results.get(call_id)
                if result is not None:
                    messages.append(item_to_message(result))
                else:
                    # An interrupted/failed historical batch must not be
                    # replayed just to repair the provider wire protocol.
                    messages.append({
                        "role": "tool", "tool_call_id": call_id,
                        "content": json.dumps({"status": "interrupted", "error": {
                            "code": "result_unavailable",
                            "message": "此调用没有已记录的执行结果，请勿假定执行成功。",
                        }}, ensure_ascii=False),
                    })
                consumed.add(call_id)
        response = None
        response_key = None

    for item in visible:
        if item.type in {ItemType.REASONING, ItemType.ASSISTANT_TEXT, ItemType.TOOL_CALL}:
            key = (item.turn_id, item.payload.get("response_id"))
            if response is not None and key != response_key:
                flush()
            if response is None:
                response = {"role": "assistant", "content": ""}
                response_key = key
            if item.type is ItemType.REASONING:
                response["reasoning_content"] = response.get("reasoning_content", "") + str(item.payload.get("text", ""))
            elif item.type is ItemType.ASSISTANT_TEXT:
                response["content"] += str(item.payload.get("text", ""))
            else:
                message = item_to_message(item)
                response.setdefault("tool_calls", []).extend(message["tool_calls"])
            continue
        if item.type is ItemType.TOOL_RESULT:
            flush()
            # Match Codex's remove_orphan_outputs: a result whose call is not
            # present in this model window is dropped.  Re-emitting it as a
            # user message changes the speaker and can make the model invent a
            # tool call that never happened after compaction.
            continue
        if item.type not in CONTEXT_ITEM_TYPES:
            continue
        flush()
        message = item_to_message(item)
        if item.type is ItemType.APPROVAL:
            # The append-only Item records the original request, while the
            # approval event projection contains the current decision. Do not
            # tell a resumed model that an already decided request is pending.
            approval = getattr(state, "approvals", {}).get(item.payload.get("approval_id"))
            if approval is not None:
                status = getattr(approval.status, "value", approval.status)
                if status != "pending":
                    message = {
                        "role": "user",
                        "content": (
                            f"[审批] 当前结果：{status}，范围：{approval.decision_scope or 'once'}。"
                            "该请求已结束等待；批准只代表允许执行，是否成功以工具结果为准。"
                        ),
                    }
        if message is not None:
            messages.append(message)
    flush()
    input_texts = {str(i.payload.get("text", "")) for i in visible if i.type is ItemType.USER_INPUT}
    for delivery in getattr(state, "mailbox", []):
        if covered_until is not None and int(delivery.get("sequence", 0)) <= covered_until:
            continue
        content = str(delivery.get("content", ""))
        if content and content not in input_texts:
            messages.append({"role": "user", "content": "[协作消息] " + content})
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
