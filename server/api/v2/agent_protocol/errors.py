# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""协议层结构化错误。

设计约束（来自验收书 §2「非法 ID、非法状态和过期游标返回结构化错误」）：

- 每个错误都带**稳定的 ``code``**，客户端据此分支，不解析 message 文本；
- 领域层（``contracts.agent_v2.errors``）的异常在这里被**翻译**成协议错误，
  协议错误码是超集：既覆盖领域语义，也覆盖传输/会话语义（例如 ``stale_cursor``）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from contracts.agent_v2.errors import (
    AgentV2Error,
    ApprovalNotFound,
    ConcurrentTurnError,
    ContractViolation,
    CorruptedEventError,
    IllegalTurnTransition,
    LeaseError,
    MemoryOverwriteDenied,
    ModelStreamError,
    SummaryNotFound,
    ThreadNotFound,
    TurnNotFound,
)


class ErrorCode(StrEnum):
    """协议错误码（封闭集合）。"""

    # ---- 传输 / 会话 ----
    INVALID_REQUEST = "invalid_request"
    INVALID_PARAMS = "invalid_params"
    UNKNOWN_METHOD = "unknown_method"
    CONTRACT_MISMATCH = "contract_mismatch"
    DUPLICATE_REQUEST = "duplicate_request"
    STALE_CURSOR = "stale_cursor"
    NOT_SUBSCRIBED = "not_subscribed"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    SHUTTING_DOWN = "shutting_down"
    INTERNAL_ERROR = "internal_error"

    # ---- 领域（与 contracts.agent_v2.errors 一一对应）----
    INVALID_ID = "invalid_id"
    CONTRACT_VIOLATION = "contract_violation"
    THREAD_NOT_FOUND = "thread_not_found"
    TURN_NOT_FOUND = "turn_not_found"
    APPROVAL_NOT_FOUND = "approval_not_found"
    SUMMARY_NOT_FOUND = "summary_not_found"
    ILLEGAL_TURN_TRANSITION = "illegal_turn_transition"
    CONCURRENT_TURN = "concurrent_turn"
    LEASE_ERROR = "lease_error"
    MEMORY_OVERWRITE_DENIED = "memory_overwrite_denied"
    CORRUPTED_EVENT = "corrupted_event"
    MODEL_STREAM_ERROR = "model_stream_error"
    CANCELLED = "cancelled"


#: 领域异常 -> 协议错误码
_DOMAIN_CODES: tuple[tuple[type[AgentV2Error], ErrorCode], ...] = (
    (ContractViolation, ErrorCode.CONTRACT_VIOLATION),
    (IllegalTurnTransition, ErrorCode.ILLEGAL_TURN_TRANSITION),
    (ConcurrentTurnError, ErrorCode.CONCURRENT_TURN),
    (ThreadNotFound, ErrorCode.THREAD_NOT_FOUND),
    (TurnNotFound, ErrorCode.TURN_NOT_FOUND),
    (ApprovalNotFound, ErrorCode.APPROVAL_NOT_FOUND),
    (SummaryNotFound, ErrorCode.SUMMARY_NOT_FOUND),
    (CorruptedEventError, ErrorCode.CORRUPTED_EVENT),
    (LeaseError, ErrorCode.LEASE_ERROR),
    (MemoryOverwriteDenied, ErrorCode.MEMORY_OVERWRITE_DENIED),
    (ModelStreamError, ErrorCode.MODEL_STREAM_ERROR),
)

#: 能安全回给客户端的领域异常私有字段（其余字段可能含内部路径）。
_SAFE_FIELDS: dict[ErrorCode, tuple[str, ...]] = {
    ErrorCode.ILLEGAL_TURN_TRANSITION: ("turn_id", "src", "dst", "allowed"),
    ErrorCode.CORRUPTED_EVENT: ("sequence", "reason", "line_no"),
    ErrorCode.MEMORY_OVERWRITE_DENIED: ("memory_id",),
    ErrorCode.MODEL_STREAM_ERROR: ("error_class",),
}


class ProtocolError(Exception):
    """协议错误：``code`` + 人可读 ``message`` + 结构化 ``data``。"""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.message = message
        self.data: dict[str, Any] = dict(data or {})
        super().__init__(f"[{self.code}] {message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "data": self.data}

    # ---- 常用构造捷径 ----
    @classmethod
    def invalid_params(cls, message: str, **data: Any) -> ProtocolError:
        return cls(ErrorCode.INVALID_PARAMS, message, data=data)

    @classmethod
    def invalid_id(cls, kind: str, value: object, *, param: str | None = None) -> ProtocolError:
        """非法 ID：``kind`` 形如 ``thread`` / ``turn`` / ``call`` / ``approval``。"""
        return cls(
            ErrorCode.INVALID_ID,
            f"{param or kind}: {value!r} is not a valid {kind} id",
            data={"kind": kind, "value": value, "param": param},
        )

    @classmethod
    def stale_cursor(
        cls, requested: int, *, last_sequence: int, reason: str, floor: int = 0
    ) -> ProtocolError:
        return cls(
            ErrorCode.STALE_CURSOR,
            f"cursor {requested} cannot be resumed ({reason}); "
            f"retained range is [{floor + 1}, {last_sequence}]",
            data={
                "requested": requested,
                "last_sequence": last_sequence,
                "floor": floor,
                "reason": reason,
            },
        )

    @classmethod
    def unknown_method(cls, method: str) -> ProtocolError:
        return cls(
            ErrorCode.UNKNOWN_METHOD,
            f"unknown method {method!r}",
            data={"method": method},
        )

    @classmethod
    def shutting_down(cls, method: str | None = None) -> ProtocolError:
        return cls(
            ErrorCode.SHUTTING_DOWN,
            "server is shutting down; no new requests are accepted",
            data={"method": method},
        )


def error_from_agent_v2(exc: AgentV2Error) -> ProtocolError:
    """把领域异常翻译成协议错误（保留安全的结构化字段）。"""
    code = ErrorCode.INTERNAL_ERROR
    for exc_type, mapped in _DOMAIN_CODES:
        if isinstance(exc, exc_type):
            code = mapped
            break
    data: dict[str, Any] = {}
    for name in _SAFE_FIELDS.get(code, ()):
        value = getattr(exc, name, None)
        if value is not None:
            data[name] = value
    if isinstance(exc, IllegalTurnTransition):
        data["turn_id"] = exc.turn_id
        data["from"] = exc.src
        data["to"] = exc.dst
        data["allowed"] = list(exc.allowed)
    return ProtocolError(code, str(exc), data=data)


def error_from_exception(exc: BaseException) -> ProtocolError:
    """任意异常 -> 协议错误。

    ``ProtocolError`` 原样透出；领域异常走映射；其余归为 ``internal_error``
    并保留异常类型名（不泄露 traceback 与内部路径）。
    """
    if isinstance(exc, ProtocolError):
        return exc
    if isinstance(exc, AgentV2Error):
        return error_from_agent_v2(exc)
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return ProtocolError(
            ErrorCode.INVALID_PARAMS,
            f"{type(exc).__name__}: {exc}",
            data={"exception": type(exc).__name__},
        )
    return ProtocolError(
        ErrorCode.INTERNAL_ERROR,
        f"{type(exc).__name__}: {exc}",
        data={"exception": type(exc).__name__},
    )


__all__ = [
    "ErrorCode",
    "ProtocolError",
    "error_from_agent_v2",
    "error_from_exception",
]
