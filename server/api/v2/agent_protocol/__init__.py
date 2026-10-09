# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""WebSocket 双向 JSON-RPC 风格协议层（Agent 3 / WP-09）。

分层：

- :mod:`api.v2.agent_protocol.envelope` —— 请求 / 响应 / 通知三类信封；
- :mod:`api.v2.agent_protocol.errors` —— 结构化错误码与领域错误映射；
- :mod:`api.v2.agent_protocol.methods` —— 方法注册表（含幂等与参数要求）；
- :mod:`api.v2.agent_protocol.idempotency` —— 客户端幂等键缓存；
- :mod:`api.v2.agent_protocol.subscriptions` —— 线程订阅与游标推进。

协议版本：``agent.v2.protocol.v1``（与冻结契约 ``agent.v2.contract.v1`` 并列，互相不覆盖）。
"""

from __future__ import annotations

from .envelope import (
    KIND_NOTIFICATION,
    KIND_REQUEST,
    KIND_RESPONSE,
    NOTIFY_EVENT,
    NOTIFY_HEARTBEAT,
    NOTIFY_PROTOCOL_ERROR,
    NOTIFY_READY,
    NOTIFY_SHUTTING_DOWN,
    NOTIFY_SUBSCRIPTION_CANCELLED,
    NOTIFY_SUBSCRIPTION_STARTED,
    NOTIFY_THREAD_CLOSED,
    Notification,
    Request,
    Response,
    control_notification,
    notification_from_event,
    parse_client_frame,
)
from .errors import (
    ErrorCode,
    ProtocolError,
    error_from_agent_v2,
    error_from_exception,
)
from .idempotency import IdempotencyStore
from .methods import (
    METHOD_TABLE,
    MethodSpec,
    describe_methods,
    method_names,
    method_spec,
    validate_params,
)
from .schema import (
    FRAME_KINDS,
    SCHEMA_FILENAME,
    frame_validator,
    load_protocol_schema,
    validate_frame,
)
from .subscriptions import Subscription, SubscriptionRegistry
from .version import PROTOCOL_VERSION, READABLE_PROTOCOL_VERSIONS, is_readable

__all__ = [
    "PROTOCOL_VERSION",
    "READABLE_PROTOCOL_VERSIONS",
    "is_readable",
    "KIND_REQUEST",
    "KIND_RESPONSE",
    "KIND_NOTIFICATION",
    "NOTIFY_EVENT",
    "NOTIFY_READY",
    "NOTIFY_SUBSCRIPTION_STARTED",
    "NOTIFY_SUBSCRIPTION_CANCELLED",
    "NOTIFY_THREAD_CLOSED",
    "NOTIFY_SHUTTING_DOWN",
    "NOTIFY_HEARTBEAT",
    "NOTIFY_PROTOCOL_ERROR",
    "Request",
    "Response",
    "Notification",
    "parse_client_frame",
    "notification_from_event",
    "control_notification",
    "ErrorCode",
    "ProtocolError",
    "error_from_agent_v2",
    "error_from_exception",
    "IdempotencyStore",
    "Subscription",
    "SubscriptionRegistry",
    "METHOD_TABLE",
    "MethodSpec",
    "method_spec",
    "method_names",
    "validate_params",
    "describe_methods",
    "FRAME_KINDS",
    "SCHEMA_FILENAME",
    "frame_validator",
    "load_protocol_schema",
    "validate_frame",
]
