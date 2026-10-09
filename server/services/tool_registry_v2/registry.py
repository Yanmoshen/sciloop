# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""ToolRegistry：工具注册、参数校验与执行边界（Agent 2 / WP-01）。

本类就是契约里 :class:`contracts.agent_v2.fake.ToolExecutor` 的**实现**：

    def specs() -> Sequence[ToolSpec]
    def spec(name) -> ToolSpec | None
    async def execute(call, cancel) -> ToolResult

执行流程（顺序固定，测试据此断言）：

1. 未知工具 → ``invalid_arguments``（不调用任何 handler）；
2. **参数校验**（官方 ``jsonschema``，见 ``contracts.agent_v2.validate``）→ 执行前拒绝；
3. 取消令牌已触发 → ``cancelled``；
4. 沙箱/审批前置判定 → 被拒时返回结构化错误（不抛异常）；
5. handler 执行（带超时）→ 成功 / 失败 / 超时；
6. 输出按 ``max_output_bytes`` **截断并如实标记**（不改字段类型）；
7. 结果始终带原始 ``call_id``。

Registry **不负责执行策略**（那是 Scheduler 与 ApprovalManager 的事），只负责
"工具能做什么、参数对不对、结果怎么回来"。
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
from services.approval_v2 import ApprovalManager, DecisionScope
from services.host_execution_v2 import HostExecutionManager
from services.sandbox_v2 import SandboxManager

from .models import (
    DEFAULT_MAX_OUTPUT_BYTES,
    PermissionClass,
    ToolContext,
    ToolDefinition,
)


class ToolRegistry:
    """工具注册表 + 执行器（契约 ``ToolExecutor`` 的实现）。"""

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
        if not definition.name:
            raise ValueError("tool name must not be empty")
        if definition.name in self._definitions and not replace:
            raise ValueError(f"tool {definition.name!r} is already registered")
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
        """注入后端实现（搜索 / 知识库 / 技能 / MCP 等）。"""
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

    def permission_of(self, name: str) -> PermissionClass:
        definition = self._definitions.get(name)
        # 未知工具按最严处理：宁可要裁决，也不要猜它无害
        return definition.permission if definition else PermissionClass.DANGEROUS

    # ------------------------------------------------------------------ 契约投影
    def specs(self) -> list[ToolSpec]:
        """给 Agent 1 调度器的契约声明。"""
        return [item.to_spec() for item in self.definitions()]

    def spec(self, name: str) -> ToolSpec | None:
        definition = self._definitions.get(name)
        return None if definition is None else definition.to_spec()

    # ------------------------------------------------------------------ 交付证据
    def permission_matrix(self) -> list[dict[str, Any]]:
        return [item.describe() for item in self.definitions()]

    def permission_class_matrix(self) -> dict[str, list[str]]:
        matrix: dict[str, list[str]] = {member.value: [] for member in PermissionClass}
        for definition in self.definitions():
            matrix[definition.permission.value].append(definition.name)
        return matrix

    def capability_matrix(self) -> dict[str, list[str]]:
        """按副作用性质分类（交付证据之一）。"""
        matrix: dict[str, list[str]] = {}
        for definition in self.definitions():
            matrix.setdefault(definition.side_effect.value, []).append(definition.name)
        return {key: sorted(value) for key, value in sorted(matrix.items())}

    def validated_schemas(self) -> dict[str, bool]:
        """每个工具的输入/输出 Schema 是否可被 Draft 2020-12 校验器接受。"""
        from jsonschema import Draft202012Validator

        result: dict[str, bool] = {}
        for definition in self.definitions():
            ok = True
            for schema in (definition.input_schema, definition.output_schema):
                try:
                    Draft202012Validator.check_schema(schema)
                except Exception:  # noqa: BLE001 - 交给测试断言
                    ok = False
            result[definition.name] = ok
        return result

    # ------------------------------------------------------------------ 校验
    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> list[str]:
        validator = self._validators.get(name)
        if validator is None:
            return [f"tool {name!r} is not registered"]
        return validator.errors(arguments or {})

    # ------------------------------------------------------------------ 审批判定
    def approval_requirement(self, call: ToolCall, *, thread_id: str) -> dict[str, Any]:
        """参数感知的"是否需要研究者决定"判定（不执行任何东西）。"""
        definition = self._definitions.get(call.name)
        arguments = dict(call.arguments or {})
        if definition is None:
            return {"required": True, "reason": f"未知工具 {call.name!r}", "risk": "high_risk"}
        if definition.permission is PermissionClass.READ:
            return {"required": False, "reason": "只读工具自动执行", "risk": "read_only"}

        approvals = self.approvals
        full_access = bool(approvals is not None and approvals.full_access)

        # 命令类：沙箱命令检查（cwd + 参数路径 + 重定向 + 内联代码升级）→ 风险判定
        if definition.name in {"host.command", "skill.run"}:
            argv = [str(item) for item in (arguments.get("argv") or [])]
            cwd = arguments.get("cwd") or str(
                self.default_cwd or (self.sandbox.workspace_root if self.sandbox else ".")
            )
            verdict: dict[str, Any] | None = None
            if self.sandbox is not None and argv:
                check = self.sandbox.check_command_paths(argv, cwd=cwd)
                verdict = check.verdict.to_dict()
                if check.verdict.denied:
                    return {
                        "required": False,
                        "denied": True,
                        "reason": check.verdict.reason,
                        "risk": "high_risk",
                        "verdict": verdict,
                    }
            if definition.name == "skill.run" and not argv:
                if approvals is not None:
                    assessment = approvals.assess_tool(
                        tool=call.name,
                        permission_class=definition.permission.value,
                        thread_id=thread_id,
                        arguments=arguments,
                        cwd=cwd,
                        summary="技能执行需要研究者决定",
                    )
                    payload = assessment.to_dict()
                    payload["required"] = assessment.required
                    return payload
                return {"required": not full_access, "reason": "技能执行需要裁决", "risk": "high_risk"}
            if approvals is not None:
                assessment = approvals.assess_command(
                    argv,
                    cwd=cwd,
                    thread_id=thread_id,
                    tool=call.name,
                    sandbox_verdict=verdict,
                    requested_scope=DecisionScope.ONCE.value,
                )
                payload = assessment.to_dict()
                payload["required"] = assessment.required
                return payload
            return {
                "required": not full_access,
                "reason": (verdict or {}).get("reason") or "命令执行需要裁决",
                "risk": "high_risk",
                "verdict": verdict,
            }

        # 文件工具：沙箱 + 覆盖/删除
        if definition.name.startswith("host.file."):
            if full_access:
                return {"required": False, "reason": "完全访问模式自动放行（仍记账）", "risk": "normal"}
            targets = [
                arguments.get("path"),
                arguments.get("source"),
                arguments.get("destination"),
            ]
            if self.sandbox is not None:
                for value in targets:
                    if not isinstance(value, str) or not value:
                        continue
                    verdict = self.sandbox.check_write(value)
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
            if definition.permission is PermissionClass.DANGEROUS:
                return {"required": True, "reason": "删除/覆盖是不可逆的高危操作", "risk": "high_risk"}
            if bool(arguments.get("overwrite")):
                return {"required": True, "reason": "覆盖既有内容需要裁决", "risk": "high_risk"}
            return {"required": False, "reason": "工作区内按策略执行", "risk": "normal"}

        if definition.name == "knowledge.write":
            return {"required": False, "reason": "知识库写入不是高危操作", "risk": "normal"}

        if approvals is not None:
            assessment = approvals.assess_tool(
                tool=call.name,
                permission_class=definition.permission.value,
                thread_id=thread_id,
                arguments=arguments,
            )
            payload = assessment.to_dict()
            payload["required"] = assessment.required
            return payload
        return {
            "required": not full_access,
            "reason": f"{definition.permission.value} 类别需要裁决",
            "risk": "high_risk",
        }

    def approval_gate(self, thread_id: str) -> Callable[[ToolCall], str]:
        """构造与 Agent 1 ``ApprovalGate`` 兼容的门（返回 ``allow`` / ``require``）。"""

        def gate(call: ToolCall) -> str:
            verdict = self.approval_requirement(call, thread_id=thread_id)
            return "require" if verdict.get("required") else "allow"

        return gate

    # ------------------------------------------------------------------ 执行
    async def execute(self, call: ToolCall, cancel: CancelToken | None = None) -> ToolResult:
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
                error={"code": "not_implemented", "message": f"tool {call.name!r} has no handler bound"},
            )

        context = self._context_for(call)
        try:
            if definition.timeout_ms:
                raw_output = await asyncio.wait_for(
                    definition.handler(dict(call.arguments or {}), context),
                    timeout=float(definition.timeout_ms) / 1000.0,
                )
            else:
                raw_output = await definition.handler(dict(call.arguments or {}), context)
        except TimeoutError:
            return finish(
                ToolCallStatus.TIMEOUT,
                error={
                    "code": "timeout",
                    "message": f"tool {call.name!r} exceeded {definition.timeout_ms}ms",
                    "timeout_ms": definition.timeout_ms,
                },
            )
        except asyncio.CancelledError:
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
                f"输出超过 {definition.max_output_bytes} 字节，已截断（完整内容见执行记录）"
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
        """按上限裁剪输出，**绝不改变字段类型**（结构字段保类型，只裁字符串）。"""
        limit = min(definition.max_output_bytes, self.max_output_bytes)
        try:
            encoded = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            return output, False
        if len(encoded.encode("utf-8")) <= limit:
            return output, False

        trimmed: dict[str, Any] = {}
        remaining = limit
        for key, value in output.items():
            if isinstance(value, str):
                continue
            size = len(json.dumps({key: value}, ensure_ascii=False).encode("utf-8"))
            if size <= remaining:
                trimmed[key] = value
                remaining -= size
        for key, value in output.items():
            if not isinstance(value, str):
                continue
            keep = max(0, remaining - len(key) - 8)
            piece = value[:keep]
            trimmed[key] = piece
            remaining = max(0, remaining - len(piece.encode("utf-8")) - len(key) - 8)
        return trimmed, True


__all__ = ["ToolRegistry"]
