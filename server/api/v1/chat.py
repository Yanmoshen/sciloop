# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""首页「开始使用」的对话端点。

做什么
------
- ``POST /api/v1/chat/home``：一次请求内完成「生成标题」+「生成正式回答」（非流式）。
- ``POST /api/v1/chat/home/stream``：**真流式**（SSE），只给首页对话用。

为什么需要独立的一条流式路径
----------------------------
六环节流水线走 ``llm.adapter.chat()``（非流式 + 结构化输出 + JSON 校验重试）。
首页对话要的是「边生成边显示」，两者对重试/降级的要求互相冲突，因此**新增**
``adapter.chat_stream()`` 与 ``http_client.chat_completions_stream()``，
``chat()`` 那条主链路一行不改（风险最低）。

红线与口径
----------
- **外层只做校验与转发**，所有出网、路由、计价、记账、回放都复用 ``llm.adapter``
  —— 不自己拼 HTTP、不自己算钱（单价缺失时 ``cost_usd`` 如实为 null）。
- 属于写操作（会产生真实费用）→ **Owner 专属**，匿名 403 ``owner_token_required``。
- 标题失败**不让整次请求失败**：退化为「取用户输入前 20 字」，并在 ``title_source`` 里如实标注。
- 流中断/出错**不丢已生成部分**：落盘保留正文，SSE 尾部如实推 ``error`` 事件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.security import require_owner
from llm import adapter
from llm.errors import LLMError
from llm.registry import get_registry
from llm.types import slugify_provider
from services import conversations
from services.agent import approvals as agent_approvals
from services.research import dialog, messages

logger = logging.getLogger("sciloop.chat")

router = APIRouter(tags=["chat"])

TITLE_MAX_CHARS = 20
#: 思考过程的落盘上限（超长截断）：它可能比正文长几倍，全存会让会话文件迅速膨胀
REASONING_MAX_CHARS = 6000
#: 64 太小：思考型模型会把预算全花在 reasoning 上，content 为空 → 标题静默降级。
TITLE_MAX_TOKENS = 400

#: 标题调用的 user 提示（约束在 `conversations.TITLE_SYSTEM` 里，两边都留着更稳）
TITLE_PROMPT = "为下面这段研究需求拟一个标题。\n\n研究需求：\n{text}"

REPLY_SYSTEM = (
    "你是 SciLoop 的科研助手，帮助研究者把模糊的研究需求整理成可执行的研究方案。\n"
    "回答要求：结构清晰、直指要点。\n"
    "信息不足时**最多集中提出 3 个问题**，且每个问题都要给出一个可用的默认值；"
    "如果研究者说「随便」「你帮我定」，就按默认值继续推进，并在回答里标明你用的是什么假设。\n"
    "**不要输出你的思考过程、推理草稿或自我对话**（例如「我们需要回答用户……」「让我想想……」），"
    "只输出给研究者看的最终答复。\n"
    "不要编造文献、数据或结论；没有实际查过本地数据就不要声称查过。"
)

#: 通用回答的输出上限。默认 1536 会被思考型模型的长思考吃光，正文只挤出半句就断
#: （实测有一轮只落了 26 个字符）。放宽到 4096 让正文有地方落。
REPLY_MAX_TOKENS = 4096


class HomeChatRequest(BaseModel):
    """首页单轮对话入参。"""

    text: str = Field(min_length=1, max_length=4000)
    model_config_id: int
    model_id: str = Field(min_length=1, max_length=200)
    #: 不传 = 新建会话；传了 = 接着这个会话继续（会带上最近若干轮作为上下文）
    conversation_id: str | None = Field(default=None, max_length=64)
    #: 不传 = 未分组；传了 = 这条新会话直接归到该项目下
    project_id: int | None = None
    #: **编辑重开**：把这轮当成"改写第 N 条用户消息"——先丢弃该条及其后的所有轮次，
    #: 再以 ``text`` 作为新的第 N 条重问一次。不传 = 普通追加一轮。
    #: 只接受指向 user 轮次的下标（指到 assistant 轮次直接 422，不做猜测）。
    replace_from: int | None = Field(default=None, ge=0)


class HomeChatResponse(BaseModel):
    title: str
    #: ``model`` = 模型给出；``fallback`` = 标题调用失败后按用户输入截断
    title_source: str
    #: 降级原因（仅在 ``title_source == "fallback"`` 时有值）——**如实回传，不静默**
    title_note: str | None = None
    reply: str
    #: 本次会话 id（后端 JSON 落盘），前端据此接着继续
    conversation_id: str
    project_id: int | None = None
    model_ref: str
    provider: str
    model_id: str
    cost_usd: float | None = None
    cost_unknown_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


def _error(status_code: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _agent_loop(
    messages_now: list[dict[str, Any]],
    *,
    conversation_id: str,
    ref: str,
    tool_defs: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    approvals_out: list[dict[str, Any]],
    state: dict[str, Any],
    allow_fallback_first: bool = True,
) -> AsyncIterator[str]:
    """agent 循环：模型 → 工具 → 结果回喂 → 再模型（轮数有上限，防死循环）。

    每一次工具调用都走同一道裁决（`agent_tools.judge` → `policy`），再叠上**本对话的授权**：

    - 硬拒（删代码这类）：直接回绝，不进批准队列（没得商量）；
    - 无害（列目录 / 读文件 / 查论文库）：直接做 —— 它不改变任何状态；
    - 动手（跑命令 / 写文件 / 删东西）：
      · 本对话选了「默认允许执行」（高危也放行）→ 直接做；
      · 开了「完全访问模式」且不是高危 → 直接做；
      · 否则 → **发一张批准卡，一次都不执行**。模型可以提出，但没有资格替自己批准。

    主对话与批准后续答共用本函数。**状态通过 `state` 写回**（`text` / `reasoning` /
    `result` / `pending`），而不是靠生成器返回值 —— 调用方在两处需要同一份口径，
    复制一份出来迟早会分叉。
    """

    from services.agent import mcp_tools as agent_tools

    for round_index in range(agent_tools.MAX_TOOL_ROUNDS + 1):
        round_result: Any = None
        async for update in adapter.chat_stream(
            messages_now,
            model_ref=ref,
            purpose="home_reply",
            max_tokens=REPLY_MAX_TOKENS,
            # 首轮与原有行为一致；**工具轮回喂时关掉降级**：实测降级链会切到
            # 没配 key 的供应商，把真正的 `bad_request` 掩盖成一句无关的
            # 「env 未配置 API Key」。宁可如实报第一跳的错，也不要换一家继续跑。
            #
            # 批准后的续答同理：它**不是**用户的第一句，`allow_fallback_first=False`
            # 才能把真因如实报出来（实测这里真因是"assistant 的 tool_calls 没带
            # reasoning_content"，被降级链掩盖成了一句无关的 auth 错误）。
            allow_fallback=allow_fallback_first and round_index == 0,
            tools=tool_defs or None,
        ):
            if update.kind == "delta":
                state["text"].append(update.text)
                yield _sse("delta", {"text": update.text})
            elif update.kind == "reasoning":
                # **思考过程走独立通道**：它不是答复。前端折叠展示在耗时那一行下面，
                # 落盘也单独存一个字段。此前它只被收集、最后被塞进 content 冒充正文，
                # 结果「界面上看到的回答」和「库里存的」不是同一个东西。
                state["reasoning"].append(update.text)
                yield _sse("reasoning", {"text": update.text})
            elif update.kind == "done":
                round_result = update.result

        if round_result is not None:
            state["result"] = round_result
        calls = agent_tools.normalize_tool_calls(getattr(round_result, "tool_calls", None) or [])
        if not calls or round_index >= agent_tools.MAX_TOOL_ROUNDS:
            break

        # 模型要求调工具：先把这一轮如实记进消息（含它已说的话），再逐个处理
        messages_now.append(
            agent_tools.assistant_tool_message(getattr(round_result, "content", "") or "", calls)
        )
        stopped_for_approval = False
        for call in calls:
            tool_name = str((call.get("function") or {}).get("name") or "")
            # 逐次裁决（不是按工具名一刀切）：同一句删除命令，删研究数据是"待批准"，
            # 删 SciLoop 自己的代码是"直接拒绝"，读文件则根本不用打扰研究者。
            verdict = await agent_tools.judge(call)
            if verdict.forbidden:
                # **没有商量余地的事不进批准队列**：直接拒绝，把原因如实回给模型，
                # 并落一行过程行 —— 研究者看得到"它想干什么、为什么被挡"。
                refused = {"ok": False, "error": verdict.message, "refused": True}
                row = agent_tools.tool_row(call, "err", verdict.message)
                rows.append(row)
                yield _sse("row", {"row": row})
                messages_now.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": agent_tools.tool_message_content(refused),
                    }
                )
                continue
            if verdict.needs_approval or not verdict.harmless:
                # 本对话当前的授权（**每轮重读**：研究者中途拨开关要立刻生效）
                grants_now = agent_approvals.grants(
                    agent_approvals.load(conversation_id) or {}
                )
                high_risk = verdict.needs_approval
                allowed_by_grant = agent_approvals.allows(grants_now, high_risk=high_risk)
                if not allowed_by_grant:
                    # 动手类：**只建请求，不执行**。连一次执行都不发出去。
                    arguments = agent_tools.arguments_of(call)
                    cwd = arguments.get("cwd")
                    request = agent_approvals.new_request(
                        tool=tool_name,
                        args=arguments,
                        cwd=cwd if isinstance(cwd, str) else None,
                        call_id=str(call.get("id") or "") or None,
                    )
                    approvals_out.append(request)
                    # **行即卡片**：卡片本身就是那一条过程行。同一份内容既发 row（用来落盘，
                    # 刷新后凭它重建）也发 approval（前端据此渲染按钮）——
                    # 只发 SSE 不落盘的话，刷新后卡片刻凭空消失，而库里留着一句"等待批准"。
                    card = agent_tools.approval_row(request)
                    rows.append(card)
                    yield _sse("row", {"row": card})
                    yield _sse("approval", card)
                    state["pending"] = request
                    stopped_for_approval = True
                    break
                # 已获授权：这一行如实写明"是按你在本对话里的授权直接执行的"，
                # 让研究者事后对得上账（而不是看到一次没人批准的执行）。
                start_detail = "按你在这个对话里的授权直接执行（无需再确认）"
                grant_token = agent_approvals.issue_token(f"grant-{conversation_id[:8]}")
            else:
                start_detail = ""
                grant_token = None
            start_row = agent_tools.tool_row(call, "start", start_detail)
            rows.append(start_row)
            yield _sse("row", {"row": start_row})
            payload_out, summary = await agent_tools.run_tool_call(
                call, approval_token=grant_token
            )
            end_row = agent_tools.tool_row(call, "ok" if payload_out.get("ok") else "err", summary)
            rows.append(end_row)
            yield _sse("row", {"row": end_row})
            messages_now.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": agent_tools.tool_message_content(payload_out),
                }
            )
        if stopped_for_approval:
            # 等研究者裁决：这一轮到此为止，绝不"先跑了再补批准"
            break


def _streaming(source: AsyncIterator[str]) -> StreamingResponse:
    """统一的 SSE 响应包装（对话各条分支共用同一组响应头）。"""

    return StreamingResponse(
        source,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive"},
    )


async def _resolve_model_ref(model_config_id: int, model_id: str) -> str:
    """校验供应商与模型登记，返回 ``provider:model_id``。"""
    record = await get_registry().get_config(model_config_id)
    if record is None:
        raise _error(
            404,
            "config_not_found",
            f"供应商 {model_config_id} 不存在",
            {"model_config_id": model_config_id},
        )
    ref = f"{slugify_provider(record.name)}:{model_id}"
    if not any(entry.get("model_id") == model_id for entry in (record.models or [])):
        raise _error(
            409,
            "no_model",
            f"供应商「{record.name}」未登记模型 {model_id}",
            {"model_ref": ref},
        )
    return ref


async def _generate_title(text: str, ref: str) -> tuple[str, str, str | None]:
    """生成标题。返回 ``(title, title_source, title_note)``；失败只降级，不抛。"""
    title = text[:TITLE_MAX_CHARS]
    title_source = "fallback"
    title_note: str | None = None
    try:
        title_result = await adapter.chat(
            [
                # ⚠️ 必须有 system 约束：只有 user 提示时，爱"自言自语"的模型
                # 会把推理写进正文，第一行就被当标题存下来（2026-09-22 实测 17 例）
                {"role": "system", "content": conversations.TITLE_SYSTEM},
                {"role": "user", "content": TITLE_PROMPT.format(text=text)},
            ],
            model_ref=ref,
            max_tokens=TITLE_MAX_TOKENS,
            temperature=0.2,
            purpose="home_title",
            allow_fallback=False,
            strict_logging=False,
        )
        candidate = conversations.pick_title_line(
            title_result.content or "", max_chars=TITLE_MAX_CHARS
        )
        if candidate:
            title = candidate[:TITLE_MAX_CHARS]
            title_source = "model"
        elif (title_result.content or "").strip():
            # 有正文但不含标题（整段是思考过程）：如实说明，用需求前 20 字兜底
            title_note = "模型返回的是思考过程而不是标题，已沿用需求前 20 字"
        else:
            title_note = "模型未给出标题（返回空正文），已沿用需求前 20 字"
    except Exception as exc:  # noqa: BLE001 - 标题是锦上添花，任何失败都降级
        logger.warning("首页标题生成失败，降级为用户输入截断：%s", exc)
        title_note = f"标题生成失败：{exc}"[:200]
    return title, title_source, title_note


def _truncate_for_edit(conversation: dict[str, Any] | None, replace_from: int | None) -> bool:
    """``replace_from`` 非空 = 编辑重开：校验下标并丢弃该条及其后的所有轮次（不落盘）。

    返回 True 表示本次走"编辑重开"（调用方据此决定：① 不重新生成标题；② 即使一个字都没生成，
    也要把用户改过的内容落盘 —— 用户改了就一定留痕）。

    校验从严、不做猜测：会话不存在 → 404；下标越界 → 422；指到 assistant 轮次 → 422。
    """
    if replace_from is None:
        return False
    if conversation is None:
        raise _error(404, "conversation_not_found", "要编辑的会话不存在（可能已被删除）")
    turns = conversation.get("turns") or []
    if replace_from >= len(turns):
        raise _error(
            422,
            "turn_index_out_of_range",
            f"第 {replace_from} 条消息不存在（当前共 {len(turns)} 条）",
        )
    if (turns[replace_from] or {}).get("role") != "user":
        raise _error(422, "not_a_user_turn", "只能编辑你自己发过的消息")
    conversations.truncate_from(conversation, replace_from)
    return True


@router.post(
    "/chat/home",
    response_model=HomeChatResponse,
    summary="首页对话：生成标题 + 正式回答（需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def home_chat(payload: HomeChatRequest) -> HomeChatResponse:
    text = payload.text.strip()
    if not text:
        raise _error(422, "empty_text", "请输入内容后再发送")

    ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)

    conversation = conversations.read(payload.conversation_id) if payload.conversation_id else None
    # 编辑重开：先校验并截断（会话不存在/下标越界都不会先建出空会话）
    is_edit = _truncate_for_edit(conversation, payload.replace_from)

    # 1) 标题：失败只降级，不让整次请求失败。编辑重开不动标题 —— 用户改的是正文，
    #    这次会话的主题没变（也避免"编辑一下标题就换了"）。
    if is_edit and conversation is not None:
        title = str(conversation.get("title") or text[:TITLE_MAX_CHARS])
        title_source, title_note = "kept", None
    else:
        title, title_source, title_note = await _generate_title(text, ref)

    # 2) 正式回答：失败必须如实抛出（不能伪装成功）
    #    带上下文：同一会话的历史轮次（实现「接着上次继续」；编辑重开后即为截断后的历史）
    history = conversations.context_messages(conversation) if conversation else []
    try:
        reply_result = await adapter.chat(
            [
                {"role": "system", "content": REPLY_SYSTEM},
                *history,
                {"role": "user", "content": text},
            ],
            model_ref=ref,
            purpose="home_reply",
            allow_fallback=False,
        )
    except LLMError as exc:
        raise _error(
            status.HTTP_502_BAD_GATEWAY,
            getattr(exc, "code", "llm_failed") or "llm_failed",
            str(exc),
            {"model_ref": ref, "type": type(exc).__name__},
        ) from exc

    # 3) 落盘：新会话用模型给的标题建，已知会话则只补这一轮（编辑重开时标题保持不变）
    if conversation is None:
        conversation = conversations.create(
            title=title,
            model_ref=ref,
            project_id=payload.project_id,
        )
    elif not is_edit:
        conversations.rename(str(conversation["id"]), title)
    conversations.append_turns(
        conversation,
        [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": reply_result.content or "",
                "model_id": reply_result.model_id,
                "duration_ms": reply_result.duration_ms,
            },
        ],
    )

    usage = reply_result.usage
    return HomeChatResponse(
        title=title,
        title_source=title_source,
        title_note=title_note,
        conversation_id=str(conversation["id"]),
        project_id=conversation.get("project_id"),
        reply=reply_result.content or "",
        model_ref=reply_result.model_ref,
        provider=reply_result.provider,
        model_id=reply_result.model_id,
        cost_usd=reply_result.cost_usd,
        cost_unknown_reason=reply_result.cost_unknown_reason,
        usage={
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        },
    )


@router.post(
    "/chat/home/stream",
    summary="首页对话（真流式 SSE：meta / delta / done / title / error，需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def home_chat_stream(payload: HomeChatRequest) -> StreamingResponse:
    """首页对话的流式版本。

    事件序列：

    - ``meta``：会话 id / 模型 / 项目（前端据此立刻把「新对话」挂到左栏）
    - ``delta``：正文增量（**逐段追加渲染**）
    - ``done``：耗时 / 用量 / 成本（``cost_usd`` 单价缺失时为 null，不估算）
    - ``title``：标题（模型生成完成或如实降级为需求前 20 字）
    - ``error``：建连失败或流中途断线；**此时 delta 已推的部分仍然有效并已落盘**

    落盘在 ``finally`` 里做：正常结束、报错、客户端断开三种情况都会把已生成的部分写进
    会话文件——**不白花 token**，也不允许"界面显示了但记录里没有"。

    ``replace_from`` 非空时为**编辑重开**：先校验（会话必须存在、下标必须指向 user 轮次，
    否则 404 / 422）再丢弃该条及其后的所有轮次，然后以 ``text`` 作为新的该条重问一次。
    这种情况下即使一个字都没生成也会落盘 —— 用户改过的正文必须留痕。
    """
    text = payload.text.strip()
    if not text:
        raise _error(422, "empty_text", "请输入内容后再发送")

    ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)

    conversation = conversations.read(payload.conversation_id) if payload.conversation_id else None
    # 编辑重开：先校验并截断，再建会话（会话不存在/下标越界都不会先建出空会话）
    is_edit = _truncate_for_edit(conversation, payload.replace_from)
    is_new = conversation is None
    if conversation is None:
        conversation = conversations.create(
            title=text[:TITLE_MAX_CHARS],
            model_ref=ref,
            project_id=payload.project_id,
        )
    conversation_id = str(conversation["id"])
    history = conversations.context_messages(conversation)

    # ------------------------------------------------------------------ #
    # 意图路由：**对话就是 agent 的入口**
    #   guide  → 模糊引导词且还没开链：给引导词 + 三个可点出口
    #   node   → 明确执行意图（「开始文献调研」）：直接跑节点，过程回到对话
    #   query  → 查询本地数据：确定性只读查询 + 结果卡片
    #   其余    → 通用回答（下面原有的流式路径）
    # 前三条都是**确定性**的（不再多花一次模型调用去措辞），费用与措辞都可控。
    # ------------------------------------------------------------------ #
    scope: dict[str, Any] = {
        "conversation": conversation,
        "project_id": conversation.get("project_id"),
        "text": text,
    }

    if dialog.is_plain_chat_reply(text):
        return _streaming(
            dialog.stream_plain_chat(conversation_id=conversation_id, scope=scope)
        )

    routing = await dialog.route(
        text, conversation_id, force_plain=bool(conversation.get("plain_chat"))
    )
    if routing.kind == "guide":
        return _streaming(dialog.stream_guide(conversation_id=conversation_id, scope=scope))
    if routing.kind == "node":
        return _streaming(
            dialog.stream_node(
                conversation_id=conversation_id, text=text, scope=scope, routing=routing
            )
        )
    if routing.kind == "query":
        return _streaming(
            dialog.stream_query(
                conversation_id=conversation_id, text=text, scope=scope, routing=routing
            )
        )

    async def event_source() -> AsyncIterator[str]:
        # 首帧：前端拿到会话 id 就能立刻把这条对话挂到左栏
        yield ": connected\n\n"
        yield _sse(
            "meta",
            {
                "conversation_id": conversation_id,
                "project_id": conversation.get("project_id"),
                "model_ref": ref,
                "model_id": payload.model_id,
                "title": conversation.get("title"),
                "turn_count": len(conversation.get("turns") or []),
            },
        )

        # 标题与正文并行：标题只在「新会话」时生成（续聊不改标题）
        title_task: asyncio.Task[tuple[str, str, str | None]] | None = None
        if is_new:
            title_task = asyncio.create_task(_generate_title(text, ref))

        buffer: list[str] = []
        reasoning_buffer: list[str] = []
        # 工具调用过程行也要**落盘**：只发 SSE 不落盘的话，刷新后这段过程凭空消失，
        # 而库里只剩一句结论 —— 正是「显示与落盘必须一致」要防的那种不一致。
        tool_rows: list[dict[str, Any]] = []
        #: 本轮里模型的**写盘/执行请求**（尚未执行）。与正文同源落进这一轮，
        #: 这样刷新后卡片还能按原状态重建，而不是"界面上一张卡、库里什么都没有"。
        approval_requests: list[dict[str, Any]] = []
        #: 停下来等研究者裁决的那一条（非空 = 这一轮没有答完，在等人）
        pending_approval: dict[str, Any] | None = None
        #: 本轮正文是**系统说明**（模型只给了思考过程）而不是模型说的话
        note_only = False
        started = time.perf_counter()
        result: Any = None
        error_info: dict[str, Any] | None = None
        aborted = False
        try:
            # ------------------------------------------------------------ #
            # agent 循环：把工具摆给模型 → 收 tool_calls → 只读的经 MCP 执行、
            # 写/执行类**只建批准请求** → 结果以 role=tool 喂回 → 再调一次，
            # 直到模型不再要求调工具（上限防死循环）。
            # 工具跑在 `mcp_server` 那个进程里（stdio 标准协议），边界也在那边。
            # 循环体在 `_agent_loop`，与批准后续答共用同一份口径。
            # 局部导入：与六环节的写法一致，避免无工具场景引入导入期依赖。
            # ------------------------------------------------------------ #
            from services.agent import mcp_tools as agent_tools

            messages_now: list[dict[str, Any]] = [
                {"role": "system", "content": REPLY_SYSTEM},
                *history,
                {"role": "user", "content": text},
            ]
            try:
                tool_defs = await agent_tools.tool_schemas()
            except Exception as exc:  # noqa: BLE001 - 工具侧不可用不该拖垮整段对话
                logger.warning("工具声明获取失败，本轮按无工具回答：%s", exc)
                tool_defs = []

            state: dict[str, Any] = {
                "text": buffer,
                "reasoning": reasoning_buffer,
                "result": None,
                "pending": None,
            }
            async for frame in _agent_loop(
                messages_now,
                conversation_id=conversation_id,
                ref=ref,
                tool_defs=tool_defs,
                rows=tool_rows,
                approvals_out=approval_requests,
                state=state,
            ):
                yield frame
            result = state["result"]
            pending_approval = state["pending"]
        except LLMError as exc:
            aborted = True
            error_info = {
                "code": getattr(exc, "code", "llm_failed") or "llm_failed",
                "message": str(exc),
                "kind": type(exc).__name__,
            }
            logger.warning("首页流式调用失败 conversation=%s：%s", conversation_id, exc)
        except asyncio.CancelledError:
            # 客户端断开：finally 仍会把已生成的部分落盘，然后原样抛出
            aborted = True
            error_info = {"code": "client_disconnected", "message": "客户端已断开", "kind": "Cancelled"}
            raise
        finally:
            if aborted and title_task is not None and not title_task.done():
                # 本轮已经失败/断开，标题结果用不上了，别留悬挂任务
                title_task.cancel()
            duration_ms = int((time.perf_counter() - started) * 1000)
            generated = "".join(buffer)
            # **显示与落盘必须一致**：部分供应商只在收尾帧给正文（delta 为空），
            # 旧代码只存 delta buffer，于是界面上有内容、库里是空串。
            if not generated and result is not None:
                generated = str(getattr(result, "content", "") or "")
            reasoning_text = "".join(reasoning_buffer)
            if not reasoning_text and result is not None:
                reasoning_text = str((getattr(result, "raw", None) or {}).get("reasoning") or "")
            if len(reasoning_text) > REASONING_MAX_CHARS:
                reasoning_text = reasoning_text[:REASONING_MAX_CHARS] + "\n…（思考过程过长，已截断）"
            if not generated and reasoning_text and error_info is None:
                # 模型只给了思考过程：如实说明，**不拿思考过程冒充答复**
                generated = messages.NODE_ONLY_REASONING_TEXT
                # 这句是"系统在说话"，不是模型说的话 —— 标记出来，别让它以 assistant 的身份
                # 进下一轮的上下文（模型会以为那是自己说过的话）。
                note_only = True
            # 有正文 → 追加本轮；无正文且无错误 → 模型真的返回空，也要如实记一条。
            # 编辑重开时**无条件落盘**：用户改过的正文必须留痕，否则刷新后改动就凭空消失了。
            # 等批准时也**无条件落盘**：卡片本身就是这一轮的产出（哪怕一个字都没有），
            # 不落盘就会出现"界面上有一张待批的卡、刷新后它不见了、而工具也永远没跑"。
            if generated or error_info is None or is_edit or pending_approval is not None:
                conversations.append_turns(
                    conversation,
                    [
                        {"role": "user", "content": text},
                        {
                            "role": "assistant",
                            "content": generated,
                            "model_id": getattr(result, "model_id", payload.model_id)
                            if result is not None
                            else payload.model_id,
                            "duration_ms": getattr(result, "duration_ms", duration_ms)
                            if result is not None
                            else duration_ms,
                            "reasoning": reasoning_text or None,
                            "rows": tool_rows,
                            # 批准请求随轮次落盘：裁决端点要凭它认账（一次性、带有效期）
                            "approvals": approval_requests,
                            "awaiting_approval": pending_approval["id"] if pending_approval else None,
                            # 正文是系统说明（不是模型说的话）→ 别让它进下一轮的上下文
                            "note_only": note_only or None,
                            "interrupted": bool(error_info),
                        },
                    ],
                )
            if is_new and error_info is not None and not generated:
                # 一个字都没生成也没落轮的「空会话」不留垃圾记录
                conversations.delete(conversation_id)

        if error_info is not None:
            # 已生成部分照旧显示；尾部用 error 事件让前端标「已中断 / 出错」
            yield _sse(
                "error",
                {
                    **error_info,
                    "interrupted": True,
                    "generated_chars": len("".join(buffer)),
                },
            )
            return

        usage = getattr(result, "usage", None)
        yield _sse(
            "done",
            {
                "conversation_id": conversation_id,
                "content": getattr(result, "content", "") if result is not None else "",
                "duration_ms": getattr(result, "duration_ms", 0) if result is not None else 0,
                "model_id": getattr(result, "model_id", payload.model_id),
                "model_ref": getattr(result, "model_ref", ref),
                "provider": getattr(result, "provider", ""),
                "cost_usd": getattr(result, "cost_usd", None) if result is not None else None,
                "cost_unknown_reason": getattr(result, "cost_unknown_reason", None)
                if result is not None
                else None,
                "finish_reason": getattr(result, "finish_reason", None) if result is not None else None,
                # 停在「等研究者批准」：前端据此把这一轮标成待裁决，**而不是**当成正常答完。
                # 两者混起来，界面会显示一个"答完了但什么都没有"的空回复。
                "awaiting_approval": agent_tools.approval_row(pending_approval)
                if pending_approval is not None
                else None,
                "usage": {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                },
            },
        )

        if title_task is not None:
            try:
                title, title_source, title_note = await title_task
            except Exception as exc:  # noqa: BLE001 - 标题失败不能影响这一轮对话
                logger.warning("标题任务异常：%s", exc)
                title, title_source, title_note = text[:TITLE_MAX_CHARS], "fallback", str(exc)[:200]
            if title != conversation.get("title"):
                conversations.rename(conversation_id, title)
            yield _sse(
                "title",
                {
                    "conversation_id": conversation_id,
                    "title": title,
                    "title_source": title_source,
                    "title_note": title_note,
                },
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx 反向代理下必须禁用缓冲，否则增量会被攒批，流式观感全失
            "X-Accel-Buffering": "no",
        },
    )


class ApprovalDecisionRequest(BaseModel):
    """研究者对一条**写盘/执行请求**的裁决。

    谁有资格裁决：`X-Owner-Token` 的唯一持有人（即研究者本人）。所以本端点与
    ``/chat/home/stream`` 一样是 Owner 专属 —— 批准入口如果对匿名开放，那道门就白设了。
    """

    conversation_id: str = Field(min_length=1, max_length=64)
    #: 待裁决请求的 id（来自 `approval` 事件 / 过程行里的 `request_id`）
    request_id: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "approve_conversation", "deny"]
    #: 可选备注，随裁决一起留痕（例如"只跑这次，下次同样的再说"）
    note: str | None = Field(default=None, max_length=500)
    #: 续答用哪个模型；不传 = 沿用会话记录里的 `model_ref`（通常够用）
    model_config_id: int | None = None
    model_id: str | None = Field(default=None, max_length=200)


#: 请求"已经不在待批状态"时的如实说明。**不能合并成一句"无效"** ——
#: 「批过了」「拒过了」「过期了」对研究者是三件不同的事，混起来他会以为是自己点错了。
_APPROVAL_CLOSED_REASONS = {
    "approved": "这条请求已经批准过了（一次性：同一批准不能重复执行）",
    "denied": "这条请求已被拒绝",
    "expired": "这条请求已过期（超过有效期未裁决）；需要的话让助手重新发起一次",
}

#: 拒绝后的确定性答复。**不花一次模型调用**：这句话不是模型的观点，是系统在陈述事实。
APPROVAL_DENIED_REPLY = "已记录你的拒绝，本次不执行「{label}」。需要换个做法的话，直接告诉我要怎么做。"


def _replace_approval_card(record: dict[str, Any], turn_index: int, card: dict[str, Any]) -> None:
    """把轮次里那张卡**换成新状态**（同一 request_id 只留一张，不追加第二张）。

    否则一次流程下来会攒出三四张卡，每一张都是同一次调用 —— 那不是留痕，那是噪声。
    """

    turns = record.get("turns") or []
    if turn_index >= len(turns):
        return
    turn = turns[turn_index]
    rows = list(turn.get("rows") or [])
    for index, row in enumerate(rows):
        if row.get("kind") == "approval" and row.get("request_id") == card.get("request_id"):
            rows[index] = card
            break
    else:  # pragma: no cover - 正常路径上卡片一定已经由主流程写进这一轮
        rows.append(card)
    turn["rows"] = rows


async def _approval_stream(
    record: dict[str, Any],
    turn_index: int,
    request: dict[str, Any],
    payload: ApprovalDecisionRequest,
    ref: str,
) -> AsyncIterator[str]:
    """裁决事件的流：批准就真的执行并继续回答，拒绝就如实记下来。

    **执行与否只由这里决定**，模型没有任何路径能自己把写盘/执行类工具跑掉。
    """

    from services.agent import mcp_tools as agent_tools

    conversation_id = str(record["id"])
    request_id = str(request["id"])
    tool = str(request["tool"])
    label = agent_tools.TOOL_LABELS.get(tool, tool)

    yield ": connected\n\n"
    yield _sse(
        "meta",
        {
            "conversation_id": conversation_id,
            "project_id": record.get("project_id"),
            "model_ref": ref,
            "title": record.get("title"),
            "turn_count": len(record.get("turns") or []),
            "routing": "approval",
        },
    )

    rows: list[dict[str, Any]] = []
    buffer: list[str] = []
    reasoning_buffer: list[str] = []
    #: 续答里模型**新提出**的批准请求：它们属于这一轮（新的一轮），不属于原来那一轮
    approvals_out: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None
    #: 本次裁决后那张卡的新状态（在 finally 里无条件换上去，**哪怕后面续答失败了**）
    card: dict[str, Any] | None = None
    result: Any = None
    error_info: dict[str, Any] | None = None
    started = time.perf_counter()

    try:
        if payload.decision == "deny":
            agent_approvals.decide(
                record, request_id, status="denied", actor="owner", note=payload.note
            )
            card = agent_tools.approval_row(request)
            decision_row = {
                "kind": "tool",
                "tone": "warn",
                "text": f"研究者已拒绝「{label}」" + (f"：{payload.note}" if payload.note else ""),
            }
            rows.append(decision_row)
            yield _sse("row", {"row": decision_row})
            yield _sse("approval", card)
            text = APPROVAL_DENIED_REPLY.format(label=label)
            buffer.append(text)
            yield _sse("delta", {"text": text})
        else:
            # ① 先签发**一次性**令牌并存指纹：会话文件里永远不会出现可用凭据
            token = agent_approvals.issue_token(request_id)
            agent_approvals.decide(
                record,
                request_id,
                status="approved",
                actor="owner",
                note=payload.note,
                token=token,
            )
            # 选「此对话中默认允许执行」：把授权写进**这个对话**（下一个对话要重新决定）。
            # 这是比「完全访问模式」更宽的一档：连高危操作也不再弹卡。
            if payload.decision == agent_approvals.DECISION_APPROVE_CONVERSATION:
                summary = agent_approvals.set_grants(record, allow_exec=True)
                agent_approvals.save(record)
                grant_row = {
                    "kind": "tool",
                    "tone": "ok",
                    "text": f"已允许在本对话内直接执行（{agent_approvals.grants_summary(record)['note']}）",
                }
                rows.append(grant_row)
                yield _sse("row", {"row": grant_row})
                logger.info("会话 %s 获得执行授权：%s", conversation_id, summary)
            card = agent_tools.approval_row(request)
            yield _sse("approval", card)
            decision_row = {"kind": "tool", "tone": "ok", "text": f"研究者已批准「{label}」"}
            rows.append(decision_row)
            yield _sse("row", {"row": decision_row})

            # ② 按**原样参数**执行（参数在批准时就冻结了，不能在这之后再被改）
            call = {
                # 沿用模型当时给的 id：`tool_calls` 与 `tool` 结果严格配对，
                # 也是"批准的到底是哪一次调用"最直接的凭证
                "id": str(request.get("call_id") or f"call_{request_id}"),
                "type": "function",
                "function": {
                    "name": tool,
                    "arguments": json.dumps(request.get("args") or {}, ensure_ascii=False),
                },
            }
            start_row = agent_tools.tool_row(call, "start")
            rows.append(start_row)
            yield _sse("row", {"row": start_row})
            payload_out, summary = await agent_tools.run_tool_call(call, approval_token=token)
            # 无论跑成没跑成，这次批准都**已经用掉了**（一次性）：不让"失败就再来一次"
            # 变成不受限的重试 —— 那等于把一次性批准变成了长期开关。
            agent_approvals.mark_consumed(record, request_id)
            end_row = agent_tools.tool_row(
                call, "ok" if payload_out.get("ok") else "err", summary
            )
            rows.append(end_row)
            yield _sse("row", {"row": end_row})

            # ③ 把「提了 → 批了 → 跑了」整条链补进消息再让模型回答：
            # 直接把结果塞成一句用户消息，模型就不知道这是工具跑出来的，会当成用户说的话。
            calls = agent_tools.normalize_tool_calls([call])
            # **把上一跳的思考过程原样带回**：思考型供应商（实测 deepseek 系列）在
            # `assistant.tool_calls` 回合会硬性要求 `reasoning_content`，缺了直接 400。
            # 它就在这一轮记录里（`turn["reasoning"]`），没理由不带。
            assistant_call_msg = agent_tools.assistant_tool_message("", calls)
            reasoning_before = str(((record.get("turns") or [])[turn_index] or {}).get("reasoning") or "")
            if reasoning_before:
                assistant_call_msg["reasoning_content"] = reasoning_before
            messages_now: list[dict[str, Any]] = [
                {"role": "system", "content": REPLY_SYSTEM},
                *conversations.context_messages(record),
                assistant_call_msg,
                {
                    "role": "tool",
                    "tool_call_id": str(calls[0]["id"]),
                    "content": agent_tools.tool_message_content(payload_out),
                },
            ]
            # 续答**照旧把工具摆上**：模型看到结果后常常要提下一条命令，
            # 那就再冒一张卡、再等一次裁决 —— 每条命令各批一次，这正是我们要的节奏。
            # （实测：不摆工具时，思考型供应商会因为"assistant 的 tool_calls 没带
            #   reasoning_content"直接 400，把续答整段打断。）
            try:
                tool_defs = await agent_tools.tool_schemas()
            except Exception as exc:  # noqa: BLE001 - 工具侧不可用不该拖垮续答
                logger.warning("续答时工具声明获取失败，本轮按无工具回答：%s", exc)
                tool_defs = []
            state: dict[str, Any] = {
                "text": buffer,
                "reasoning": reasoning_buffer,
                "result": None,
                "pending": None,
            }
            async for frame in _agent_loop(
                messages_now,
                conversation_id=conversation_id,
                ref=ref,
                tool_defs=tool_defs,
                rows=rows,
                approvals_out=approvals_out,
                state=state,
                allow_fallback_first=False,
            ):
                yield frame
            result = state["result"]
            pending = state["pending"]
    except LLMError as exc:
        error_info = {
            "code": getattr(exc, "code", "llm_failed") or "llm_failed",
            "message": str(exc),
            "kind": type(exc).__name__,
        }
        logger.warning("批准裁决后续答失败 conversation=%s：%s", conversation_id, exc)
    except asyncio.CancelledError:
        error_info = {"code": "client_disconnected", "message": "客户端已断开", "kind": "Cancelled"}
        raise
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        # **裁决这件事必须留在记录里，哪怕续答失败**：卡片的终态先换上。
        # 放在 finally 而不是分支末尾，是因为"续答炸了"和"裁决没发生"完全是两件事，
        # 不能让前者的异常把后者的痕迹一起抹掉。
        if card is not None:
            _replace_approval_card(record, turn_index, card)
        answer = "".join(buffer)
        if not answer and result is not None:
            answer = str(getattr(result, "content", "") or "")
        reasoning_text = "".join(reasoning_buffer)
        if not reasoning_text and result is not None:
            reasoning_text = str((getattr(result, "raw", None) or {}).get("reasoning") or "")
        if len(reasoning_text) > REASONING_MAX_CHARS:
            reasoning_text = reasoning_text[:REASONING_MAX_CHARS] + "\n…（思考过程过长，已截断）"
        # 裁决 + 卡片状态 + 这一轮答复**一次写入**：`append_turns` 写的就是整份记录，
        # 所以不会出现"批准记下了、卡片没更新"或反过来的半成品状态。
        if answer or rows:
            conversations.append_turns(
                record,
                [
                    {
                        "role": "assistant",
                        "content": answer,
                        "model_id": getattr(result, "model_id", "") if result is not None else "",
                        "duration_ms": getattr(result, "duration_ms", duration_ms)
                        if result is not None
                        else duration_ms,
                        "reasoning": reasoning_text or None,
                        "rows": rows,
                        # 续答里模型又提出的请求，随**这一轮**落盘（可能要再批一次）
                        "approvals": approvals_out,
                        "awaiting_approval": pending["id"] if pending else None,
                        "routing": "approval",
                        "interrupted": bool(error_info),
                    }
                ],
            )
        else:  # pragma: no cover - 兜底：一个字都没有、也没有行，至少把裁决写下
            conversations.write(record)

    if error_info is not None:
        yield _sse("error", {**error_info, "interrupted": True, "generated_chars": len("".join(buffer))})
        return

    usage = getattr(result, "usage", None)
    yield _sse(
        "done",
        {
            "conversation_id": conversation_id,
            "content": "".join(buffer) or (getattr(result, "content", "") if result is not None else ""),
            "duration_ms": getattr(result, "duration_ms", 0) if result is not None else 0,
            "model_id": getattr(result, "model_id", "") if result is not None else "",
            "model_ref": getattr(result, "model_ref", ref) if result is not None else ref,
            "provider": getattr(result, "provider", "") if result is not None else "",
            "cost_usd": getattr(result, "cost_usd", None) if result is not None else None,
            "cost_unknown_reason": getattr(result, "cost_unknown_reason", None)
            if result is not None
            else None,
            "routing": "approval",
            "decision": payload.decision,
            "request_id": request_id,
            # 本次裁决已收口；但**续答里模型可能又提了一条**，那就继续等下一次裁决
            "awaiting_approval": agent_tools.approval_row(pending) if pending else None,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        },
    )


@router.post(
    "/chat/approvals/decide",
    summary="研究者裁决一次写盘/执行请求（批准此次 / 此对话默认允许 / 拒绝）",
    dependencies=[Depends(require_owner)],
)
async def decide_approval(payload: ApprovalDecisionRequest) -> StreamingResponse:
    """批准入口 —— 合规 8.3「关键动作需人工接管」的落点。

    校验从严，不做猜测：

    - 会话不存在 → 404；请求不存在 → 404
    - 请求**已批准/已拒绝/已过期** → 409，且**分别给出不同的 code 与说明**
      （合并成一句"无效"会让研究者以为是自己点错了）
    - 只有 `pending` 且未过期才可裁决

    裁决成功后由 ``_approval_stream`` 决定后续动作：批准 → 签发一次性令牌 → 经 MCP
    执行（令牌进 Guard 的门 3）→ 结果回喂模型 → 继续回答；拒绝 → 确定性答复，不花模型调用。
    """

    record = agent_approvals.load(payload.conversation_id)
    if record is None:
        raise _error(404, "conversation_not_found", "会话不存在（可能已被删除）")

    found = agent_approvals.find_pending(record, payload.request_id)
    if found is None:
        existing = agent_approvals.find(record, payload.request_id)
        if existing is None:
            raise _error(404, "approval_not_found", "这条批准请求不存在")
        status = agent_approvals.effective_status(existing[1])
        raise _error(
            409,
            f"approval_{status}",
            _APPROVAL_CLOSED_REASONS.get(status, f"这条请求当前不可裁决（状态：{status}）"),
            {"request_id": payload.request_id, "status": status},
        )

    turn_index, request = found
    ref = str(record.get("model_ref") or "")
    if payload.model_config_id is not None and payload.model_id:
        ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)
    if not ref:
        raise _error(409, "no_model", "会话没有记录可用模型，请重新发送一次对话再裁决")

    return _streaming(_approval_stream(record, turn_index, request, payload, ref))


class AccessModeRequest(BaseModel):
    """完全访问模式开关（**按对话**）。

    为什么按对话而不是全局：研究者说的是「都是盖当前对话」——
    换一个对话就该重新决定，避免"某一次图省事"变成永久开关。
    """

    conversation_id: str = Field(min_length=1, max_length=64)
    full_access: bool


@router.post(
    "/chat/access-mode",
    summary="切换本对话的完全访问模式（开=普通动手操作不再逐一确认，高危仍会问）",
    dependencies=[Depends(require_owner)],
)
async def set_access_mode(payload: AccessModeRequest) -> dict[str, Any]:
    """开 / 关当前对话的「完全访问模式」。

    它管的是"动手类操作"（跑命令、写文件、删文件）要不要逐一确认：
    开着 → 普通操作直接做；关着 → 都先问一句。**高危操作无论开关如何都会先问**
    （除非研究者在那张卡上选了"此对话中默认允许执行"，那是更宽的一档）。
    只看东西的操作（列目录、读文件、查论文库）任何时候都不打扰研究者。
    """

    record = agent_approvals.load(payload.conversation_id)
    if record is None:
        raise _error(404, "conversation_not_found", "会话不存在（可能已被删除）")
    agent_approvals.set_grants(record, full_access=payload.full_access)
    agent_approvals.save(record)
    summary = agent_approvals.grants_summary(record)
    logger.info("会话 %s 完全访问模式=%s", payload.conversation_id, payload.full_access)
    return {"ok": True, "conversation_id": payload.conversation_id, **summary}


@router.get(
    "/chat/access-mode/{conversation_id}",
    summary="读本对话当前的授权状态（公开只读）",
)
async def read_access_mode(conversation_id: str) -> dict[str, Any]:
    """给界面显示用：现在是"动手前都先问"还是"直接做"。"""

    record = agent_approvals.load(conversation_id)
    if record is None:
        raise _error(404, "conversation_not_found", "会话不存在（可能已被删除）")
    return {"conversation_id": conversation_id, **agent_approvals.grants_summary(record)}


__all__ = ["router"]
