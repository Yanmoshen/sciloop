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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.security import require_owner
from llm import adapter
from llm.errors import LLMError
from llm.registry import get_registry
from llm.types import slugify_provider
from services import conversations
from services.research import dialog, messages

logger = logging.getLogger("sciloop.chat")

router = APIRouter(tags=["chat"])

TITLE_MAX_CHARS = 20
#: 思考过程的落盘上限（超长截断）：它可能比正文长几倍，全存会让会话文件迅速膨胀
REASONING_MAX_CHARS = 6000
#: 64 太小：思考型模型会把预算全花在 reasoning 上，content 为空 → 标题静默降级。
TITLE_MAX_TOKENS = 400

TITLE_PROMPT = (
    "你是科研项目的命名助手。为下面这段研究需求拟一个标题。\n"
    "要求：中文；不超过 20 个字；只输出标题本身，不要引号、不要句号、不要解释。\n\n"
    "研究需求：\n{text}"
)

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
            TITLE_PROMPT.format(text=text),
            model_ref=ref,
            max_tokens=TITLE_MAX_TOKENS,
            temperature=0.2,
            purpose="home_title",
            allow_fallback=False,
            strict_logging=False,
        )
        candidate = (title_result.content or "").strip().strip("《》\"'“”").splitlines()[0].strip()
        if candidate:
            title = candidate[:TITLE_MAX_CHARS]
            title_source = "model"
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
        started = time.perf_counter()
        result: Any = None
        error_info: dict[str, Any] | None = None
        aborted = False
        try:
            async for update in adapter.chat_stream(
                [
                    {"role": "system", "content": REPLY_SYSTEM},
                    *history,
                    {"role": "user", "content": text},
                ],
                model_ref=ref,
                purpose="home_reply",
                max_tokens=REPLY_MAX_TOKENS,
                allow_fallback=True,
            ):
                if update.kind == "delta":
                    buffer.append(update.text)
                    yield _sse("delta", {"text": update.text})
                elif update.kind == "reasoning":
                    # **思考过程走独立通道**：它不是答复。前端折叠展示在耗时那一行下面，
                    # 落盘也单独存一个字段。此前它只被收集、最后被塞进 content 冒充正文，
                    # 结果「界面上看到的回答」和「库里存的」不是同一个东西。
                    reasoning_buffer.append(update.text)
                    yield _sse("reasoning", {"text": update.text})
                elif update.kind == "done":
                    result = update.result
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
            # 有正文 → 追加本轮；无正文且无错误 → 模型真的返回空，也要如实记一条。
            # 编辑重开时**无条件落盘**：用户改过的正文必须留痕，否则刷新后改动就凭空消失了。
            if generated or error_info is None or is_edit:
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


__all__ = ["router"]
