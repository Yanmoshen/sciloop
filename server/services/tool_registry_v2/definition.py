# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""工具声明与执行上下文（Agent 2 / WP-04）。

契约里的 ``ToolSpec`` 只承载调度必需的四项（name / kind / description / parameters / timeout_s），
而计划书 §4.1 还要求**版本、输入/输出 Schema、权限类别、并行能力、幂等键、输出上限**。
因此这里定义更完整的 :class:`ToolDefinition`，并给出到契约 ``ToolSpec`` 的**单向投影**
（契约不可改，只读使用）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from contracts.agent_v2.enums import ToolKind
from contracts.agent_v2.models import ToolSpec
from services.approval_v2 import ApprovalManager
from services.host_execution_v2 import HostExecutionManager
from services.sandbox_v2 import SandboxManager


class ToolCategory(StrEnum):
    """权限类别（计划书 §4.1 / §5.1）。"""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXECUTION = "execution"
    HIGH_RISK = "high_risk"


class IdempotencyMode(StrEnum):
    """幂等键口径。"""

    #: 不做重复保护（只读、无副作用）
    NONE = "none"
    #: 以 ``call_id`` 为幂等键（同一 Call 只执行一次）
    CALL_ID = "call_id"
    #: 以 ``(thread_id, turn_id, call_id)`` 为幂等键（可跨进程复用执行记录）
    EXECUTION = "execution"


#: 权限类别 → 契约调度类型（只读可并行，其余一律串行）
CONTRACT_KIND: dict[ToolCategory, ToolKind] = {
    ToolCategory.READ_ONLY: ToolKind.READ_ONLY,
    ToolCategory.WORKSPACE_WRITE: ToolKind.SIDE_EFFECT,
    ToolCategory.EXECUTION: ToolKind.SIDE_EFFECT,
    ToolCategory.HIGH_RISK: ToolKind.SIDE_EFFECT,
}

#: 默认输出上限（字节）
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class ToolContext:
    """一次工具执行的上下文（**不来自模型参数**，因此不放审批令牌以外的任何秘密）。"""

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
    category: ToolCategory
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler | None = None
    version: str = "1.0.0"
    timeout_s: float | None = 60.0
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    idempotency: IdempotencyMode = IdempotencyMode.NONE
    #: 声明"允许并行"；最终是否并行由类别决定（只有只读类才真并行）
    parallel: bool = False
    #: 额外元数据（例如 mcp 桥接的来源工具名）
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 投影
    @property
    def kind(self) -> ToolKind:
        return CONTRACT_KIND[self.category]

    @property
    def side_effect(self) -> bool:
        return self.kind is ToolKind.SIDE_EFFECT

    @property
    def parallelizable(self) -> bool:
        return self.parallel and self.category is ToolCategory.READ_ONLY

    def to_spec(self) -> ToolSpec:
        """投影为契约 ``ToolSpec``（供 Agent 1 的调度器使用）。"""
        return ToolSpec(
            name=self.name,
            kind=self.kind,
            description=self.description,
            parameters=dict(self.input_schema),
            timeout_s=self.timeout_s,
        )

    def describe(self) -> dict[str, Any]:
        """权限矩阵 / 文档用的一行描述。"""
        return {
            "name": self.name,
            "version": self.version,
            "category": self.category.value,
            "kind": self.kind.value,
            "parallel": self.parallelizable,
            "side_effect": self.side_effect,
            "idempotency": self.idempotency.value,
            "timeout_s": self.timeout_s,
            "max_output_bytes": self.max_output_bytes,
            "description": self.description,
            "implemented": self.handler is not None,
        }

    def with_handler(self, handler: ToolHandler) -> ToolDefinition:
        """返回替换了 handler 的副本（注入真实后端时用）。"""
        from dataclasses import replace

        return replace(self, handler=handler)


__all__ = [
    "ToolCategory",
    "IdempotencyMode",
    "CONTRACT_KIND",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "ToolContext",
    "ToolDefinition",
    "ToolHandler",
]
