# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""协议层结构化错误。

设计约束（WP-01「错误码至少区分 ……」，验收 §7.2）：

- 每个错误都带**稳定的 ``code``**，客户端据此分支，不解析 message 文本；
- ``message`` 面向用户可读，**绝不**把 Python traceback、内部路径或令牌发出去；
- 领域层（``contracts.agent_v2.errors``）的异常在这里被**翻译**成协议错误；
- 协议错误码是超集：既覆盖领域语义，也覆盖传输 / 会话语义。

错误码与触发点（便于前端分支，也便于验收逐条核对）：

======================================  ==================================================
code                                    什么时候出现
======================================  ==================================================
``invalid_request``                     帧结构不合法 / 不是 JSON / kind 不是 request
``invalid_params``                      参数缺失或多余 / 类型不对 / 变更类缺幂等键
``invalid_id``                          非法 Thread / Turn / Call / Approval / Memory ID
``invalid_state``                       领域状态机不允许的迁移（``data.kind`` 给出细分）
``idempotency_conflict``                同一幂等键或 request_id 配了不同载荷
``cursor_expired``                      游标越过末尾，或落在已重写的历史之前
``permission_denied``                   只读访问面发起变更类请求 / 连接鉴权被拒
``runtime_unavailable``                 运行时未装配或已关闭
``server_shutting_down``                服务关闭中，拒绝新请求
``approval_pending``                    目标 Turn 正在等待审批，需先处理审批
``tool_failed``                          要处理的调用已经以失败结束
``turn_interrupted``                    目标 Turn 已被中断（终态）
``turn_failed``                         目标 Turn 已失败（终态）
``concurrent_turn``                     同一 Thread 上已有活动 Turn
``not_subscribed``                      对未订阅的线程取消订阅
``unknown_method``                      方法不在注册表里
``contract_mismatch`` / ``contract_violation``   版本或结构不符合冻结契约
``corrupted_event`` / ``model_stream_error`` / ``lease_error``
                                         领域异常原样翻译（保留安全的结构化字段）
``memory_overwrite_denied``             自动流程试图覆盖用户编辑过的记忆
``cancelled``                           调用被取消
``internal_error``                      兜底（不含 traceback）
======================================  ==================================================
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
    INVALID_ID = "invalid_id"
    INVALID_STATE = "invalid_state"
    UNKNOWN_METHOD = "unknown_method"
    CONTRACT_MISMATCH = "contract_mismatch"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CURSOR_EXPIRED = "cursor_expired"
    NOT_SUBSCRIBED = "not_subscribed"
    PERMISSION_DENIED = "permission_denied"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    SERVER_SHUTTING_DOWN = "server_shutting_down"
    INTERNAL_ERROR = "internal_error"

    # ---- 领域（与 contracts.agent_v2.errors 一一对应）----
    CONTRACT_VIOLATION = "contract_violation"
    THREAD_NOT_FOUND = "thread_not_found"
    TURN_NOT_FOUND = "turn_not_found"
    APPROVAL_NOT_FOUND = "approval_not_found"
    SUMMARY_NOT_FOUND = "summary_not_found"
    APPROVAL_PENDING = "approval_pending"
    TOOL_FAILED = "tool_failed"
    TURN_INTERRUPTED = "turn_interrupted"
    TURN_FAILED = "turn_failed"
    CONCURRENT_TURN = "concurrent_turn"
    LEASE_ERROR = "lease_error"
    MEMORY_OVERWRITE_DENIED = "memory_overwrite_denied"
    CORRUPTED_EVENT = "corrupted_event"
    MODEL_STREAM_ERROR = "model_stream_error"
    CANCELLED = "cancelled"


#: 领域异常 -> 协议错误码
_DOMAIN_CODES: tuple[tuple[type[AgentV2Error], ErrorCode], ...] = (
    (ContractViolation, ErrorCode.CONTRACT_VIOLATION),
    (IllegalTurnTransition, ErrorCode.INVALID_STATE),
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
    ErrorCode.INVALID_STATE: ("turn_id", "src", "dst", "allowed"),
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

    # ------------------------------------------------------------ 构造捷径
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
    def invalid_state(
        cls, message: str, *, kind: str | None = None, **data: Any
    ) -> ProtocolError:
        """领域状态不允许。``kind`` 给出细分，便于前端区分「Turn 迁移」与其他状态问题。"""
        body: dict[str, Any] = dict(data)
        if kind:
            body["kind"] = kind
        return cls(ErrorCode.INVALID_STATE, message, data=body)

    @classmethod
    def idempotency_conflict(cls, message: str, **data: Any) -> ProtocolError:
        return cls(ErrorCode.IDEMPOTENCY_CONFLICT, message, data=data)

    @classmethod
    def cursor_expired(
        cls, requested: int, *, last_sequence: int, reason: str, floor: int = 0
    ) -> ProtocolError:
        return cls(
            ErrorCode.CURSOR_EXPIRED,
            "这个会话的事件已经变动过，无法从上次位置继续。请刷新后重试。",
            data={
                "requested": requested,
                "last_sequence": last_sequence,
                "floor": floor,
                "reason": reason,
            },
        )

    @classmethod
    def permission_denied(cls, message: str, **data: Any) -> ProtocolError:
        return cls(ErrorCode.PERMISSION_DENIED, message, data=data)

    @classmethod
    def approval_pending(
        cls, *, thread_id: str, turn_id: str, approval_id: str | None = None
    ) -> ProtocolError:
        return cls(
            ErrorCode.APPROVAL_PENDING,
            "这个回合正在等待审批，先完成审批再继续操作。",
            data={"thread_id": thread_id, "turn_id": turn_id, "approval_id": approval_id},
        )

    @classmethod
    def tool_failed(cls, call_id: str, error: dict[str, Any] | None = None) -> ProtocolError:
        return cls(
            ErrorCode.TOOL_FAILED,
            "要处理的工具调用已经失败结束，没有可执行的步骤。",
            data={"call_id": call_id, "error": dict(error or {})},
        )

    @classmethod
    def turn_terminated(
        cls, status: str, *, turn_id: str, **data: Any
    ) -> ProtocolError:
        """Turn 已是终态时的精确错误：中断与失败分开，前端才好给下一步动作。"""
        code = ErrorCode.TURN_FAILED if status == "failed" else ErrorCode.TURN_INTERRUPTED
        message = (
            "这个回合已经失败结束，请重试后继续。"
            if status == "failed"
            else "这个回合已经被中断，先恢复再继续。"
        )
        body = {"turn_id": turn_id, "status": status, **data}
        return cls(code, message, data=body)

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
            ErrorCode.SERVER_SHUTTING_DOWN,
            "服务正在关闭，暂时不接受新请求。",
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
        # 细分标识保留下来：前端可据此区分「Turn 迁移非法」与其他状态问题
        data["kind"] = "illegal_turn_transition"
        data["turn_id"] = exc.turn_id
        data["from"] = exc.src
        data["to"] = exc.dst
        data["allowed"] = list(exc.allowed)
    return ProtocolError(code, str(exc), data=data)


def error_from_exception(exc: BaseException) -> ProtocolError:
    """任意异常 -> 协议错误。

    ``ProtocolError`` 原样透出；领域异常走映射；其余归为 ``internal_error``
    并只保留异常类型名（不泄露 traceback 与内部路径）。
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
        "服务器处理这条请求时出错了，请稍后重试。",
        data={"exception": type(exc).__name__},
    )


__all__ = [
    "ErrorCode",
    "ProtocolError",
    "error_from_agent_v2",
    "error_from_exception",
]
