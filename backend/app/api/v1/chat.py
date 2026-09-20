# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""首页「开始使用」的对话端点。

做什么
------
- ``POST /api/v1/chat/home``：一次请求内完成「生成标题」+「生成正式回答」（非流式）。
- ``POST /api/v1/chat/home/stream``：**真流式**（SSE），只给首页对话用。

为什么需要独立的一条流式路径
----------------------------
六环节流水线走 ``app.llm.adapter.chat()``（非流式 + 结构化输出 + JSON 校验重试）。
首页对话要的是「边生成边显示」，两者对重试/降级的要求互相冲突，因此**新增**
``adapter.chat_stream()`` 与 ``http_client.chat_completions_stream()``，
``chat()`` 那条主链路一行不改（风险最低）。

红线与口径
----------
- **外层只做校验与转发**，所有出网、路由、计价、记账、回放都复用 ``app.llm.adapter``
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

from app.core.security import require_owner
from app.llm import adapter
from app.llm.errors import LLMError
from app.llm.registry import get_registry
from app.llm.types import slugify_provider
from app.services import conversations

logger = logging.getLogger("sciloop.chat")

router = APIRouter(tags=["chat"])

TITLE_MAX_CHARS = 20
#: 64 太小：思考型模型会把预算全花在 reasoning 上，content 为空 → 标题静默降级。
TITLE_MAX_TOKENS = 400

TITLE_PROMPT = (
    "你是科研项目的命名助手。为下面这段研究需求拟一个标题。\n"
    "要求：中文；不超过 20 个字；只输出标题本身，不要引号、不要句号、不要解释。\n\n"
    "研究需求：\n{text}"
)

REPLY_SYSTEM = (
    "你是 SciLoop 的科研助手，帮助研究者把模糊的研究需求整理成可执行的研究方案。\n"
    "回答要求：结构清晰、直指要点；如果信息不足，就直接列出你需要用户补充的关键信息；"
    "不要编造文献、数据或结论。"
)


class HomeChatRequest(BaseModel):
    """首页单轮对话入参。"""

    text: str = Field(min_length=1, max_length=4000)
    model_config_id: int
    model_id: str = Field(min_length=1, max_length=200)
    #: 不传 = 新建会话；传了 = 接着这个会话继续（会带上最近若干轮作为上下文）
    conversation_id: str | None = Field(default=None, max_length=64)
    #: 不传 = 未分组；传了 = 这条新会话直接归到该项目下
    project_id: int | None = None


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

    # 1) 标题：失败只降级，不让整次请求失败
    title, title_source, title_note = await _generate_title(text, ref)

    # 2) 正式回答：失败必须如实抛出（不能伪装成功）
    #    带上下文：同一会话的历史轮次（实现「接着上次继续」）
    conversation = conversations.read(payload.conversation_id) if payload.conversation_id else None
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

    # 3) 落盘：新会话用模型给的标题建，已知会话则只补这一轮
    if conversation is None:
        conversation = conversations.create(
            title=title,
            model_ref=ref,
            project_id=payload.project_id,
        )
    else:
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
    """
    text = payload.text.strip()
    if not text:
        raise _error(422, "empty_text", "请输入内容后再发送")

    ref = await _resolve_model_ref(payload.model_config_id, payload.model_id)

    conversation = conversations.read(payload.conversation_id) if payload.conversation_id else None
    is_new = conversation is None
    if conversation is None:
        conversation = conversations.create(
            title=text[:TITLE_MAX_CHARS],
            model_ref=ref,
            project_id=payload.project_id,
        )
    conversation_id = str(conversation["id"])
    history = conversations.context_messages(conversation)

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
                allow_fallback=True,
            ):
                if update.kind == "delta":
                    buffer.append(update.text)
                    yield _sse("delta", {"text": update.text})
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
            if generated or error_info is None:
                # 有正文 → 追加本轮；无正文且无错误 → 模型真的返回空，也要如实记一条
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
