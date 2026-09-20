# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""会话接口：列表 / 详情 / 归档 / 移入项目。写操作由 ``POST /chat/home`` 与流式端点完成。

顶层字面前缀 ``/conversations``，与四核心模块同样规避「静态段被参数路由抢占」。

分组口径（2026-09-20 起）
-------------------------
会话按所属项目分目录落盘（``conversations/<项目id>/<会话id>.json``，未分组为
``_ungrouped``）。列表默认返回**全部未归档**会话，前端据此铺左栏的
「未分组」与各项目下的对话；``group=ungrouped`` / ``group=<项目id>`` / ``archived=true``
用于精确取子集。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import require_owner
from app.services import conversations as store

router = APIRouter(tags=["conversations"])

OwnerDep = Annotated[None, Depends(require_owner)]


class ConversationBrief(BaseModel):
    id: str
    title: str | None = None
    model_ref: str | None = None
    project_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived: bool = False
    turn_count: int = 0


class ConversationList(BaseModel):
    items: list[ConversationBrief] = Field(default_factory=list)


class ConversationDetail(ConversationBrief):
    turns: list[dict[str, Any]] = Field(default_factory=list)


def _error(http_status: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"code": code, "message": message, "detail": detail},
    )


def _resolve_group(group: str) -> Any:
    """``group`` 查询参数 → ``store.list_all`` 的 ``project_id`` 口径。

    ``all``（默认，不过滤）/ ``ungrouped``（未分组）/ 数字字符串（该项目）。
    """
    normalized = (group or "all").strip().lower()
    if normalized in {"", "all"}:
        return "any"
    if normalized in {"ungrouped", "none", "null"}:
        return None
    try:
        return int(normalized)
    except ValueError:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"group 非法：{group}（应为 all / ungrouped / 项目 id）",
            {"group": group},
        ) from None


@router.get("/conversations", response_model=ConversationList, summary="会话列表（按最近更新倒序）")
async def list_conversations(
    group: str = Query("all", max_length=32),
    archived: bool = Query(False),
    limit: int | None = Query(None, ge=1, le=500),
) -> ConversationList:
    items = store.list_all(
        limit,
        project_id=_resolve_group(group),
        archived=archived,
    )
    return ConversationList(items=[ConversationBrief(**item) for item in items])


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="会话详情（含全部轮次）",
)
async def get_conversation(conversation_id: str) -> ConversationDetail:
    record = store.read(conversation_id)
    if record is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "conversation_not_found",
            f"会话 {conversation_id} 不存在",
        )
    data = store.summary(record)
    data["turns"] = record.get("turns") or []
    return ConversationDetail(**data)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="归档 / 取消归档、移入项目、重命名（需 X-Owner-Token）",
)
async def update_conversation(
    conversation_id: str,
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
) -> ConversationDetail:
    """只允许改 ``archived`` / ``project_id`` / ``title`` 三个字段。

    轮次内容不可从本端点改写：对话正文是「模型真实产出」的留痕，
    任何覆写都会让界面显示与 ``llm_call_logs`` 对不上。
    """
    unknown = set(payload) - {"archived", "project_id", "title"}
    if unknown:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            f"不支持修改字段：{sorted(unknown)}",
            {"allowed": ["archived", "project_id", "title"]},
        )
    if not payload:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "请求体不能为空", None)

    if store.read(conversation_id) is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "conversation_not_found",
            f"会话 {conversation_id} 不存在",
        )

    if "archived" in payload:
        if not isinstance(payload["archived"], bool):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "archived 必须为布尔值",
                {"archived": payload["archived"]},
            )
        store.set_archived(conversation_id, payload["archived"])

    if "project_id" in payload:
        raw = payload["project_id"]
        if raw is not None and not isinstance(raw, int):
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "project_id 必须为整数或 null（null = 移回未分组）",
                {"project_id": raw},
            )
        store.move(conversation_id, raw)

    if "title" in payload:
        title = str(payload["title"] or "").strip()
        if not title:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "validation_error", "title 不能为空", None
            )
        store.rename(conversation_id, title[:80])

    record = store.read(conversation_id)
    if record is None:  # pragma: no cover - 上一步刚校验过存在
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "conversation_not_found",
            f"会话 {conversation_id} 不存在",
        )
    data = store.summary(record)
    data["turns"] = record.get("turns") or []
    return ConversationDetail(**data)


__all__ = ["router"]
