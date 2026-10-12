# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话里的三条非通用分支：**主题澄清 / 节点执行 / 本地查询**。

背景（为什么有这个文件）
------------------------
之前首页对话只会走通用助手提示词（``chat.py: REPLY_SYSTEM``），
所以用户在对话里说「开始文献调研」拿到的是通用反问清单——**对话没有接上编排层**。
这里把三条分支补上，使**对话本身就是 agent 的入口**：

- ``guide``  明确启动但缺少主题 → 只询问一个具体主题问题
- ``node``   明确的执行意图（「开始文献调研」）
             → 直接跑节点，过程以紧凑系统行逐条回到对话，结论是确定性文本
- ``query``  查询意图（查论文 / 项目 / 产出 / 研究链）
             → 程序做确定性只读查询，结果以卡片 + 一句话回到对话

三条分支都**不额外花模型调用**（节点执行本身就是调用），因此不会因为「措辞」再花一次钱，
也不会出现「模型把结果复述错」的情况。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from services import conversations as conversations_service
from services.research import intent as intent_mod
from services.research import lookup, messages, orchestrator, phrasing

logger = logging.getLogger("sciloop.research.dialog")

__all__ = [
    "PLAIN_CHAT_TRIGGERS",
    "CONTINUE_WORDS",
    "chain_snapshot",
    "has_chain",
    "is_continue_command",
    "is_plain_chat_reply",
    "route",
    "request_persisted_node_interrupt",
    "request_node_interrupt",
    "stream_guide",
    "stream_node",
    "stream_plain_chat",
    "stream_query",
]

#: 选了「只是普通对话」时前端会发过来的原句
PLAIN_CHAT_TRIGGERS: tuple[str, ...] = ("只是普通对话", "普通对话", "就聊聊", "随便聊聊")


#: 「继续」类的说法 —— 已开链时表示**重入当前这一站**（再给它一次机会）。
#: 为什么必须补：转人工时给的可点选项里就有「继续」，而动作词表里只有英文 `continue`，
#: 实测点「继续」不会触发任何动作 —— 点了没反应的按钮比没有按钮更糟（2026-09-26）。
CONTINUE_WORDS: tuple[str, ...] = ("继续", "下一步", "接着跑", "往下走", "接着做")


class ResearchInterrupted(Exception):
    """Raised when the owner explicitly stops an active research turn."""


_ACTIVE_RESEARCH: dict[str, asyncio.Event] = {}


def request_node_interrupt(conversation_id: str) -> bool:
    event = _ACTIVE_RESEARCH.get(str(conversation_id))
    if event is None:
        return False
    event.set()
    return True


async def request_persisted_node_interrupt(conversation_id: str) -> bool:
    """Persist a stop request when the active stream belongs to another worker."""
    from db.session import AsyncSessionLocal

    return await orchestrator.request_persisted_interrupt(
        AsyncSessionLocal, str(conversation_id)
    )


def _node_deadline_seconds() -> float | None:
    """Return an optional subscription guard for one research-node turn.

    A turn worker owns research execution; the SSE connection is only a
    subscriber.  Therefore no total wall-clock deadline is applied by
    default.  Deployments and focused tests may opt into a bounded
    subscription with ``SCILOOP_RESEARCH_DEADLINE_SECONDS``; the orchestrator
    still owns its independent provider/tool cancellation budgets.
    """

    raw = os.getenv("SCILOOP_RESEARCH_DEADLINE_SECONDS")
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(max(value, 5.0), 3600.0)


def _conclusion_timeout_seconds() -> float:
    """Bound the optional user-facing conclusion call independently."""
    raw = os.getenv("SCILOOP_RESEARCH_CONCLUSION_TIMEOUT_SECONDS", "30")
    try:
        return min(max(float(raw), 1.0), 120.0)
    except (TypeError, ValueError):
        return 30.0


async def _iter_with_deadline(
    source: AsyncIterator[tuple[str, dict[str, Any]]], *, seconds: float | None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Forward events, optionally applying an explicit subscription guard."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds if seconds is not None else None
    iterator = source.__aiter__()
    while True:
        remaining = deadline - loop.time() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise TimeoutError("研究节点订阅超过最大时长")
        next_task = asyncio.create_task(iterator.__anext__())
        cancel_task = (
            asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        )
        try:
            waiters = {next_task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task is not None and cancel_task in done and cancel_event and cancel_event.is_set():
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_task
                raise ResearchInterrupted("研究者请求停止当前研究回合")
            if next_task not in done:
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_task
                raise TimeoutError("研究节点订阅超过最大时长")
            try:
                item = next_task.result()
            except StopAsyncIteration:
                return
            yield item
        finally:
            if not next_task.done():
                next_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await next_task
            if cancel_task is not None:
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

def is_continue_command(text: str) -> bool:
    """是不是「继续往下走」这类说法（允许少量修饰）。"""

    compact = (text or "").strip()
    if not compact or len(compact) > 12:
        return False
    return any(word in compact for word in CONTINUE_WORDS)


# 邀请后的确认必须是明确的短确认。把任意下一条消息都当成同意，会让
# 用户继续追问时意外启动文献调研并产生长时间工具调用。
_EXPLICIT_CONFIRMATION_WORDS: tuple[str, ...] = (
    "开始吧",
    "可以开始",
    "同意开始",
    "同意",
    "确认开始",
    "开始研究链",
    "继续研究",
    "好，开始",
    "好的，开始",
)
_EXPLICIT_CONFIRMATION_DENIALS: tuple[str, ...] = (
    "不同意",
    "不要开始",
    "不开始",
    "不想开始",
    "暂不开始",
    "先不开始",
    "无需开始",
)


def _is_explicit_confirmation(text: str) -> bool:
    """判断用户是否明确确认待启动的研究范围。"""

    compact = re.sub(r"[\s，。！？、,.!?~]+", "", (text or "").strip())
    if not compact or len(compact) > 16:
        return False
    if any(denial in compact for denial in _EXPLICIT_CONFIRMATION_DENIALS):
        return False
    return any(
        re.sub(r"[\s，。！？、,.!?~]+", "", word) in compact
        for word in _EXPLICIT_CONFIRMATION_WORDS
    )


def _explicit_requested_node(text: str) -> tuple[str, str] | None:
    """Select the first stage mentioned in a multi-stage natural request."""
    raw = (text or "").lower()
    matches = [
        (position, -len(alias), node, alias)
        for alias, node in intent_mod.NODE_ALIASES.items()
        if (position := raw.find(alias.lower())) >= 0
    ]
    if not matches:
        return None
    _, _, node, alias = min(matches)
    return node, alias


async def chain_snapshot(conversation_id: str) -> dict[str, Any]:
    """本对话研究链的**事实快照**：有没有开链、当前在哪一站、第几步。

    为什么要有它：对话通道（`api/v1/chat.py`）是**文件型**的、拿不到 DB session，
    而模型现在必须在**回复开头**点明"现在处于哪个节点"、并据此决定要不要发邀请 ——
    它需要真相，否则只能猜（实测就猜错过：把"还没挂链"当成"已经在跑"，
    于是凭空做起了实验准备）。

    与 `has_chain` 同一套做法：自己开会话去读，**读不到一律按"还没开链"处理，绝不阻塞对话**。
    """

    from services.research import graph

    order = list(graph.NODE_ORDER)
    empty: dict[str, Any] = {
        "chained": False,
        "node": None,
        "label": None,
        "index": 0,
        "total": len(order),
    }

    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 未配库时不影响对话
        return empty
    try:
        async with AsyncSessionLocal() as session:
            if await _node_run_count(session, conversation_id) <= 0:
                return empty
            node = await orchestrator.current_node(session, conversation_id=conversation_id)
    except Exception as exc:  # noqa: BLE001 - 读不到就当"还没开链"，不影响这一轮对话
        logger.warning("读研究链快照失败 conversation=%s：%s", conversation_id, exc)
        return empty

    index = order.index(node) + 1 if node in order else 0
    return {
        "chained": True,
        "node": node,
        "label": graph.NODE_LABELS.get(node or "", node),
        "index": index,
        "total": len(order),
    }


async def _active_research_run(conversation_id: str) -> tuple[str, str] | None:
    """Return the durable node/status for the current resumable run.

    The chain snapshot intentionally exposes only navigation facts.  Routing
    needs the run lifecycle as well so a natural-language reply can steer a
    waiting turn instead of creating a new guide turn.
    """

    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover
        return None
    try:
        async with AsyncSessionLocal() as session:
            node = await orchestrator.current_node(session, conversation_id=conversation_id)
            if not node:
                return None
            latest = await orchestrator._latest_run(  # type: ignore[attr-defined]
                session, conversation_id=conversation_id, node=node
            )
            if not latest:
                return None
            status = str(latest.get("status") or "")
            # A disconnected SSE subscriber is intentionally settled as
            # ``pending``.  It is still the same resumable research turn;
            # treating it as inactive makes the next natural-language
            # confirmation start a fresh guide turn and loses the context.
            if status not in {"running", "waiting_human", "pending"}:
                return None
            return node, status
    except Exception as exc:  # noqa: BLE001 - routing must never block chat
        logger.warning("读取活动研究回合失败 conversation=%s：%s", conversation_id, exc)
        return None


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

    confirmed_scope = str(conversation.get("research_confirmed_scope") or "")
    if confirmed_scope and (_is_explicit_confirmation(text) or re.search(
        r"现在可以(?:搜索|检索|开始|执行)|(?:确认|同意)开始|(?:go ahead|start now)",
        text, re.IGNORECASE,
    )):
        scope_text = intent_mod.strip_command_words(confirmed_scope)
        if len(scope_text) >= 4:
            return scope_text
    remainder = intent_mod.strip_command_words(text)
    if len(remainder) >= 4:
        return remainder

    noise = re.compile(
        r"^(?:你好|您好|嗨|hello|hi|怎么开始|你是谁|谢谢|感谢|只是普通对话|普通对话|"
        r"为什么会有.*入口|文献调研怎么做|研究怎么做|我想问问你能做什么)[!！。？?，,\s]*$",
        re.IGNORECASE,
    )
    for turn in reversed(conversation.get("turns") or []):
        if turn.get("role") != "user":
            continue
        candidate = str(turn.get("content") or "").strip()
        if not candidate or noise.match(candidate):
            continue
        cleaned = intent_mod.strip_command_words(candidate)
        if len(cleaned) >= 4 and not noise.match(cleaned):
            return cleaned

    title = str(conversation.get("title") or "").strip()
    # 标题也要先剥命令词：新会话的标题就是用户那句命令（「开始文献调研」），
    # 直接拿它当研究问题会让「缺研究问题」的检查失效，然后白跑 3 次重试。
    cleaned_title = intent_mod.strip_command_words(title)
    return cleaned_title if len(cleaned_title) >= 4 else ""


def _research_facts(conversation: dict[str, Any]) -> dict[str, Any]:
    """Extract durable, user-confirmed context for a later research node.

    The conversation record is the durable source for the v1 entry point.  A
    node must receive the confirmed scope and prior decisions explicitly; it
    should not depend on a short follow-up message or a fresh route decision.
    Keep the snapshot bounded so ordinary chat history cannot crowd out the
    node contract.
    """

    turns = conversation.get("turns") or []
    facts: list[dict[str, str]] = []
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("note_only"):
            continue
        role = str(turn.get("role") or "")
        content = str(turn.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        facts.append({"role": role, "content": content[:1800]})
    return {
        "confirmed_scope": str(conversation.get("research_confirmed_scope") or "").strip(),
        "research_question": str(conversation.get("research_question") or "").strip(),
        "facts": facts[-8:],
    }


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


async def route(
    text: str,
    conversation_id: str,
    *,
    model_ref: str | None = None,
    conversation: dict[str, Any] | None = None,
) -> intent_mod.Intent:
    """判定这句话该走哪条分支。

    **规则兜明确的 + 模型兜底**（用户口径 2026-09-25）：规则（词表匹配）命中明确意图就直接用，
    零延迟；规则拿不准、且这句话**看着像指令**时，再让模型判一次 —— 中英混排与口语说法
    只有这一层盖得住（词表是纯子串匹配，`start a literature review` 必掉进通用回答）。
    模型超时/失败/给不可信值都不影响结果：一律回落规则结论。

    ⚠️ 但**只在规则判成 ``chat`` 时才让模型复判**：``guide`` 是"想开始但没说清"的分支，
    它给的可点按钮就是入口，绝不能被模型降级成一句普通回答（2026-09-26 实测踩过）。
    """

    record = conversation if conversation is not None else conversations_service.read(conversation_id) or {}
    found = intent_mod._find_node(text)
    if intent_mod.defers_research(text):
        if found and not record.get("research_confirmed"):
            record["research_confirmation_pending"] = {"node": found[0], "scope": text}
        return intent_mod.Intent(kind="chat", reason="用户暂缓研究，只讨论需求")
    # An explicit next-stage request belongs to the requested node even when a
    # previous node is waiting for human input. Do not silently feed it back
    # into the old literature turn.
    explicit_node = _explicit_requested_node(text)
    explicit_stage_request = explicit_node and (
        explicit_node[0] != "literature_review" and intent_mod.looks_like_command(text)
    )
    if explicit_stage_request:
        record.pop("research_confirmation_pending", None)
        return intent_mod.Intent(
            kind="node",
            node=explicit_node[0],
            keyword=explicit_node[1],
            reason="用户明确指定了研究流程的下一节点",
        )
    pending = record.get("research_confirmation_pending")
    # A timed-out/paused node is still the active turn.  Codex steers that
    # turn's input queue instead of routing the next natural-language message
    # as a fresh research request.  Recover the node from the durable chain
    # snapshot when the older conversation record has no pending marker.
    active_run = await _active_research_run(conversation_id)
    waiting_node = active_run[0] if active_run else ""
    if not pending and waiting_node:
        authorization = bool(re.search(
            r"(?:授权|确认|同意|默认执行|按你的推荐|开始(?:检索|研究|实验|写作)|"
            r"继续(?:推进|执行|研究)|可以开始|go ahead|start now)",
            text,
            re.IGNORECASE,
        ))
        if authorization or is_continue_command(text):
            record["research_confirmed"] = True
            record["research_confirmed_scope"] = str(record.get("research_confirmed_scope") or text)
            # Consume the pending gate in the same turn.  Leaving this marker
            # behind makes chat.py hide the search and execution tools again,
            # so a natural-language confirmation can never resume the run.
            record.pop("research_confirmation_pending", None)
            return intent_mod.Intent(
                kind="node",
                node=waiting_node,
                keyword="继续",
                reason="把自然语言确认接入当前等待中的研究回合",
            )
    confirmed_now = bool(pending) and (
        _is_explicit_confirmation(text) or bool(re.search(
            r"现在可以(?:搜索|检索|开始|执行)|(?:确认|同意)开始|"
            r"开始文献调研|开始\s*idea|(?:go ahead|start now)", text, re.IGNORECASE
        ))
    )
    if confirmed_now:
        record["research_confirmed"] = True
        record["research_confirmed_scope"] = str(pending.get("scope") or "")
        record.pop("research_confirmation_pending", None)
        return intent_mod.Intent(kind="node", node=str(pending.get("node") or "literature_review"), reason="用户明确确认研究范围并授权开始")
    chained = await has_chain(conversation_id)
    result = intent_mod.resolve_intent(text, has_chain=chained)
    # 只让模型补足规则漏掉的口语或中英混排指令；不要让它改写
    # 明确的主题澄清分支。
    if result.kind == "chat" and intent_mod.looks_like_command(text):
        judged = await intent_mod.classify_with_model(
            text, model_ref=model_ref, has_chain=chained
        )
        if judged is not None:
            result = judged
    # 「继续 / 下一步」——已开链时**重入当前这一站**（再给它一次机会）。
    # 这是明确的执行意图，即使已有研究链也应重入当前节点。
    if result.kind == "chat" and chained and is_continue_command(text):
        snapshot = await chain_snapshot(conversation_id)
        if snapshot["node"]:
            return intent_mod.Intent(
                kind="node",
                node=str(snapshot["node"]),
                keyword="继续",
                reason="按「继续」重入当前这一站",
            )

    if result.kind == "node":
        # An explicit execution request is already authorization.  Only ask a
        # single topic question when the literature node has no usable scope.
        if result.node == "literature_review" and not _research_text(text, record):
            record["research_confirmation_pending"] = {"node": result.node, "scope": text}
            return intent_mod.Intent(
                kind="guide", node=result.node, reason="开始文献调研前还缺少具体研究主题"
            )
        record.pop("research_confirmation_pending", None)
    return result


# --------------------------------------------------------------------------- #
# ⓪ 普通对话（用户明确选了「只是普通对话」）
# --------------------------------------------------------------------------- #
async def stream_plain_chat(
    *, conversation_id: str, scope: dict[str, Any]
) -> AsyncIterator[str]:
    """处理当前回合的普通对话请求，不把它保存成永久模式。"""

    conversation = scope["conversation"]
    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": scope.get("project_id"),
            "routing": {"kind": "plain_chat", "mode": "plain"},
        },
    )
    # 正文由模型写；程序只传递当前回合的事实，不设置会话级模式。
    text = await phrasing.say(
        facts=(
            "研究者这条消息只是普通交流，不启动研究流程。"
            "请用自然语言直接回答当前消息，控制在两三句话，"
            "不要提研究模式、菜单或内部术语。"
        ),
        model_ref=str((scope.get("conversation") or {}).get("model_ref") or "") or None,
        purpose="plain_chat_intro",
    )
    if not text:
        # 模型没写出来 → **留空 + 一条过程行**，绝不拿程序话术冒充正文
        note = messages.system_row(
            "notice", "这条消息按普通交流处理。", tone="idle"
        )
        yield _sse("row", {"row": note})
        persisted_plain = [note]
    else:
        persisted_plain = []
        yield _sse("delta", {"text": text})
    try:
        conversations_service.append_turns(
            conversation,
            [
                {"role": "user", "content": scope["text"]},
                {
                    "role": "assistant",
                    "content": text,
                    "routing": "plain_chat",
                    "rows": persisted_plain,
                },
            ],
        )
    except Exception as exc:  # noqa: BLE001 - surface persistence failure as a terminal SSE frame
        logger.exception("普通对话持久化失败 conversation=%s", conversation_id)
        yield _sse(
            "error",
            {
                "code": "conversation_persist_failed",
                "message": f"对话保存失败：{type(exc).__name__}",
                "kind": type(exc).__name__,
                "interrupted": True,
            },
        )
        return
    yield _sse("done", {"conversation_id": conversation_id, "routing": "plain_chat"})


# --------------------------------------------------------------------------- #
# ① 引导词
# --------------------------------------------------------------------------- #
async def stream_guide(
    *,
    conversation_id: str,
    scope: dict[str, Any],
) -> AsyncIterator[str]:
    """当明确启动研究但缺主题时，只提出一个可回答的问题。"""

    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": scope.get("project_id"),
            "routing": {"kind": "guide", "reason": scope.get("reason", "")},
        },
    )
    conversation = scope["conversation"]
    if not conversation.get("research_confirmation_pending"):
        conversation["research_confirmation_pending"] = {"node": "literature_review", "scope": scope.get("text", "")}
    text = await phrasing.say(
        facts=(
            "研究者已经明确要求开始研究，但还没有给出研究主题。"
            "请只问一个具体问题：‘你希望调研哪个具体问题或算法？’"
            "不要提供选项或菜单，不要调用检索工具。"
        ),
        model_ref=str((scope.get("conversation") or {}).get("model_ref") or "") or None,
        purpose="research_guide_intro",
    )
    if text:
        yield _sse("delta", {"text": text})
    conversations_service.append_turns(
        scope["conversation"],
        [
            {"role": "user", "content": scope["text"]},
            {
                "role": "assistant",
                "content": text,
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
    subscription_timeout = False
    total_cost = 0.0
    persisted_rows: list[dict[str, Any]] = []

    if AsyncSessionLocal is None:  # pragma: no cover
        yield _sse("error", {"code": "db_unavailable", "message": "数据库会话不可用"})
        return

    cancel_event = asyncio.Event()
    _ACTIVE_RESEARCH[conversation_id] = cancel_event
    source: AsyncIterator[tuple[str, dict[str, Any]]] | None = None

    try:
        # The turn worker owns the research lifetime.  Binding it to the SSE
        # iterator (or a fixed wall-clock deadline) pauses long searches when
        # the browser is still waiting.  The browser is only a subscriber;
        # explicit user interruption is handled by the turn lifecycle.
        # Keep a generous outer guard around the subscription.  The actual
        # turn is owned by the orchestrator and has its own idle/transport
        # budgets; this guard only prevents a broken async iterator from
        # leaving an HTTP response open forever.
        source = orchestrator.run_node(
            AsyncSessionLocal,
            conversation_id=conversation_id,
            node=routing.node,
            text=_research_text(text, scope["conversation"]),
            project_id=project_id,
            research_facts=_research_facts(scope["conversation"]),
        )
        async for event, data in _iter_with_deadline(
            source, seconds=_node_deadline_seconds(), cancel_event=cancel_event
        ):
            events.append((event, data))
            if event == "done":
                status = str(data.get("status") or "failed")
                refs = data.get("refs") or {}
                total_cost = float(data.get("cost_usd") or 0)
                # 系统行：结论本身随后单独发，这里不重复
            elif event == "row":
                row = data.get("row")
                if isinstance(row, dict) and row.get("kind") != "system":
                    persisted_rows.append(row)
                    yield _sse("row", {"row": row})
            elif event in ("node", "attempt", "notice", "validation", "revert", "migrated",
                           "waiting_human", "error"):
                row = _system_row_for(event, data)
                if row is not None:
                    persisted_rows.append(row)
                    yield _sse("row", {"row": row})
    except TimeoutError as exc:
        # A broken subscription is a resumable waiting state.  Keep the
        # already collected facts and let the model explain the next action;
        # never turn a transport timeout into a fake assistant error.
        status = "waiting_human"
        subscription_timeout = True
        timeout_data = {
            "code": "research_subscription_timeout",
            "message": str(exc) or "研究步骤暂时没有返回",
            "retryable": True,
        }
        events.append(("error", timeout_data))
        events.append(("waiting_human", timeout_data))
        persisted_rows.append(_system_row_for("error", timeout_data) or {})
        yield _sse("error", timeout_data)
    except ResearchInterrupted as exc:
        status = "interrupted"
        events.append(("error", {"code": "research_interrupted", "message": str(exc)}))
        yield _sse("error", {
            "code": "research_interrupted",
            "message": str(exc),
            "interrupted": True,
        })
    finally:
        if source is not None:
            with contextlib.suppress(Exception):
                await source.aclose()
        if _ACTIVE_RESEARCH.get(conversation_id) is cancel_event:
            _ACTIVE_RESEARCH.pop(conversation_id, None)

    next_node = next((d.get("next_node") for e, d in events if e == "done"), None)
    # **不假装往下走**：后面几个节点首版未实装，自动连续跑必须在这里停住。
    # 但"停住"这件事**由模型用自己的话告诉研究者**（用户口径 2026-09-26：
    # 对话里的正文一律模型写），程序只把它作为一条**事实**交上去 + 留一条过程行。
    next_unimplemented = bool(
        status == "done" and next_node and next_node != "end" and not _implemented(next_node)
    )

    facts = _facts_for_conclusion(
        label=label,
        status=status,
        events=events,
        refs=refs,
        next_node=next_node,
        next_unimplemented=next_unimplemented,
    )
    try:
        # A subscription timeout is already a terminal turn fact. Starting an
        # unrelated conclusion request here used to turn a bounded timeout into
        # an unbounded hang. Let the next resume request perform the follow-up.
        if subscription_timeout or status == "interrupted":
            summary = ""
        else:
            summary = await asyncio.wait_for(
                _write_conclusion(
                    facts=facts,
                    model_ref=str((scope.get("conversation") or {}).get("model_ref") or "") or None,
                ),
                timeout=_conclusion_timeout_seconds(),
            )
    except TimeoutError:
        status = "failed"
        timeout_data = {
            "code": "research_conclusion_timeout",
            "message": "研究节点已完成主体工作，但收尾摘要超时；可重新打开会话继续。",
            "retryable": True,
            "node_status": status,
        }
        events.append(("error", timeout_data))
        yield _sse("error", timeout_data)
        persisted_rows.append(_system_row_for("error", timeout_data) or {})
        summary = ""
    except Exception:  # noqa: BLE001 - terminal SSE must always close
        logger.warning("研究节点结论生成失败 conversation=%s", conversation_id, exc_info=True)
        status = "failed"
        error_data = {
            "code": "research_conclusion_failed",
            "message": "研究节点收尾摘要生成失败；主体产出状态已保留。",
            "retryable": True,
            "node_status": status,
        }
        events.append(("error", error_data))
        yield _sse("error", error_data)
        persisted_rows.append(_system_row_for("error", error_data) or {})
        summary = ""

    # 转人工时给**可点选项**（用户口径 2026-09-26：停下但要说清 + 给选项）。
    # 先从真实收尾正文提取具体行动；不按状态或证据数量硬编码研究者的选择。
    if status == "waiting_human":
        blocks = messages.waiting_human_blocks(
            summary=summary,
            node_label=label,
        )
        yield _sse("blocks", {"blocks": blocks})

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
                **({"blocks": blocks} if status == "waiting_human" else {}),
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
    """Render actionable orchestration facts as durable process rows."""
    if event not in {"attempt", "notice", "validation", "waiting_human", "error"}:
        return None
    message = str(data.get("message") or data.get("code") or event)
    tone = "err" if event == "error" else "warn" if event in {"validation", "waiting_human"} else "idle"
    return {"kind": "system", "tone": tone, "label": "研究节点", "text": message[:500]}


def _facts_for_conclusion(
    *,
    label: str,
    status: str,
    events: list[tuple[str, dict[str, Any]]],
    refs: dict[str, Any],
    next_node: str | None,
    next_unimplemented: bool,
) -> str:
    """把这一步的**事实**整理成给模型看的载荷（**不含任何面向用户的话术**）。

    用户口径 2026-09-26：对话里的正文一律由模型写。所以这里只报事实 ——
    状态、试了几次、为什么没过、产出了什么、下一步是什么，措辞一个字都不代它写。
    """

    done = next((d for e, d in events if e == "done"), {})
    lines: list[str] = [
        f"这一步是：{label}",
        f"结果状态：{status}",
        f"这一步尝试修复的次数：{len([1 for e, _ in events if e == 'validation'])}",
    ]

    # 未通过的具体原因（程序已经算出来的事实，照抄给模型，不加工）
    reasons = [str(d.get("message") or "") for e, d in events if e in ("validation", "error")]
    reasons = [item for item in reasons if item.strip()]
    if reasons:
        lines.append("未通过的原因（逐条）：")
        lines.extend(f"- {item[:400]}" for item in reasons[-3:])

    waiting = next((d for e, d in reversed(events) if e == "waiting_human"), None)
    if waiting is not None:
        lines.append(f"需要人介入的说明（程序记录的原文）：{str(waiting.get('message') or '')[:400]}")

    produced: list[str] = []
    if refs.get("evidence_ids"):
        produced.append(f"证据 {len(refs['evidence_ids'])} 条")
    if refs.get("idea_id"):
        produced.append(f"候选假设一条（编号 {refs['idea_id']}）")
    if refs.get("feasibility_id"):
        produced.append(f"可行性报告一份（编号 {refs['feasibility_id']}）")
    if refs.get("taskbook_id"):
        produced.append(f"任务书一份（编号 {refs['taskbook_id']}，已锁定）")
    if refs.get("taskbook_skipped"):
        produced.append("任务书没落库（本对话没挂项目，任务书要归属到项目下）")
    lines.append("这一步的产出：" + ("、".join(produced) if produced else "没有产出"))

    if next_node and next_node != "end":
        tail = f"下一步本来可以走：{_label_of(next_node)}"
        if next_unimplemented:
            tail += "（但这一步还**没有实装**：不会做校验、也不会真的执行实验）"
        lines.append(tail)

    # 节点自己写在产出里的文字（coverage_note / gaps / limits / closest_work …）。
    # ⚠️ 少了这一段，收尾就只能写出"没有任何产出" —— 哪怕模型在产出里已经写了自己的判断。
    notes = done.get("content_notes") or {}
    if notes:
        lines.append(
            "这一步**自己写在产出里的内容**（用你自己的话转述给研究者，"
            "**不要把字段名念出来**，也不要照抄 JSON）："
        )
        for key, value in notes.items():
            if isinstance(value, list):
                lines.append(f"- {key}：" + "；".join(str(item) for item in value))
            else:
                lines.append(f"- {key}：{value}")
    return "\n".join(lines)


async def _write_conclusion(*, facts: str, model_ref: str | None) -> str:
    """让**模型**写节点收尾那段话；写不出来就返回空串。

    为什么不兜一句程序文案：用户口径 2026-09-26「对话里所有正文都必须是模型输出」。
    失败时**宁可留空**（再补一条过程行如实说明），也不拿程序话术冒充正文。
    """

    return await phrasing.say(
        facts=messages.NODE_CONCLUSION_TEMPLATE.format(facts=facts),
        model_ref=model_ref,
        purpose="research_node_conclusion",
        system=messages.NODE_CONCLUSION_SYSTEM,
    )


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
