# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""沙箱策略与裁决值对象（Agent 2 / WP-05）。

计划书 §4.3 的三种策略在这里定型，裁决结果统一用 :class:`AccessVerdict` 表达，
让「路径越界」这件事**只有一个判定口径**（工具入口、审批规则、宿主执行共用）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SandboxPolicy(StrEnum):
    """Codex 风格沙箱策略。"""

    #: 显式兼容能力：任何写入都拒绝（不是默认测试模式）
    READ_ONLY = "read-only"
    #: 选中工作区可读写执行，工作区外只读
    WORKSPACE_WRITE = "workspace-write"
    #: 宿主机任意目录访问
    DANGER_FULL_ACCESS = "danger-full-access"


#: 默认策略（计划书 §4.3：``workspace-write`` 是常态）
DEFAULT_POLICY = SandboxPolicy.WORKSPACE_WRITE
#: 测试基线策略——**刻意不是** ``read-only``：
#: 计划书要求只读只是显式兼容能力，测试必须覆盖工作区可写这条主路径。
TEST_POLICY = SandboxPolicy.WORKSPACE_WRITE


class AccessKind(StrEnum):
    """访问意图。"""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"


class SandboxDecision(StrEnum):
    """裁决结果。"""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


#: 严重程度：合并多个裁决时取最严（deny > require_approval > allow）
_SEVERITY: dict[SandboxDecision, int] = {
    SandboxDecision.ALLOW: 0,
    SandboxDecision.REQUIRE_APPROVAL: 1,
    SandboxDecision.DENY: 2,
}


@dataclass(frozen=True)
class AccessVerdict:
    """一次路径访问裁决。"""

    decision: SandboxDecision
    access: AccessKind
    reason: str
    path: str | None = None
    inside_workspace: bool = False
    #: 命中敏感文件（凭据 / 版本库元数据）
    protected: bool = False
    #: 完全访问模式下的"仍然记账"标记
    audited: bool = False
    policy: str = DEFAULT_POLICY

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
            "inside_workspace": self.inside_workspace,
            "protected": self.protected,
            "audited": self.audited,
            "policy": self.policy,
        }

    @staticmethod
    def strictest(verdicts: Iterable[AccessVerdict]) -> AccessVerdict:
        """取最严的一条裁决（并列时保留更靠前的那条，便于测试定位）。"""
        ordered = list(verdicts)
        if not ordered:
            raise ValueError("strictest() requires at least one verdict")
        best = ordered[0]
        for verdict in ordered[1:]:
            if _SEVERITY[verdict.decision] > _SEVERITY[best.decision]:
                best = verdict
        return best


__all__ = [
    "SandboxPolicy",
    "DEFAULT_POLICY",
    "TEST_POLICY",
    "AccessKind",
    "SandboxDecision",
    "AccessVerdict",
]
