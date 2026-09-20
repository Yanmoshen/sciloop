# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""首页「开始使用」的对话端点。

做什么
------
``POST /api/v1/chat/home``（Owner）
用户在首页输入一段需求后，一次请求内完成两件事：

1. **生成项目标题**：让模型给出一个简短标题（用户要求：项目名 = 模型返回的标题）；
2. **生成正式回答**：以科研助手身份回答这段需求，前端直接展示为对话首轮。

（标题与回答是两次 LLM 调用，但合并成一个 HTTP 请求：避免前端串两次往返。）

为什么需要它
------------
此前没有任何「按需单次对话」的 HTTP 入口：模型只被六环节流水线间接调用。
首页要能把**用户的一段话**直接交给模型，就必须有这样一个薄端点。

红线与口径
----------
- **外层只做校验与转发**，所有出网、路由、计价、记账、回放都复用 ``app.llm.adapter.chat``
  —— 不自己拼 HTTP、不自己算钱（单价缺失时 ``cost_usd`` 如实为 null）。
- 属于写操作（会产生真实费用）→ **Owner 专属**，匿名 403 ``owner_token_required``。
- 标题失败**不让整次请求失败**：退化为「取用户输入前 20 字」，并在 ``title_source`` 里如实标注。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
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


class HomeChatResponse(BaseModel):
    title: str
    #: ``model`` = 模型给出；``fallback`` = 标题调用失败后按用户输入截断
    title_source: str
    #: 降级原因（仅在 ``title_source == "fallback"`` 时有值）——**如实回传，不静默**
    title_note: str | None = None
    reply: str
    #: 本次会话 id（后端 JSON 落盘），前端据此接着继续
    conversation_id: str
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


@router.post(
    "/chat/home",
    response_model=HomeChatResponse,
    summary="首页对话：生成项目标题 + 正式回答（需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def home_chat(payload: HomeChatRequest) -> HomeChatResponse:
    text = payload.text.strip()
    if not text:
        raise _error(422, "empty_text", "请输入内容后再发送")

    record = await get_registry().get_config(payload.model_config_id)
    if record is None:
        raise _error(
            404,
            "config_not_found",
            f"供应商 {payload.model_config_id} 不存在",
            {"model_config_id": payload.model_config_id},
        )

    ref = f"{slugify_provider(record.name)}:{payload.model_id}"
    if not any(entry.get("model_id") == payload.model_id for entry in (record.models or [])):
        raise _error(
            409,
            "no_model",
            f"供应商「{record.name}」未登记模型 {payload.model_id}",
            {"model_ref": ref},
        )

    # 1) 标题：失败只降级，不让整次请求失败
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
            title_note = "模型未给出标题（返回空正文），已改用输入前 20 字"
    except Exception as exc:  # noqa: BLE001 - 标题是锦上添花，任何失败都降级
        logger.warning("首页标题生成失败，降级为用户输入截断：%s", exc)
        title_note = f"标题生成失败：{exc}"[:200]

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

    # 3) 落盘：新会话用模型给的标题建，已知会话则追加这一轮
    if conversation is None:
        conversation = conversations.create(title=title, model_ref=ref)
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


__all__ = ["router"]
