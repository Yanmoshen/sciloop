# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""ToolRegistry：工具声明、参数校验与统一执行边界（Agent 2 / WP-04）。

本类就是契约里 :class:`contracts.agent_v2.fake.ToolExecutor` 的**实现**：

    def specs() -> Sequence[ToolSpec]
    def spec(name) -> ToolSpec | None
    async def execute(call, cancel) -> ToolResult

执行流程（顺序固定，测试据此断言）：

1. 未知工具 → ``invalid_arguments``（不调用任何 handler）；
2. **参数校验**（官方 ``jsonschema``，见 ``contracts.agent_v2.validate``）→
   非法参数在执行前被拒绝，错误信息可读；
3. 取消令牌已触发 → ``cancelled``；
4. 沙箱/审批前置判定 → 被拒时返回结构化错误（不抛异常）；
5. handler 执行（带超时）→ 成功 / 失败 / 超时；
6. 输出按 ``max_output_bytes`` **截断并如实标记**；
7. 结果始终带原始 ``call_id``。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from typing import Any

from contracts.agent_v2.cancellation import CancelledError, CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ToolCallStatus
from contracts.agent_v2.models import ToolCall, ToolResult, ToolSpec
from contracts.agent_v2.validate import SchemaValidator
from services.approval_v2 import ApprovalManager
from services.host_execution_v2 import HostExecutionManager
from services.sandbox_v2 import AccessKind, SandboxManager

from .definition import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ToolCategory,
    ToolContext,
    ToolDefinition,
)


class ToolRegistry:
    """工具注册表 + 执行器。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        sandbox: SandboxManager | None = None,
        host: HostExecutionManager | None = None,
        approvals: ApprovalManager | None = None,
        agent_tree: Any | None = None,
        services: dict[str, Any] | None = None,
        default_cwd: str | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        validate_output: bool = True,
    ) -> None:
        self.clock = clock or SystemClock()
        self.sandbox = sandbox
        self.host = host
        self.approvals = approvals
        self.agent_tree = agent_tree
        self.services: dict[str, Any] = dict(services or {})
        self.default_cwd = default_cwd
        self.max_output_bytes = int(max_output_bytes)
        self.validate_output = bool(validate_output)
        self._definitions: dict[str, ToolDefinition] = {}
        self._validators: dict[str, SchemaValidator] = {}

    # ------------------------------------------------------------------ 注册
    def register(self, definition: ToolDefinition, *, replace: bool = False) -> ToolDefinition:
        if definition.name in self._definitions and not replace:
            raise ValueError(f"tool {definition.name!r} is already registered")
        if not definition.name:
            raise ValueError("tool name must not be empty")
        self._definitions[definition.name] = definition
        self._validators[definition.name] = SchemaValidator(definition.input_schema)
        return definition

    def register_all(
        self, definitions: Sequence[ToolDefinition], *, replace: bool = False
    ) -> list[ToolDefinition]:
        return [self.register(item, replace=replace) for item in definitions]

    def unregister(self, name: str) -> bool:
        self._validators.pop(name, None)
        return self._definitions.pop(name, None) is not None

    def bind(self, **services: Any) -> ToolRegistry:
        """注入后端实现（搜索 / 知识库 / 技能 / MCP 等），返回自身便于链式调用。"""
        for key, value in services.items():
            self.services[key] = value
        return self

    # ------------------------------------------------------------------ 查询
    def names(self) -> list[str]:
        return sorted(self._definitions)

    def definitions(self) -> list[ToolDefinition]:
        return [self._definitions[name] for name in self.names()]

    def definition(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def category_of(self, name: str) -> ToolCategory:
        definition = self._definitions.get(name)
        # 未知工具按高危处理：宁可要审批，也不要猜它无害
        return definition.category if definition else ToolCategory.HIGH_RISK

    def specs(self) -> list[ToolSpec]:
        return [item.to_spec() for item in self.definitions()]

    def spec(self, name: str) -> ToolSpec | None:
        definition = self._definitions.get(name)
        return None if definition is None else definition.to_spec()

    def permission_matrix(self) -> list[dict[str, Any]]:
        """权限矩阵（交付证据之一）。"""
        return [item.describe() for item in self.definitions()]

    def category_matrix(self) -> dict[str, list[str]]:
        matrix: dict[str, list[str]] = {category.value: [] for category in ToolCategory}
        for definition in self.definitions():
            matrix[definition.category.value].append(definition.name)
        return matrix

    # ------------------------------------------------------------------ 校验
    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> list[str]:
        """返回参数错误列表（空 = 合法）。未知工具返回 unknown_tool。"""
        validator = self._validators.get(name)
        if validator is None:
            return [f"tool {name!r} is not registered"]
        return validator.errors(arguments or {})

    # ------------------------------------------------------------------ 执行
    async def execute(
        self, call: ToolCall, cancel: CancelToken | None = None
    ) -> ToolResult:
        started_iso = self.clock.now_iso()
        started_monotonic = time.monotonic()

        def finish(
            status: ToolCallStatus,
            *,
            output: dict[str, Any] | None = None,
            error: dict[str, Any] | None = None,
        ) -> ToolResult:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=status,
                output=output,
                error=error,
                duration_ms=int((time.monotonic() - started_monotonic) * 1000),
                started_at=started_iso,
                finished_at=self.clock.now_iso(),
            )

        definition = self._definitions.get(call.name)
        if definition is None:
            return finish(
                ToolCallStatus.INVALID_ARGUMENTS,
                error={
                    "code": "unknown_tool",
                    "message": f"tool {call.name!r} is not registered",
                    "registered": self.names(),
                },
            )

        problems = self.validate_arguments(call.name, dict(call.arguments or {}))
        if problems:
            return finish(
                ToolCallStatus.INVALID_ARGUMENTS,
                error={
                    "code": "invalid_arguments",
                    "message": "参数不符合工具 Schema",
                    "problems": sorted(problems)[:8],
                },
            )

        if cancel is not None and cancel.cancelled:
            return finish(
                ToolCallStatus.CANCELLED,
                error={"code": "cancelled", "message": cancel.reason or "cancelled before execution"},
            )

        if definition.handler is None:
            return finish(
                ToolCallStatus.FAILED,
                error={
                    "code": "not_implemented",
                    "message": f"tool {call.name!r} has no handler bound",
                },
            )

        context = self._context_for(call)
        try:
            if definition.timeout_s is not None:
                raw_output = await asyncio.wait_for(
                    definition.handler(dict(call.arguments or {}), context),
                    timeout=float(definition.timeout_s),
                )
            else:
                raw_output = await definition.handler(dict(call.arguments or {}), context)
        except TimeoutError:
            return finish(
                ToolCallStatus.TIMEOUT,
                error={
                    "code": "timeout",
                    "message": f"tool {call.name!r} exceeded {definition.timeout_s}s",
                    "timeout_s": definition.timeout_s,
                },
            )
        except asyncio.CancelledError:
            # 异步取消必须原样传播（与 Agent 1 的调度器口径一致）
            raise
        except CancelledError:
            return finish(
                ToolCallStatus.CANCELLED,
                error={"code": "cancelled", "message": "cancelled during execution"},
            )
        except Exception as exc:  # noqa: BLE001 - 工具崩溃必须回喂模型而不是中断循环
            return finish(
                ToolCallStatus.FAILED,
                error={"code": "tool_error", "message": f"{type(exc).__name__}: {exc}"},
            )

        output = dict(raw_output or {})
        if output.get("__error__"):
            error = dict(output.pop("__error__"))
            output.pop("__error__", None)
            return finish(ToolCallStatus.FAILED, output=output or None, error=error)

        output, truncated = self._enforce_output_limit(output, definition)
        if truncated:
            output["truncated"] = True
            output["truncation_notice"] = (
                f"输出超过 {definition.max_output_bytes} 字节，已截断（完整内容请查看执行记录）"
            )

        if self.validate_output and definition.output_schema:
            problems = SchemaValidator(definition.output_schema).errors(output)
            if problems:
                return finish(
                    ToolCallStatus.FAILED,
                    output=output,
                    error={
                        "code": "invalid_output",
                        "message": "工具输出不符合声明 Schema",
                        "problems": sorted(problems)[:8],
                    },
                )

        return finish(ToolCallStatus.SUCCEEDED, output=output)

    # ------------------------------------------------------------------ 审批门
    def approval_requirement(self, call: ToolCall, *, thread_id: str) -> dict[str, Any]:
        """判断一个调用是否需要研究者批准（**参数感知**，不只看工具名）。

        口径：

        - 只读类 → 不需要；
        - ``host.exec`` / ``skill.run`` → 走命令风险分析（含持续批准与完全访问）；
        - ``host.file.*`` → 沙箱裁决 + 覆盖/删除判定；
        - 其余 → 按权限类别（``execution`` / ``high_risk`` 需要）。
        """
        definition = self._definitions.get(call.name)
        arguments = dict(call.arguments or {})
        if definition is None:
            return {"required": True, "reason": f"未知工具 {call.name!r}", "risk": "high_risk"}
        if definition.category is ToolCategory.READ_ONLY:
            return {"required": False, "reason": "只读工具自动执行", "risk": "read_only"}

        approvals = self.approvals
        full_access = bool(approvals is not None and approvals.full_access)

        if call.name == "host.exec":
            argv = [str(item) for item in (arguments.get("argv") or [])]
            cwd = arguments.get("cwd") or str(self.default_cwd or (self.sandbox.workspace_root if self.sandbox else "."))
            sandbox_reason = None
            sandbox_verdict: dict[str, Any] | None = None
            if self.sandbox is not None and argv:
                check = self.sandbox.check_argv(argv, cwd=cwd)
                sandbox_verdict = check.verdict.to_dict()
                if check.verdict.needs_approval:
                    sandbox_reason = check.verdict.reason
                if check.verdict.denied:
                    return {
                        "required": False,
                        "denied": True,
                        "reason": check.verdict.reason,
                        "risk": "high_risk",
                        "verdict": sandbox_verdict,
                    }
            if approvals is not None:
                assessment = approvals.assess_command(
                    argv,
                    cwd=cwd,
                    thread_id=thread_id,
                    tool=call.name,
                    sandbox_reason=sandbox_reason,
                    sandbox_verdict=sandbox_verdict,
                )
                payload = assessment.to_dict()
                payload["required"] = assessment.required
                return payload
            return {
                "required": not full_access,
                "reason": sandbox_reason or "命令执行需要批准",
                "risk": "high_risk",
            }

        if call.name.startswith("host.file."):
            if full_access:
                return {"required": False, "reason": "完全访问模式自动批准（仍记账）", "risk": "normal"}
            if call.name == "host.file.delete":
                return {"required": True, "reason": "删除是不可逆的高危操作", "risk": "high_risk"}
            overwrite = bool(arguments.get("overwrite")) or bool(
                arguments.get("append") is False and arguments.get("path")
            )
            targets = [
                arguments.get("path"),
                arguments.get("source"),
                arguments.get("destination"),
            ]
            if self.sandbox is not None:
                for value in targets:
                    if not isinstance(value, str) or not value:
                        continue
                    verdict = self.sandbox.check_path(value, AccessKind.WRITE, cwd=self.default_cwd)
                    if verdict.denied:
                        return {
                            "required": False,
                            "denied": True,
                            "reason": verdict.reason,
                            "risk": "high_risk",
                            "verdict": verdict.to_dict(),
                        }
                    if verdict.needs_approval:
                        return {
                            "required": True,
                            "reason": verdict.reason,
                            "risk": "high_risk",
                            "verdict": verdict.to_dict(),
                        }
            if overwrite:
                return {"required": True, "reason": "覆盖既有内容需要批准", "risk": "high_risk"}
            return {"required": False, "reason": "工作区内写入", "risk": "normal"}

        if call.name == "kb.write":
            return {"required": False, "reason": "知识库写入不是高危操作", "risk": "normal"}

        if approvals is not None:
            assessment = approvals.assess_tool(
                tool=call.name,
                category=definition.category.value,
                thread_id=thread_id,
                arguments=arguments,
            )
            payload = assessment.to_dict()
            payload["required"] = assessment.required
            return payload
        return {
            "required": not full_access,
            "reason": f"{definition.category.value} 类别需要批准",
            "risk": "high_risk",
        }

    def approval_gate(self, thread_id: str) -> Callable[[ToolCall], str]:
        """构造与 Agent 1 ``ApprovalGate`` 兼容的门（返回 ``allow`` / ``require``）。"""

        def gate(call: ToolCall) -> str:
            verdict = self.approval_requirement(call, thread_id=thread_id)
            return "require" if verdict.get("required") else "allow"

        return gate

    # ------------------------------------------------------------------ 内部
    def _context_for(self, call: ToolCall) -> ToolContext:
        from pathlib import Path

        base = self.default_cwd
        if base is None and self.sandbox is not None:
            base = str(self.sandbox.workspace_root)
        if base is None:
            base = str(Path.cwd())
        return ToolContext(
            thread_id=call.thread_id,
            turn_id=call.turn_id,
            call_id=call.call_id,
            cwd=Path(base),
            sandbox=self.sandbox,
            host=self.host,
            approvals=self.approvals,
            agent_tree=self.agent_tree,
            services=dict(self.services),
        )

    def _enforce_output_limit(
        self, output: dict[str, Any], definition: ToolDefinition
    ) -> tuple[dict[str, Any], bool]:
        """按上限裁剪输出，**绝不改变字段类型**。

        非字符串字段（数组、数字、布尔）是结构信息：放得下就原样保留，放不下就整条丢弃——
        早先的实现把它们 `json.dumps` 成字符串，结果 `argv` 从数组变成字符串、
        `truncated` 从布尔变成 `""`，直接被输出 Schema 判为非法。
        """
        limit = min(definition.max_output_bytes, self.max_output_bytes)
        try:
            encoded = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            return output, False
        if len(encoded.encode("utf-8")) <= limit:
            return output, False

        trimmed: dict[str, Any] = {}
        remaining = limit
        # 1) 结构字段优先（保持类型）
        for key, value in output.items():
            if isinstance(value, str):
                continue
            size = len(json.dumps({key: value}, ensure_ascii=False).encode("utf-8"))
            if size <= remaining:
                trimmed[key] = value
                remaining -= size
        # 2) 字符串字段按剩余空间裁剪（就是它们把体积撑爆的）
        for key, value in output.items():
            if not isinstance(value, str):
                continue
            keep = max(0, remaining - len(key) - 8)
            piece = value[:keep]
            trimmed[key] = piece
            remaining = max(0, remaining - len(piece.encode("utf-8")) - len(key) - 8)
        return trimmed, True


__all__ = ["ToolRegistry"]
