# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""审批领域模型（Agent 2 / WP-03）。

冻结契约的 ``ApprovalRequest`` 字段较少（action 是自由字典），而文档要求审批请求带齐
``tool_name / normalized_argv / cwd / target_paths / risk_categories / requested_scope /
created_at / expires_at / status``。这里定义 :class:`ApprovalView` 作为**对外的完整视图**，
并从契约对象派生，保证与冻结 Schema 共存而不修改它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from contracts.agent_v2.enums import ApprovalStatus
from contracts.agent_v2.models import ApprovalRequest

from .rules import CommandAnalysis, RiskCategory, RiskLevel


class DecisionScope(StrEnum):
    """裁决作用域。"""

    ONCE = "once"
    THREAD = "thread"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    ESCALATED = "escalated_full_access"


@dataclass(frozen=True)
class RiskAssessment:
    """一次「要不要打扰研究者」的判定结论。"""

    required: bool
    risk: str
    level: RiskLevel
    categories: tuple[str, ...]
    reasons: tuple[str, ...]
    summary: str
    action: dict[str, Any]
    allowlisted: bool = False
    auto_approved: bool = False
    matched_grant: dict[str, Any] | None = None
    escalation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "risk": self.risk,
            "level": self.level.value,
            "categories": list(self.categories),
            "reasons": list(self.reasons),
            "summary": self.summary,
            "action": dict(self.action),
            "allowlisted": self.allowlisted,
            "auto_approved": self.auto_approved,
            "matched_grant": self.matched_grant,
            "escalation": self.escalation,
        }

    def has_category(self, category: RiskCategory | str) -> bool:
        value = category.value if isinstance(category, RiskCategory) else str(category)
        return value in self.categories


@dataclass
class ApprovalView:
    """审批请求的完整视图（UI / 执行层 / 审计共用同一个 approval_id）。"""

    approval_id: str
    thread_id: str
    turn_id: str
    status: str
    tool_name: str
    created_at: str
    call_id: str | None = None
    normalized_argv: tuple[str, ...] = ()
    executable: str | None = None
    cwd: str | None = None
    target_paths: tuple[str, ...] = ()
    risk_categories: tuple[str, ...] = ()
    requested_scope: str = DecisionScope.ONCE.value
    expires_at: str | None = None
    risk: str = "unknown"
    summary: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None
    decision_scope: str | None = None
    action: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 派生
    @property
    def pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "normalized_argv": list(self.normalized_argv),
            "executable": self.executable,
            "cwd": self.cwd,
            "target_paths": list(self.target_paths),
            "risk_categories": list(self.risk_categories),
            "requested_scope": self.requested_scope,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "risk": self.risk,
            "summary": self.summary,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_scope": self.decision_scope,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalView:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in data.items() if key in known}
        payload.setdefault("approval_id", "")
        payload.setdefault("thread_id", "")
        payload.setdefault("turn_id", "")
        payload.setdefault("status", ApprovalStatus.PENDING.value)
        payload.setdefault("tool_name", "")
        payload.setdefault("created_at", "")
        for key in ("normalized_argv", "target_paths", "risk_categories"):
            payload[key] = tuple(payload.get(key) or ())
        return cls(**payload)  # type: ignore[arg-type]

    @classmethod
    def from_contract(
        cls, request: ApprovalRequest, *, expires_at: str | None = None, summary: str | None = None
    ) -> ApprovalView:
        """从冻结契约对象派生（action 里携带工具/命令/风险细节）。"""
        action = dict(request.action or {})
        arguments = action.get("arguments")
        nested = arguments if isinstance(arguments, dict) else {}
        argv = action.get("normalized_argv") or action.get("argv") or nested.get("argv") or ()
        return cls(
            approval_id=request.approval_id,
            thread_id=request.thread_id,
            turn_id=request.turn_id,
            call_id=request.call_id,
            status=request.status.value if hasattr(request.status, "value") else str(request.status),
            tool_name=str(action.get("tool") or action.get("tool_name") or "unknown"),
            normalized_argv=tuple(str(item) for item in argv),
            executable=action.get("executable") or (str(argv[0]) if argv else None),
            cwd=action.get("cwd") or nested.get("cwd"),
            target_paths=tuple(str(item) for item in (action.get("targets") or ())),
            risk_categories=tuple(str(item) for item in (action.get("risk_categories") or ())),
            requested_scope=str(action.get("requested_scope") or DecisionScope.ONCE.value),
            created_at=request.created_at,
            expires_at=expires_at or action.get("expires_at"),
            risk=request.risk,
            summary=summary or action.get("summary"),
            decided_at=request.decided_at,
            decided_by=request.decided_by,
            decision_scope=request.decision_scope,
            action=action,
        )


def analysis_view(analysis: CommandAnalysis) -> dict[str, Any]:
    """命令分析 → 审批请求的 action 载荷。"""
    return {
        "kind": "command",
        "argv": list(analysis.normalized_argv),
        "normalized_argv": list(analysis.normalized_argv),
        "executable": analysis.executable,
        "cwd": analysis.cwd,
        "targets": list(analysis.targets),
        "risk_categories": [item.value for item in analysis.categories],
        "risk_level": analysis.level.value,
        "prefix": list(analysis.prefix),
        "unknown_command": analysis.unknown_command,
    }


__all__ = [
    "DecisionScope",
    "RiskAssessment",
    "ApprovalView",
    "analysis_view",
    "RiskCategory",
    "RiskLevel",
]
