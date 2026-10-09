# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""WebSocket 会话循环。

一条连接的生命周期：

1. 握手鉴权（不通过则回一条结构化错误通知后关闭，不静默掉线）；
2. 循环：等客户端帧（超时即做「推送 + 心跳」），收到帧就解析、调度、回响应；
3. 命令执行期间**继续推送**——长命令（等待子 Agent、中断收尾）不会让事件流停摆；
4. 断线时清理订阅；服务关闭时先推 ``server/shutting_down`` 再以 1001 关闭。

顺序保证：事件通知严格按 ``sequence`` 递增投递；响应与通知之间不保证相对顺序
（客户端按序号去重与重排，见前端 reducer）。
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from api.v2.agent_protocol import (
    NOTIFY_PROTOCOL_ERROR,
    Notification,
    ProtocolError,
    control_notification,
    parse_client_frame,
)
from contracts.agent_v2.errors import AgentV2Error

from .auth import check_websocket
from .service import AgentV2Service

#: 关闭码：正常关闭 / 服务重启 / 策略拒绝。
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_POLICY_VIOLATION = 1008
CLOSE_TRY_AGAIN_LATER = 1013

#: 单条命令的最长执行时间（超过则中断等待，客户端可改用游标补齐）。
MAX_COMMAND_SECONDS = 120.0


async def _send(websocket: WebSocket, payload: dict) -> bool:
    """发送一帧；连接已关闭时返回 False（不抛异常，避免把整个循环带崩）。"""
    try:
        await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError):
        return False
    return True


async def _receive(websocket: WebSocket, timeout: float) -> str | bytes | None:
    """等待一帧；超时返回 None。"""
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
    except TimeoutError:
        return None
    kind = message.get("type")
    if kind == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", CLOSE_NORMAL))
    if kind != "websocket.receive":
        return None
    text = message.get("text")
    if text is not None:
        return text
    return message.get("bytes")


async def _flush(websocket: WebSocket, service: AgentV2Service, conn) -> bool:
    """把待推送队列与订阅新事件全部发出去。"""
    service.pump(conn)
    for note in service.drain_outbound(conn):
        if not await _send(websocket, note.to_dict()):
            return False
    return True


async def _handle_frame(
    websocket: WebSocket, service: AgentV2Service, conn, raw: str | bytes
) -> None:
    """处理一帧客户端请求。"""
    try:
        request = parse_client_frame(raw)
    except ProtocolError as exc:
        await _send(
            websocket,
            control_notification(
                NOTIFY_PROTOCOL_ERROR,
                sequence=conn.next_sequence(),
                params={"error": exc.to_dict()},
            ).to_dict(),
        )
        return

    ctx = service.connect(conn.connection_id).context()
    ctx.request_id = request.id
    ctx.notifications.clear()

    task = asyncio.ensure_future(service.facade.dispatch(request, ctx))
    try:
        # 命令执行期间继续推送事件，避免长命令（等待子 Agent）冻住界面
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=MAX_COMMAND_SECONDS)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                await _send(
                    websocket,
                    control_notification(
                        NOTIFY_PROTOCOL_ERROR,
                        sequence=conn.next_sequence(),
                        params={
                            "error": {
                                "code": "internal_error",
                                "message": f"command {request.method} exceeded {MAX_COMMAND_SECONDS}s",
                                "data": {"method": request.method},
                            }
                        },
                    ).to_dict(),
                )
                return
            except Exception:  # noqa: BLE001 - 真实结果由 task 自己携带
                break
            if not task.done() and not await _flush(websocket, service, conn):
                return
        response = await task
    except WebSocketDisconnect:
        raise
    await _send(websocket, response.to_dict())
    for note in ctx.notifications:
        if not await _send(websocket, note.to_dict()):
            return
    await _flush(websocket, service, conn)


async def serve_connection(websocket: WebSocket, service: AgentV2Service) -> None:
    """一条 WebSocket 连接的完整服务过程。

    握手判定分两层：能否建立会话（``can_connect``）与是否具备写权限（``owner``）。
    匿名公开面允许建立**只读**会话：变更类请求会得到 ``permission_denied``，
    而不是把连接直接掐掉——否则用户只会看到「连不上」，不知道原因。
    """
    decision = check_websocket(websocket)
    await websocket.accept()
    conn = service.connect(owner=decision.owner)
    if not decision.can_connect:
        await _send(
            websocket,
            control_notification(
                NOTIFY_PROTOCOL_ERROR,
                sequence=conn.next_sequence(),
                params={
                    "error": {
                        "code": "permission_denied",
                        "message": "访问被拒绝：请提供研究者令牌后重连。",
                        "data": decision.to_dict(),
                    }
                },
            ).to_dict(),
        )
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        service.disconnect(conn.connection_id)
        return

    await _send(websocket, service.ready_notification(conn).to_dict())
    try:
        while True:
            if service.shutting_down:
                await _flush(websocket, service, conn)
                await websocket.close(code=CLOSE_GOING_AWAY)
                return
            try:
                raw = await _receive(websocket, service.poll_interval_s)
            except WebSocketDisconnect:
                return
            if raw is None:
                if not await _flush(websocket, service, conn):
                    return
                if service.heartbeat_due():
                    note: Notification = service.heartbeat(conn)
                    if not await _send(websocket, note.to_dict()):
                        return
                continue
            conn.received += 1
            await _handle_frame(websocket, service, conn, raw)
    except WebSocketDisconnect:
        return
    except AgentV2Error:
        return
    finally:
        service.disconnect(conn.connection_id)


__all__ = [
    "serve_connection",
    "CLOSE_NORMAL",
    "CLOSE_GOING_AWAY",
    "CLOSE_POLICY_VIOLATION",
    "CLOSE_TRY_AGAIN_LATER",
    "MAX_COMMAND_SECONDS",
]
