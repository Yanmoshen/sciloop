# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""会话接口：列表 / 详情。写操作由 ``POST /chat/home`` 内部完成。

顶层字面前缀 ``/conversations``，与四核心模块同样规避「静态段被参数路由抢占」。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.services import conversations as store

router = APIRouter(tags=["conversations"])


class ConversationBrief(BaseModel):
    id: str
    title: str | None = None
    model_ref: str | None = None
    project_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    turn_count: int = 0


class ConversationList(BaseModel):
    items: list[ConversationBrief] = Field(default_factory=list)


class ConversationDetail(ConversationBrief):
    turns: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/conversations", response_model=ConversationList, summary="会话列表（按最近更新倒序）")
async def list_conversations(limit: int = Query(20, ge=1, le=100)) -> ConversationList:
    return ConversationList(items=[ConversationBrief(**item) for item in store.list_all(limit)])


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail, summary="会话详情（含全部轮次）")
async def get_conversation(conversation_id: str) -> ConversationDetail:
    record = store.read(conversation_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "conversation_not_found",
                "message": f"会话 {conversation_id} 不存在",
                "detail": None,
            },
        )
    data = store.summary(record)
    data["turns"] = record.get("turns") or []
    return ConversationDetail(**data)


__all__ = ["router"]
