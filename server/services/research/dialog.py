# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话里的三条非通用分支：**引导词 / 节点执行 / 本地查询**。

背景（为什么有这个文件）
------------------------
之前首页对话只会走通用助手提示词（``chat.py: REPLY_SYSTEM``），
所以用户在对话里说「开始文献调研」拿到的是通用反问清单——**对话没有接上编排层**。
这里把三条分支补上，使**对话本身就是 agent 的入口**：

- ``guide``  模糊引导词（怎么开始 / 研究 / 文献调研…）且本对话还没开链
             → 输出引导词 + 三个可点选项（文献调研 / 从 idea 开始 / 普通对话）
- ``node``   明确的执行意图（「开始文献调研」）
             → 直接跑节点，过程以紧凑系统行逐条回到对话，结论是确定性文本
- ``query``  查询意图（查论文 / 项目 / 产出 / 研究链）
             → 程序做确定性只读查询，结果以卡片 + 一句话回到对话

三条分支都**不额外花模型调用**（节点执行本身就是调用），因此不会因为「措辞」再花一次钱，
也不会出现「模型把结果复述错」的情况。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from services import conversations as conversations_service
from services.research import intent as intent_mod
from services.research import lookup, messages, orchestrator

logger = logging.getLogger("sciloop.research.dialog")

__all__ = [
    "PLAIN_CHAT_TRIGGERS",
    "has_chain",
    "is_plain_chat_reply",
    "route",
    "stream_guide",
    "stream_node",
    "stream_plain_chat",
    "stream_query",
]

#: 选了「只是普通对话」时前端会发过来的原句
PLAIN_CHAT_TRIGGERS: tuple[str, ...] = ("只是普通对话", "普通对话", "就聊聊", "随便聊聊")


def is_plain_chat_reply(text: str) -> bool:
    """用户是否选择了「只是普通对话」。"""

    compact = (text or "").strip()
    return any(compact == trigger or compact == f"选{trigger}" for trigger in PLAIN_CHAT_TRIGGERS)


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _label_of(node: str) -> str:
    from services.research import graph

    return graph.NODE_LABELS.get(node, node)


def _implemented(node: str) -> bool:
    """该节点是否真的实装了（决定自动连续跑能不能往下走）。"""

    from services.research import graph

    return graph.is_implemented(node)


def _research_text(text: str, conversation: dict[str, Any]) -> str:
    """把「命令」还原成「研究问题」，给检索与提示词用。

    「开始文献调研」里的命令词不是研究内容，原样拿去检索命中一定是 0。
    这里先剥掉命令词；剥完什么都不剩时，退回到**对话里之前的用户发言**
    （它们才是这个话题的上下文），最后才退到会话标题。
    """

    remainder = intent_mod.strip_command_words(text)
    if len(remainder) >= 4:
        return remainder

    previous = [
        str(turn.get("content") or "")
        for turn in (conversation.get("turns") or [])
        if turn.get("role") == "user"
    ]
    joined = " ".join(previous[-3:]).strip()
    if len(intent_mod.strip_command_words(joined)) >= 4:
        return intent_mod.strip_command_words(joined)

    title = str(conversation.get("title") or "").strip()
    # 标题也要先剥命令词：新会话的标题就是用户那句命令（「开始文献调研」），
    # 直接拿它当研究问题会让「缺研究问题」的检查失效，然后白跑 3 次重试。
    cleaned_title = intent_mod.strip_command_words(title)
    return cleaned_title if len(cleaned_title) >= 4 else ""


async def has_chain(conversation_id: str) -> bool:
    """本对话是否已经开了研究链（决定模糊引导词要不要出引导语）。"""

    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover
        return False
    try:
        async with AsyncSessionLocal() as session:
            await orchestrator.chain_state(session, conversation_id=conversation_id)
            rows = await _node_run_count(session, conversation_id)
        return rows > 0
    except Exception as exc:  # noqa: BLE001 - 读不到就按「还没开链」处理，不阻塞
        logger.warning("读取研究链失败 conversation=%s：%s", conversation_id, exc)
        return False


async def _node_run_count(session: Any, conversation_id: str) -> int:
    from sqlalchemy import func, select

    from db.models.research import ResearchNodeRun

    stmt = select(func.count()).select_from(ResearchNodeRun).where(
        ResearchNodeRun.conversation_id == conversation_id
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


async def route(text: str, conversation_id: str, *, force_plain: bool = False) -> intent_mod.Intent:
    """判定这句话该走哪条分支。

    ``force_plain`` = 本对话已声明为「普通对话」→ 只有**明确的执行意图**能把它拉回研究模式，
    其余一律走通用回答（不反复问用户要不要开研究链）。
    """

    chained = await has_chain(conversation_id)
    result = intent_mod.resolve_intent(text, has_chain=chained)
    if force_plain and result.kind != "node":
        return intent_mod.Intent(
            kind="chat",
            keyword=result.keyword,
            reason="本对话已声明为普通对话；要开研究流程请直接说「开始文献调研」",
        )
    return result


# --------------------------------------------------------------------------- #
# ⓪ 普通对话（用户明确选了「只是普通对话」）
# --------------------------------------------------------------------------- #
async def stream_plain_chat(
    *, conversation_id: str, scope: dict[str, Any]
) -> AsyncIterator[str]:
    """确认「本对话为普通对话」，并**告知如何切回研究模式**。

    要求里明确：选了普通对话要记住，且必须告诉用户**可以切回、以及怎么切**。
    """

    conversations_service.set_fields(conversation_id, plain_chat=True)
    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": scope.get("project_id"),
            "routing": {"kind": "plain_chat", "mode": "plain"},
        },
    )
    yield _sse("delta", {"text": messages.PLAIN_CHAT_TEXT})
    conversations_service.append_turns(
        scope["conversation"],
        [
            {"role": "user", "content": scope["text"]},
            {"role": "assistant", "content": messages.PLAIN_CHAT_TEXT, "routing": "plain_chat"},
        ],
    )
    yield _sse("done", {"conversation_id": conversation_id, "routing": "plain_chat"})


# --------------------------------------------------------------------------- #
# ① 引导词
# --------------------------------------------------------------------------- #
async def stream_guide(
    *,
    conversation_id: str,
    scope: dict[str, Any],
) -> AsyncIterator[str]:
    """输出引导词 + 三个可点选项，并把这轮如实落进对话。"""

    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": scope.get("project_id"),
            "routing": {"kind": "guide", "reason": scope.get("reason", "")},
        },
    )
    yield _sse("delta", {"text": messages.GUIDE_TEXT})
    yield _sse("blocks", {"blocks": messages.guide_blocks()})

    conversations_service.append_turns(
        scope["conversation"],
        [
            {"role": "user", "content": scope["text"]},
            {
                "role": "assistant",
                "content": messages.GUIDE_TEXT,
                "blocks": messages.guide_blocks(),
                "routing": "guide",
                "duration_ms": 0,
            },
        ],
    )
    yield _sse("done", {"conversation_id": conversation_id, "routing": "guide"})


# --------------------------------------------------------------------------- #
# ② 节点执行
# --------------------------------------------------------------------------- #
async def stream_node(
    *,
    conversation_id: str,
    text: str,
    scope: dict[str, Any],
    routing: intent_mod.Intent,
) -> AsyncIterator[str]:
    """执行一个研究节点：过程逐条回对话，结论用确定性文本。"""

    from db.session import AsyncSessionLocal

    project_id = scope.get("project_id")
    label = _label_of(routing.node or "")
    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": project_id,
            "routing": {**routing.to_dict(), "source": "program"},
        },
    )

    events: list[tuple[str, dict[str, Any]]] = []
    refs: dict[str, Any] = {}
    status = "failed"
    total_cost = 0.0
    persisted_rows: list[dict[str, Any]] = []

    if AsyncSessionLocal is None:  # pragma: no cover
        yield _sse("error", {"code": "db_unavailable", "message": "数据库会话不可用"})
        return

    async for event, data in orchestrator.run_node(
        AsyncSessionLocal,
        conversation_id=conversation_id,
        node=routing.node,
        text=_research_text(text, scope["conversation"]),
        project_id=project_id,
    ):
        events.append((event, data))
        if event == "done":
            status = str(data.get("status") or "failed")
            refs = data.get("refs") or {}
            total_cost = float(data.get("cost_usd") or 0)
            # 系统行：结论本身随后单独发，这里不重复
        elif event in ("node", "attempt", "notice", "validation", "revert", "migrated",
                       "waiting_human", "error"):
            row = _system_row_for(event, data)
            if row is not None:
                persisted_rows.append(row)
                yield _sse("row", {"row": row})

    summary = _conclusion(label=label, status=status, events=events, refs=refs)
    next_node = next((d.get("next_node") for e, d in events if e == "done"), None)

    if status == "done" and next_node and next_node != "end" and not _implemented(next_node):
        # **不假装往下走**：后面几个节点首版未实装，自动连续跑必须在这里停住并说明，
        # 否则它们会依次「通过」，等于伪造「实验做完了、论文写好了」。
        summary += (
            f"\n\n下一节点是「{_label_of(next_node)}」，**首版尚未实装**："
            "它既不会校验产出，也不会真的执行实验。我不会替你把它标记成完成。"
        )
        yield _sse(
            "row",
            {
                "row": messages.system_row(
                    "stopped",
                    f"「{_label_of(next_node)}」尚未实装，已停止继续推进",
                    tone="warn",
                )
            },
        )
        persisted_rows.append(
            messages.system_row(
                "stopped", f"「{_label_of(next_node)}」尚未实装，已停止继续推进", tone="warn"
            )
        )

    yield _sse("delta", {"text": summary})

    conversations_service.append_turns(
        scope["conversation"],
        [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": summary,
                "routing": "node",
                "node": routing.node,
                "node_status": status,
                "cost_usd": total_cost,
                "duration_ms": 0,
                # 过程行也要落盘：否则刷新后节点过程整段消失，只剩一句结论
                "rows": persisted_rows,
                "interrupted": status == "failed",
            },
        ],
    )
    yield _sse(
        "done",
        {
            "conversation_id": conversation_id,
            "routing": "node",
            "node": routing.node,
            "node_status": status,
            "cost_usd": total_cost,
            "next_node": next_node,
            "next_implemented": bool(next_node) and _implemented(next_node),
        },
    )


def _system_row_for(event: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """把节点事件转成一条紧凑系统行（面向研究者，不含内部字段名）。"""

    if event == "node":
        return messages.system_row(
            "entered", f"{data.get('node_label')}（第 {data.get('entry_index')} 次进入）", tone="info"
        )
    if event == "attempt":
        hits = data.get("library_hits")
        return messages.system_row(
            "attempt",
            f"第 {data.get('attempt')}/{data.get('max_attempts')} 次尝试"
            + (f"，命中论文 {hits} 篇" if hits is not None else ""),
            tone="idle",
        )
    if event == "validation":
        items = data.get("items") or []
        detail = "；".join(str(item.get("message", "")) for item in items[:3])
        return messages.system_row(
            "validation",
            f"第 {data.get('attempt')}/{data.get('max_attempts')} 次被驳回：{detail}",
            tone="err",
        )
    if event == "notice":
        return messages.system_row("notice", str(data.get("message") or ""), tone="warn")
    if event == "revert":
        return messages.system_row(
            "revert",
            f"{data.get('from_label')} → {data.get('to_label')}：{str(data.get('reason') or '')[:160]}",
            tone="info",
        )
    if event == "migrated":
        return messages.system_row(
            "migrated", f"{data.get('from_label')} → {data.get('to_label')}", tone="ok"
        )
    if event == "waiting_human":
        return messages.system_row(
            "waiting_human", str(data.get("message") or "已转入人工介入"), tone="warn"
        )
    if event == "error":
        return messages.system_row(
            "stopped", str(data.get("message") or "执行失败"), tone="err"
        )
    return None


def _conclusion(
    *, label: str, status: str, events: list[tuple[str, dict[str, Any]]], refs: dict[str, Any]
) -> str:
    """结论文本。优先级：**缺输入的说明** > 失败原因 > 常规确定性总结。

    为什么把「缺输入」放第一位：这种情况下真正该告诉用户的是「我需要一个研究问题」，
    而不是「多次修复仍未通过校验」——后者会把**输入缺失**说成**模型修不好**，属于误导。
    """

    for event, data in reversed(events):
        if event == "waiting_human" and data.get("needs_input"):
            return str(data.get("message") or "")
    if status == "failed":
        return _failure_summary(label, events)
    return messages.node_summary(
        node_label=label, status=status, events=events, refs=refs
    )


def _failure_summary(label: str, events: list[tuple[str, dict[str, Any]]]) -> str:
    error = next((d for e, d in reversed(events) if e == "error"), None)
    reason = str((error or {}).get("message") or "未知原因")
    return f"「{label}」执行失败：{reason}"


# --------------------------------------------------------------------------- #
# ③ 本地查询
# --------------------------------------------------------------------------- #
async def stream_query(
    *,
    conversation_id: str,
    text: str,
    scope: dict[str, Any],
    routing: intent_mod.Intent,
) -> AsyncIterator[str]:
    """确定性只读查询：结果卡片 + 一句话（**不再花一次模型调用**）。"""

    from db.session import AsyncSessionLocal

    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": scope.get("project_id"),
            "routing": {**routing.to_dict(), "source": "program"},
        },
    )

    if AsyncSessionLocal is None:  # pragma: no cover
        yield _sse("error", {"code": "db_unavailable", "message": "数据库会话不可用"})
        return

    try:
        async with AsyncSessionLocal() as session:
            result = await lookup.run_lookup(
                session,
                topic=routing.topic or "papers",
                text=text,
                conversation_id=conversation_id,
            )
    except Exception as exc:  # noqa: BLE001 - 查询失败如实说，不编结果
        logger.exception("本地查询失败 conversation=%s", conversation_id)
        message = f"查询没能完成：{exc}"
        yield _sse("error", {"code": "lookup_failed", "message": message[:300]})
        conversations_service.append_turns(
            scope["conversation"],
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": message, "routing": "query", "interrupted": True},
            ],
        )
        return

    yield _sse("delta", {"text": result.summary})
    yield _sse("blocks", {"blocks": [messages.result_card(result)]})

    conversations_service.append_turns(
        scope["conversation"],
        [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": result.summary,
                "routing": "query",
                "blocks": [messages.result_card(result)],
                "duration_ms": 0,
            },
        ],
    )
    yield _sse("done", {"conversation_id": conversation_id, "routing": "query"})
