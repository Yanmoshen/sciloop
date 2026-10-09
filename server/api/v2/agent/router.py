# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""HTTP 与 WebSocket 路由（挂载前缀 ``/api/v2``）。

- ``GET  /api/v2/agent/health``   服务自检（模型为 fake、场景清单、关闭状态）
- ``GET  /api/v2/agent/protocol`` 协议 Schema + 方法清单（前端联调与验收取证用）
- ``POST /api/v2/agent/rpc``      单请求 HTTP 通道（无 WebSocket 的客户端 / 集成测试）
- ``WS   /api/v2/agent/ws``       双向会话主通道

路由只做「取服务 -> 交给门面」，不含任何业务判定。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, WebSocket

from api.v2.agent_protocol import PROTOCOL_VERSION, describe_methods, parse_client_frame
from api.v2.agent_protocol.errors import ProtocolError
from api.v2.agent_protocol.schema import load_protocol_schema

from .service import AgentV2Service
from .ws import serve_connection

ROUTER_PREFIX = "/agent"
MOUNT_PREFIX = "/api/v2"
WS_PATH = f"{MOUNT_PREFIX}{ROUTER_PREFIX}/ws"


def build_router(get_service: Callable[[], AgentV2Service]) -> APIRouter:
    """构造 agent v2 路由。"""
    router = APIRouter(prefix=ROUTER_PREFIX, tags=["agent-v2"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        service = get_service()
        return {"status": "ok" if not service.shutting_down else "shutting_down", **service.describe()}

    @router.get("/protocol")
    async def protocol() -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "methods": describe_methods(),
            "schema": load_protocol_schema(),
            "websocket_path": WS_PATH,
        }

    @router.post("/rpc")
    async def rpc(payload: Annotated[dict, Body(...)]) -> dict[str, Any]:
        """单请求通道：响应与本次调用产生的控制通知一并返回。"""
        service = get_service()
        connection_id = service.next_connection_id()
        context = service.new_call_context(connection_id)
        try:
            request = parse_client_frame(payload)
        except ProtocolError as exc:
            service.disconnect(connection_id)
            return {"response": None, "notifications": [], "error": exc.to_dict()}
        response = await service.facade.dispatch(request, context)
        notifications = [note.to_dict() for note in context.notifications]
        service.disconnect(connection_id)
        return {
            "response": response.to_dict(),
            "notifications": notifications,
            "error": None,
        }

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await serve_connection(websocket, get_service())

    return router


__all__ = ["ROUTER_PREFIX", "MOUNT_PREFIX", "WS_PATH", "build_router"]
