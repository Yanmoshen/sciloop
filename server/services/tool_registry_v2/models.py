# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""工具元数据模型（Agent 2 / WP-01）。

文档要求每个工具声明：

    name、version、description
    input_schema、output_schema
    permission_class：read / workspace_write / exec / dangerous
    side_effect：none / filesystem / process / network / external
    parallel_safe、idempotent、timeout_ms、max_output_bytes
    cancellation_support、audit_fields

冻结契约里的 ``ToolSpec`` 只承载调度必需的四项（name/kind/description/parameters/timeout_s），
因此这里定义更完整的 :class:`ToolDefinition`，并给出到契约 ``ToolSpec`` 的**单向投影**
（契约只读，不改）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from contracts.agent_v2.enums import ToolKind
from contracts.agent_v2.models import ToolSpec
from services.approval_v2 import ApprovalManager
from services.host_execution_v2 import HostExecutionManager
from services.sandbox_v2 import SandboxManager


class PermissionClass(StrEnum):
    """权限类别（文档 WP-01 的四个封闭取值）。"""

    READ = "read"
    WORKSPACE_WRITE = "workspace_write"
    EXEC = "exec"
    DANGEROUS = "dangerous"


class SideEffect(StrEnum):
    """副作用的性质（用于调度、审批与审计归类）。"""

    NONE = "none"
    FILESYSTEM = "filesystem"
    PROCESS = "process"
    NETWORK = "network"
    EXTERNAL = "external"


class IdempotencyMode(StrEnum):
    """幂等键口径。"""

    #: 不做重复保护（只读、无副作用）
    NONE = "none"
    #: 以 ``call_id`` 为幂等键（同一 Call 只执行一次）
    CALL_ID = "call_id"
    #: 以 ``(thread_id, turn_id, call_id)`` 为幂等键（可跨进程复用执行记录）
    EXECUTION = "execution"


#: 权限类别 → 契约调度类型（只读可并行，其余一律串行）
CONTRACT_KIND: dict[PermissionClass, ToolKind] = {
    PermissionClass.READ: ToolKind.READ_ONLY,
    PermissionClass.WORKSPACE_WRITE: ToolKind.SIDE_EFFECT,
    PermissionClass.EXEC: ToolKind.SIDE_EFFECT,
    PermissionClass.DANGEROUS: ToolKind.SIDE_EFFECT,
}

#: 允许并行的副作用性质：网络读取仍然是无副作用的"读"，可与其它只读并行
PARALLELIZABLE_SIDE_EFFECTS: frozenset[SideEffect] = frozenset(
    {SideEffect.NONE, SideEffect.NETWORK}
)

#: 需要审批的权限类别（普通模式；完全访问由 ApprovalManager 决定）
APPROVAL_CLASSES: frozenset[PermissionClass] = frozenset(
    {PermissionClass.EXEC, PermissionClass.DANGEROUS}
)

#: 默认输出上限（字节）
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
#: 默认超时（毫秒）
DEFAULT_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class ToolContext:
    """一次工具执行的上下文（**不来自模型参数**）。"""

    thread_id: str
    turn_id: str
    call_id: str
    cwd: Path
    sandbox: SandboxManager | None = None
    host: HostExecutionManager | None = None
    approvals: ApprovalManager | None = None
    agent_tree: Any | None = None
    services: dict[str, Any] = field(default_factory=dict)

    def service(self, key: str) -> Any:
        return self.services.get(key)


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    """工具声明（比契约 ``ToolSpec`` 更完整；投影后交给运行时调度）。"""

    name: str
    permission: PermissionClass
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler | None = None
    version: str = "1.0.0"
    side_effect: SideEffect = SideEffect.NONE
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    idempotency: IdempotencyMode = IdempotencyMode.NONE
    #: 声明"允许并行"；最终是否并行由**权限类别 + 副作用**共同决定
    parallel_safe: bool = False
    #: 是否支持取消令牌（宿主命令为 True）
    cancellation_support: bool = True
    #: 审计时必留的参数/结果字段名
    audit_fields: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 派生
    @property
    def permission_class(self) -> PermissionClass:
        """文档里的字段名（与 :attr:`permission` 同义）。"""
        return self.permission

    @property
    def kind(self) -> ToolKind:
        return CONTRACT_KIND[self.permission]

    @property
    def side_effect_name(self) -> str:
        return self.side_effect.value

    @property
    def has_side_effect(self) -> bool:
        return self.side_effect is not SideEffect.NONE

    @property
    def idempotent(self) -> bool:
        return self.idempotency is not IdempotencyMode.NONE

    @property
    def timeout_s(self) -> float | None:
        return None if self.timeout_ms is None else float(self.timeout_ms) / 1000.0

    @property
    def parallelizable(self) -> bool:
        """只有「只读 + 声明并行 + 副作用是 none/network」三者同时成立才允许并行。"""
        return (
            self.parallel_safe
            and self.permission is PermissionClass.READ
            and self.side_effect in PARALLELIZABLE_SIDE_EFFECTS
        )

    @property
    def needs_approval_by_class(self) -> bool:
        return self.permission in APPROVAL_CLASSES

    # ------------------------------------------------------------------ 投影
    def to_spec(self) -> ToolSpec:
        """投影为契约 ``ToolSpec``（供 Agent 1 的调度器使用）。"""
        return ToolSpec(
            name=self.name,
            kind=self.kind,
            description=self.description,
            parameters=dict(self.input_schema),
            timeout_s=self.timeout_s,
        )

    def to_dict(self) -> dict[str, Any]:
        """完整元数据（交付证据 / 权限矩阵 / Agent 3 的 list_tools）。"""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "permission_class": self.permission.value,
            "kind": self.kind.value,
            "side_effect": self.side_effect.value,
            "parallel_safe": self.parallel_safe,
            "parallelizable": self.parallelizable,
            "idempotent": self.idempotent,
            "idempotency": self.idempotency.value,
            "timeout_ms": self.timeout_ms,
            "max_output_bytes": self.max_output_bytes,
            "cancellation_support": self.cancellation_support,
            "audit_fields": list(self.audit_fields),
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "implemented": self.handler is not None,
            "metadata": dict(self.metadata),
        }

    def describe(self) -> dict[str, Any]:
        """权限矩阵用的一行描述（不含 schema，便于打表）。"""
        return {
            key: value
            for key, value in self.to_dict().items()
            if key not in {"input_schema", "output_schema", "metadata"}
        }

    def with_handler(self, handler: ToolHandler) -> ToolDefinition:
        """返回替换了 handler 的副本（注入真实后端时用）。"""
        return replace(self, handler=handler)


__all__ = [
    "PermissionClass",
    "SideEffect",
    "IdempotencyMode",
    "CONTRACT_KIND",
    "APPROVAL_CLASSES",
    "PARALLELIZABLE_SIDE_EFFECTS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
]
