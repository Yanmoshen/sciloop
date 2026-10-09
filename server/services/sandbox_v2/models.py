# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""沙箱裁决与平台能力模型（Agent 2 / WP-02）。

裁决结果统一用 :class:`AccessVerdict` 表达，并按文档要求带齐
``reason`` / ``matched_root`` / ``resolved_paths``，让「路径是否越界」只有一个判定口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .policy import AccessKind, SandboxDecision


@dataclass(frozen=True)
class AccessVerdict:
    """一次路径 / 命令访问的裁决。"""

    decision: SandboxDecision
    access: AccessKind
    reason: str
    path: str | None = None
    #: 命中的边界根（workspace / 额外可写根 / 额外可读根）；区外为 None
    matched_root: str | None = None
    #: 规范化（realpath + 大小写/分隔符归一）后的路径，含"父目录已解析"的结果
    resolved_paths: tuple[str, ...] = ()
    inside_workspace: bool = False
    #: 命中敏感目标（凭据 / 版本库元数据 / 密钥）
    protected: bool = False
    #: 命中哪条敏感规则（可配置规则的 id）
    protected_rule: str | None = None
    #: 完全访问模式下的"仍然记账"标记
    audited: bool = False
    policy: str = "workspace-write"
    #: 可疑命令升级到审批的原因（不是启发式路径识别的替代品，而是补强）
    escalation: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is SandboxDecision.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.decision is SandboxDecision.REQUIRE_APPROVAL

    @property
    def denied(self) -> bool:
        return self.decision is SandboxDecision.DENY

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "access": self.access.value,
            "reason": self.reason,
            "path": self.path,
            "matched_root": self.matched_root,
            "resolved_paths": list(self.resolved_paths),
            "inside_workspace": self.inside_workspace,
            "protected": self.protected,
            "protected_rule": self.protected_rule,
            "audited": self.audited,
            "policy": self.policy,
            "escalation": self.escalation,
        }


#: 严重程度：合并多个裁决时取最严（deny > require_approval > allow）
SEVERITY: dict[SandboxDecision, int] = {
    SandboxDecision.ALLOW: 0,
    SandboxDecision.REQUIRE_APPROVAL: 1,
    SandboxDecision.DENY: 2,
}


@dataclass
class CommandVerdict:
    """一次命令检查的结果（cwd + 参数路径 + 重定向 + 升级）。"""

    cwd: str
    verdict: AccessVerdict
    entries: list[AccessVerdict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "verdict": self.verdict.to_dict(),
            "paths": [item.to_dict() for item in self.entries],
        }


@dataclass(frozen=True)
class PlatformCapability:
    """平台隔离能力探测结果（**不能伪造**）。"""

    platform: str
    #: 是否具备"系统级"强隔离能力（ACL / landlock / seccomp / namespace 等）
    strong_isolation: bool
    mechanisms: tuple[str, ...]
    notes: tuple[str, ...] = ()
    #: 策略层能力（路径边界 + 审批）始终可用
    policy_layer: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "strong_isolation": self.strong_isolation,
            "policy_layer": self.policy_layer,
            "mechanisms": list(self.mechanisms),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ProtectedRule:
    """敏感目标规则（可配置 + 带审计原因）。"""

    rule_id: str
    kind: str  # dir | file | suffix | prefix
    pattern: str
    deny_write: bool = True
    deny_read: bool = False
    reason: str = "敏感目标"

    def matches(self, path_lower_parts: tuple[str, ...], name_lower: str) -> bool:
        if self.kind == "dir":
            return self.pattern in path_lower_parts
        if self.kind == "file":
            return name_lower == self.pattern
        if self.kind == "suffix":
            return name_lower.endswith(self.pattern)
        if self.kind == "prefix":
            return name_lower.startswith(self.pattern)
        return False


__all__ = [
    "AccessVerdict",
    "CommandVerdict",
    "PlatformCapability",
    "ProtectedRule",
    "SEVERITY",
]
