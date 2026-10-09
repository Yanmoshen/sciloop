# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""请求 / 响应 / 通知三类信封。

三类帧由 ``kind`` 字段严格区分，各自结构由 ``jsonrpc.schema.json`` 约束：

- ``request``：客户端发起，必须带连接内唯一 ``id``；改变状态的方法还必须带
  ``idempotency_key``，断线重试复用同一个键即可安全重放；
- ``response``：服务端对某条 request 的应答，成功带 ``result``，失败带结构化 ``error``；
- ``notification``：服务端主动推送。**每条通知都带 ``event_id`` / ``sequence`` /
  ``thread_id`` 三个字段**（连接级通知的 ``thread_id`` 为 ``null``，字段仍然存在）。
  线程作用域通知的 ``sequence`` 就是事件序号，因此可以直接用游标重放；
  连接级通知使用连接内自增序号，不参与事件游标重放。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import Event

from .errors import ErrorCode, ProtocolError
from .schema import try_parse_json, validate_frame
from .version import PROTOCOL_VERSION

KIND_REQUEST = "request"
KIND_RESPONSE = "response"
KIND_NOTIFICATION = "notification"

#: 通知通道
NOTIFY_EVENT = "event"
NOTIFY_SUBSCRIPTION_STARTED = "subscription/started"
NOTIFY_SUBSCRIPTION_CANCELLED = "subscription/cancelled"
NOTIFY_THREAD_CLOSED = "thread/closed"
NOTIFY_SHUTTING_DOWN = "server/shutting_down"
NOTIFY_HEARTBEAT = "heartbeat"
NOTIFY_PROTOCOL_ERROR = "protocol/error"
#: 连接建立后的第一条通知（WP-02「WebSocket 建立后发送 protocol/ready」）。
NOTIFY_READY = "protocol/ready"

MAX_ID_LEN = 128
MAX_IDEMPOTENCY_KEY_LEN = 200


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class Request:
    """一条客户端请求。"""

    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    contract: str = PROTOCOL_VERSION

    def fingerprint(self) -> str:
        """载荷指纹（方法 + 参数），用于同 id 重复提交的判定。"""
        digest = hashlib.sha256(_canonical({"method": self.method, "params": self.params}).encode())
        return digest.hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "kind": KIND_REQUEST,
            "id": self.id,
            "method": self.method,
            "params": dict(self.params),
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class Response:
    """对某条请求的应答。失败时 ``result`` 为 null、``error`` 非空。"""

    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    replayed: bool = False
    contract: str = PROTOCOL_VERSION

    @classmethod
    def success(
        cls, request_id: str, result: dict[str, Any] | None = None, *, replayed: bool = False
    ) -> Response:
        return cls(id=request_id, ok=True, result=dict(result or {}), replayed=replayed)

    @classmethod
    def failure(cls, request_id: str, error: ProtocolError | dict[str, Any]) -> Response:
        payload = error.to_dict() if isinstance(error, ProtocolError) else dict(error)
        return cls(id=request_id, ok=False, error=payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "kind": KIND_RESPONSE,
            "id": self.id,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "replayed": self.replayed,
        }


@dataclass
class Notification:
    """服务端主动推送。三个标识字段始终存在（连接级通知的 ``thread_id`` 为 null）。"""

    method: str
    event_id: str
    sequence: int
    params: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    call_id: str | None = None
    contract: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "kind": KIND_NOTIFICATION,
            "method": self.method,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "item_id": self.item_id,
            "call_id": self.call_id,
            "params": dict(self.params),
        }


def notification_from_event(event: Event, *, method: str = NOTIFY_EVENT) -> Notification:
    """把一条持久化事件转成通知（事件序号即通知序号，可直接作为游标）。"""
    return Notification(
        method=method,
        event_id=event.event_id,
        sequence=event.sequence,
        thread_id=event.thread_id,
        turn_id=event.turn_id,
        item_id=event.item_id,
        call_id=event.call_id,
        params={
            "type": event.type,
            "created_at": event.created_at,
            "payload": event.payload,
            "idempotency_key": event.idempotency_key,
        },
    )


def control_notification(
    method: str,
    *,
    sequence: int,
    params: dict[str, Any] | None = None,
    thread_id: str | None = None,
) -> Notification:
    """构造连接级 / 订阅级控制通知（``event_id`` 新生成，序号由调用方给出）。"""
    return Notification(
        method=method,
        event_id=new_id("event"),
        sequence=int(sequence),
        thread_id=thread_id,
        params=dict(params or {}),
    )


def parse_client_frame(raw: Any) -> Request:
    """解析并校验一帧客户端请求。

    只接受 ``kind == "request"``；``response`` / ``notification`` 由客户端发来属于协议违规。
    """
    frame = try_parse_json(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    if not isinstance(frame, dict):
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            "frame must be a JSON object",
            data={"got": type(frame).__name__},
        )
    kind = frame.get("kind")
    if kind != KIND_REQUEST:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            f"only {KIND_REQUEST!r} frames may be sent by a client, got {kind!r}",
            data={"kind": kind},
        )
    validate_frame(frame, KIND_REQUEST, label=KIND_REQUEST)
    return Request(
        id=str(frame["id"]),
        method=str(frame["method"]),
        params=dict(frame.get("params") or {}),
        idempotency_key=frame.get("idempotency_key"),
        contract=str(frame.get("contract", PROTOCOL_VERSION)),
    )


__all__ = [
    "KIND_REQUEST",
    "KIND_RESPONSE",
    "KIND_NOTIFICATION",
    "NOTIFY_EVENT",
    "NOTIFY_SUBSCRIPTION_STARTED",
    "NOTIFY_SUBSCRIPTION_CANCELLED",
    "NOTIFY_THREAD_CLOSED",
    "NOTIFY_SHUTTING_DOWN",
    "NOTIFY_HEARTBEAT",
    "NOTIFY_PROTOCOL_ERROR",
    "MAX_ID_LEN",
    "MAX_IDEMPOTENCY_KEY_LEN",
    "Request",
    "Response",
    "Notification",
    "notification_from_event",
    "control_notification",
    "parse_client_frame",
]
