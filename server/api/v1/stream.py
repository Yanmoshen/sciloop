# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SSE 事件流（WP09-T7，附录 B.4 ``GET /stream/{project_id}``）。

- 事件名与载荷严格对齐 ``contracts.sse_events``
- **订阅 ``llm.events.default_bus``**：``llm_fallback`` / ``demo_mode`` / ``llm_error`` /
  ``replay_miss`` 转发为 SSE（WP02 的契约请求）
- 断线不丢状态：仅靠事件流的 ``Last-Event-ID`` 补齐事件；**状态一律用
  ``/pipelines/{pid}/status`` 与 ``/stages`` 补齐**（前端重连后先拉一次状态）
- 禁止 WebSocket（contracts.stack.explicitly_banned）

响应首帧为 ``retry: 3000`` 提示浏览器重连间隔，随后按 ``id:`` / ``event:`` / ``data:`` 推送，
每 15s 无事件时发一行 SSE 注释保活（``: keepalive``）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from db.models import Project
from db.session import AsyncSessionLocal
from services.pipeline import sse as sse_mod
from services.pipeline.sse import KEEPALIVE_SECONDS, hub, install_llm_forwarder

logger = logging.getLogger("sciloop.api.stream")

router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/{project_id}", summary="SSE 事件流（阶段进度 / 决策 / 风险策略 / 人工 / 熔断）")
async def stream_project_events(
    project_id: int,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    if AsyncSessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unavailable", "message": "数据库会话不可用", "detail": None},
        )
    async with AsyncSessionLocal() as session:
        exists = (
            await session.execute(select(Project.id).where(Project.id == project_id))
        ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "project_not_found", "message": f"project {project_id} 不存在", "detail": None},
        )

    install_llm_forwarder()
    queue = hub.subscribe(project_id)
    since = _parse_last_event_id(last_event_id)

    async def event_source() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            yield f": connected project_id={project_id}\n\n"
            for item in hub.events_since(since, project_id):
                yield item.format()
            while True:
                if await request.is_disconnected():
                    logger.info("SSE 客户端断开 project_id=%s", project_id)
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield item.format()
        except asyncio.CancelledError:  # pragma: no cover - 客户端断开
            raise
        finally:
            hub.unsubscribe(project_id, queue)
            logger.info("SSE 订阅释放 project_id=%s", project_id)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx 反向代理下必须禁用缓冲，否则事件会被攒批
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{project_id}/recent", summary="最近事件（断线后补齐事件的兜底）")
async def recent_events(
    project_id: int,
    limit: int = 50,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> dict[str, object]:
    since = _parse_last_event_id(last_event_id)
    items = hub.events_since(since, project_id, limit=limit) if since else hub.buffer(project_id, limit=limit)
    return {
        "project_id": project_id,
        "items": [item.to_dict() for item in items],
        "subscribers": hub.subscriber_count(project_id),
        "llm_forwarder": sse_mod._FORWARDER_INSTALLED,
        "contract_events": sorted(sse_mod.CONTRACT_EVENTS),
        "note": "事件流仅用于实时展示；状态请以 /pipelines/{pid}/status 与 /stages 为准",
    }


def _parse_last_event_id(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


__all__ = ["router"]
