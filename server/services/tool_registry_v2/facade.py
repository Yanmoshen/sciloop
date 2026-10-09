# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""跨线接口门面（Agent 2 / 文档 §5）。

对 **Agent 1** 承诺的协议（不暴露内部类）：

    evaluate(call, facts) -> Decision
    execute(call, context) -> ToolResult
    cancel(call_id) -> None
    approval_events() -> EventStream

对 **Agent 3** 提供的接口（无需 UI 存在）:

    list_tools() -> ToolDefinition[]
    get_approval(approval_id) -> ApprovalView
    resolve_approval(command) -> ApprovalResult
    interrupt_execution(call_id) -> CommandResult

所有方法都可以用 fake client 调用；本模块不读前端状态、不依赖任何 UI。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.models import ToolCall, ToolResult
from services.approval_v2 import ApprovalManager, ApprovalView, DecisionScope
from services.host_execution_v2 import HostExecutionManager
from services.sandbox_v2 import SandboxManager

from .models import PermissionClass
from .registry import ToolRegistry

#: 裁决取值（与 Agent 1 的门一致）
ALLOW = "allow"
REQUIRE = "require"


class ApprovalEventStream:
    """审批事件流（内存环形缓冲 + 订阅）。"""

    def __init__(self, *, limit: int = 200) -> None:
        self.limit = limit
        self._events: list[dict[str, Any]] = []
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._sequence = 0

    def publish(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            event = {"sequence": self._sequence, "type": kind, "payload": payload}
            self._events.append(event)
            if len(self._events) > self.limit:
                self._events = self._events[-self.limit :]
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - 订阅者异常不影响主流程
                continue
        return event

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock, __import__("contextlib").suppress(ValueError):
                self._subscribers.remove(callback)

        return unsubscribe

    def drain(self, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [item for item in self._events if item["sequence"] > after_sequence]

    def list(self) -> list[dict[str, Any]]:
        return self.drain()


class ToolGateway:
    """工具线的唯一对外门面。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        sandbox: SandboxManager | None = None,
        approvals: ApprovalManager | None = None,
        host: HostExecutionManager | None = None,
        events: ApprovalEventStream | None = None,
    ) -> None:
        self.registry = registry
        self.sandbox = sandbox if sandbox is not None else registry.sandbox
        self.approvals = approvals if approvals is not None else registry.approvals
        self.host = host if host is not None else registry.host
        self.events = events or ApprovalEventStream()
        self._cancels: dict[str, CancelToken] = {}
        if self.approvals is not None and self.approvals._on_event is None:  # noqa: SLF001 - 接线
            self.approvals._on_event = self.events.publish  # noqa: SLF001

    # ================================================================== Agent 1 侧
    def evaluate(self, call: ToolCall, facts: dict[str, Any] | None = None) -> dict[str, Any]:
        """判定一个 Call 能否直接执行（**不执行**）。

        :param facts: 可选的外部事实（例如当前 sandbox mode），仅用于回显。
        """
        verdict = self.registry.approval_requirement(call, thread_id=call.thread_id)
        decision = ALLOW if not verdict.get("required") else REQUIRE
        if verdict.get("denied"):
            decision = "deny"
        return {
            "decision": decision,
            "required": bool(verdict.get("required")),
            "denied": bool(verdict.get("denied")),
            "reason": verdict.get("reason") or verdict.get("summary") or "",
            "risk": verdict.get("risk"),
            "categories": list(verdict.get("categories") or []),
            "escalation": verdict.get("escalation"),
            "verdict": verdict.get("verdict"),
            "facts": dict(facts or {}),
        }

    async def execute(
        self,
        call: ToolCall,
        context: dict[str, Any] | None = None,
        *,
        cancel: CancelToken | None = None,
    ) -> ToolResult:
        """执行一个 Call（取消令牌可选；调用方可用 ``cancel()`` 主动取消）。"""
        token = cancel
        if token is None:
            existing = self._cancels.get(call.call_id)
            if existing is None:
                existing = CancelToken()
                self._cancels[call.call_id] = existing
            token = existing
        del context
        try:
            return await self.registry.execute(call, token)
        finally:
            self._cancels.pop(call.call_id, None)

    def cancel(self, call_id: str, *, reason: str = "cancelled") -> None:
        """向正在执行的 Call 发取消信号（幂等）。"""
        token = self._cancels.get(call_id)
        if token is not None:
            token.cancel(reason)

    def approval_events(self) -> ApprovalEventStream:
        return self.events

    def sandbox_facts(self, *, thread_id: str | None = None) -> dict[str, Any]:
        if self.sandbox is None:
            return {}
        prefixes = (
            self.approvals.grants.prefixes(scope_id=thread_id)
            if self.approvals is not None and thread_id
            else []
        )
        return self.sandbox.facts(approved_prefixes=prefixes).to_dict()

    # ================================================================== Agent 3 侧
    def list_tools(self) -> list[dict[str, Any]]:
        """工具清单（含权限类别、副作用、并行、幂等、超时、输出上限）。"""
        return [item.to_dict() for item in self.registry.definitions()]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        if self.approvals is None:
            return None
        view = self.approvals.view(approval_id)
        return None if view is None else view.to_dict()

    def pending_approvals(self, *, thread_id: str | None = None) -> list[dict[str, Any]]:
        if self.approvals is None:
            return []
        return [item.to_dict() for item in self.approvals.pending(thread_id=thread_id)]

    def resolve_approval(self, command: dict[str, Any]) -> dict[str, Any]:
        """UI 裁决入口。

        ``command`` 形如 ``{"approval_id": ..., "decision": "approve_once" |
        "approve_for_thread" | "deny" | "cancel" | "expire" |
        "escalate_full_access", "by": "owner", ...}``
        """
        if self.approvals is None:
            return {"ok": False, "error": "approvals_not_configured"}
        approval_id = str(command.get("approval_id") or "")
        decision = str(command.get("decision") or "").strip()
        by = str(command.get("by") or "owner")
        try:
            if decision == "approve_once":
                view = self.approvals.approve_once(approval_id, by=by)
                ok = True
            elif decision == "approve_for_thread":
                view, _grant = self.approvals.approve_for_thread(
                    approval_id,
                    by=by,
                    scope_id=command.get("scope_id"),
                    arg_mode=str(command.get("arg_mode") or "exact"),
                    max_extra_args=int(command.get("max_extra_args") or 2),
                )
                ok = True
            elif decision == "deny":
                view, ok = self.approvals.deny(approval_id, by=by), True
            elif decision == "cancel":
                view = self.approvals.cancel(
                    approval_id, reason=str(command.get("reason") or "ui_cancel"), by=by
                )
                ok = True
            elif decision == "expire":
                view, ok = self.approvals.expire(approval_id), True
            elif decision == "escalate_full_access":
                view = self.approvals.escalate_full_access(approval_id, by=by, sandbox=self.sandbox)
                ok = True
            else:
                return {"ok": False, "error": f"unknown decision {decision!r}"}
        except Exception as exc:  # noqa: BLE001 - UI 调用必须拿到结构化错误
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result: dict[str, Any] = {"ok": ok, "decision": decision, "approval": view.to_dict()}
        if decision == "approve_once":
            result["released_call_id"] = view.call_id
        if decision == "escalate_full_access":
            result["sandbox_mode"] = self.sandbox.policy.value if self.sandbox else None
        return result

    def interrupt_execution(
        self, call_id: str, *, reason: str = "ui_interrupt", thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """中断某个 Call 的宿主机执行（终止进程树）。"""
        self.cancel(call_id, reason=reason)
        if self.host is None:
            return {"ok": False, "error": "host_not_configured", "call_id": call_id}
        if thread_id and turn_id:
            killed = self.host.interrupt_turn(thread_id, turn_id, reason=reason)
            return {"ok": True, "call_id": call_id, "killed_executions": killed}
        record = self.host.find(thread_id=thread_id, turn_id=turn_id, call_id=call_id)
        if record is None:
            return {"ok": False, "error": "execution_not_found", "call_id": call_id}
        return {
            "ok": self.host.interrupt_execution(record.execution_id, reason=reason),
            "call_id": call_id,
            "execution_id": record.execution_id,
        }

    # ================================================================== 证据
    def permission_summary(self) -> dict[str, Any]:
        """交付证据：权限矩阵 + 类别分布 + 副作用分布 + 平台能力。"""
        return {
            "tools": self.registry.permission_matrix(),
            "by_permission_class": self.registry.permission_class_matrix(),
            "by_side_effect": self.registry.capability_matrix(),
            "schemas_valid": self.registry.validated_schemas(),
            "sandbox": self.sandbox.summary() if self.sandbox else None,
            "capability": self.sandbox.capability().to_dict() if self.sandbox else None,
            "permission_classes": [member.value for member in PermissionClass],
            "decision_scopes": [member.value for member in DecisionScope],
        }


__all__ = ["ToolGateway", "ApprovalEventStream", "ALLOW", "REQUIRE", "ApprovalView"]
