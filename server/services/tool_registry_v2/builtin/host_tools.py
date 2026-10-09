# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""宿主机命令与文件工具（Agent 2 / WP-06）。

- ``host.exec``：**argv 直启**（绝不拼 shell），走 :mod:`services.host_execution_v2`；
- ``host.file.*``：列举 / 读取 / 写入 / 移动 / 删除，全部先过沙箱裁决。

错误一律用 ``{"__error__": {...}}`` 表达（注册表据此返回结构化失败），**不抛异常**。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from services.sandbox_v2 import AccessKind

from ..definition import IdempotencyMode, ToolCategory, ToolContext, ToolDefinition

#: 命令行工具超时上限（可被参数覆盖，但不超过它）
MAX_EXEC_TIMEOUT_S = 600.0

_OBJECT = {"type": "object"}


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"__error__": {"code": code, "message": message, **extra}}


def _resolve(path_value: str, ctx: ToolContext) -> Path:
    from services.sandbox_v2 import normalize

    return normalize(path_value, base=ctx.cwd)


# ---------------------------------------------------------------------------- 命令
def host_exec_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.exec",
        category=ToolCategory.EXECUTION,
        description="在宿主机上执行一条命令（argv 直启，不经过 shell 拼接）",
        version="1.0.0",
        timeout_s=MAX_EXEC_TIMEOUT_S,
        idempotency=IdempotencyMode.EXECUTION,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["argv"],
            "properties": {
                "argv": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                    "description": "命令与参数，例如 [\"python\", \"script.py\"]",
                },
                "cwd": {"type": "string", "description": "工作目录（默认工作区根）"},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "execution_id": {"type": "string"},
                "argv": {"type": "array"},
                "cwd": {"type": "string"},
                "status": {"type": "string"},
                "exit_code": {"type": ["integer", "null"]},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "duration_ms": {"type": ["integer", "null"]},
                "truncated": {"type": "boolean"},
            },
        },
        handler=run_host_exec,
    )


async def run_host_exec(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    argv = [str(item) for item in args.get("argv") or []]
    if not argv:
        return _error("invalid_arguments", "argv 不能为空")
    cwd = args.get("cwd") or str(ctx.cwd)
    timeout = args.get("timeout_s")
    if ctx.host is None:
        return _error("backend_unavailable", "宿主机执行器未接入")

    if ctx.sandbox is not None:
        check = ctx.sandbox.check_argv(argv, cwd=cwd)
        if check.verdict.denied:
            return _error(
                "sandbox_denied",
                check.verdict.reason,
                verdict=check.verdict.to_dict(),
            )
        if check.verdict.needs_approval:
            # 审批由运行时在**执行前**完成（approval_gate）；走到这里说明没批准
            return _error(
                "approval_required",
                check.verdict.reason,
                verdict=check.verdict.to_dict(),
            )

    record = await ctx.host.execute(
        argv,
        thread_id=ctx.thread_id,
        turn_id=ctx.turn_id,
        call_id=ctx.call_id,
        cwd=cwd,
        env=dict(args.get("env") or {}) or None,
        timeout_s=float(timeout) if timeout else None,
        sandbox_check=False,
    )
    output = record.to_tool_output()
    if record.status.value in {"failed", "timeout", "cancelled", "interrupted", "unknown"}:
        # 非零退出 / 超时 / 取消：**结构化结果 + 明确错误码**，绝不伪装成系统异常
        error = dict(record.error or {})
        error.setdefault("code", record.status.value)
        error.setdefault("message", f"命令未成功完成（{record.status.value}）")
        return {"__error__": error, **output}
    return output


# ---------------------------------------------------------------------------- 文件
def _sandbox_guard(
    ctx: ToolContext, path: Path, access: AccessKind
) -> dict[str, Any] | None:
    """返回错误字典表示被拦下；None 表示放行。"""
    if ctx.sandbox is None:
        return None
    verdict = ctx.sandbox.check_path(path, access)
    if verdict.denied:
        return _error("sandbox_denied", verdict.reason, verdict=verdict.to_dict())
    if verdict.needs_approval:
        return _error("approval_required", verdict.reason, verdict=verdict.to_dict())
    return None


def host_file_list_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.list",
        category=ToolCategory.READ_ONLY,
        description="列举目录内容（只读）",
        parallel=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean"},
                "max_entries": {"type": "integer", "minimum": 1},
            },
        },
        output_schema=_OBJECT,
        handler=run_file_list,
    )


async def run_file_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(args.get("path") or ".", ctx)
    blocked = _sandbox_guard(ctx, target, AccessKind.READ)
    if blocked:
        return blocked
    if not target.exists():
        return _error("not_found", f"{target} 不存在")
    if not target.is_dir():
        return _error("not_a_directory", f"{target} 不是目录")

    recursive = bool(args.get("recursive", False))
    limit = int(args.get("max_entries", 200))
    entries: list[dict[str, Any]] = []
    iterator = target.rglob("*") if recursive else target.iterdir()
    for item in iterator:
        try:
            stat = item.stat()
        except OSError:  # pragma: no cover - 并发删除时跳过
            continue
        entries.append(
            {
                "name": item.name,
                "path": str(item),
                "is_dir": item.is_dir(),
                "size": int(stat.st_size),
            }
        )
        if len(entries) >= limit:
            break
    entries.sort(key=lambda row: (not row["is_dir"], row["name"].lower()))
    return {"path": str(target), "count": len(entries), "entries": entries}


def host_file_read_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.read",
        category=ToolCategory.READ_ONLY,
        description="读取文本文件（只读）",
        parallel=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "max_bytes": {"type": "integer", "minimum": 1},
            },
        },
        output_schema=_OBJECT,
        handler=run_file_read,
    )


async def run_file_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(str(args["path"]), ctx)
    blocked = _sandbox_guard(ctx, target, AccessKind.READ)
    if blocked:
        return blocked
    if not target.is_file():
        return _error("not_found", f"{target} 不是文件")
    max_bytes = int(args.get("max_bytes", 256 * 1024))
    raw = target.read_bytes()
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "path": str(target),
        "bytes": len(raw),
        "text": text,
        "truncated": truncated,
    }


def host_file_write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.write",
        category=ToolCategory.WORKSPACE_WRITE,
        description="写入文本文件（新建或覆盖正文；目录会自动创建）",
        timeout_s=60.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
        },
        output_schema=_OBJECT,
        handler=run_file_write,
    )


async def run_file_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(str(args["path"]), ctx)
    blocked = _sandbox_guard(ctx, target, AccessKind.WRITE)
    if blocked:
        return blocked
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with target.open(mode, encoding="utf-8") as handle:
        handle.write(content)
    return {
        "path": str(target),
        "bytes": len(content.encode("utf-8")),
        "created": not existed,
        "appended": append,
        "size": target.stat().st_size,
    }


def host_file_move_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.move",
        category=ToolCategory.WORKSPACE_WRITE,
        description="移动或重命名文件/目录（覆盖既有目标需要审批）",
        timeout_s=60.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["source", "destination"],
            "properties": {
                "source": {"type": "string", "minLength": 1},
                "destination": {"type": "string", "minLength": 1},
                "overwrite": {"type": "boolean"},
            },
        },
        output_schema=_OBJECT,
        handler=run_file_move,
    )


async def run_file_move(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(str(args["source"]), ctx)
    destination = _resolve(str(args["destination"]), ctx)
    for path in (source, destination):
        blocked = _sandbox_guard(ctx, path, AccessKind.WRITE)
        if blocked:
            return blocked
    if not source.exists():
        return _error("not_found", f"{source} 不存在")
    overwrite = bool(args.get("overwrite", False))
    if destination.exists() and not overwrite:
        return _error(
            "target_exists",
            f"{destination} 已存在；如需覆盖请显式声明 overwrite=true（会要求审批）",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            return _error("refused", "目标是非空目录，拒绝覆盖")
        destination.unlink()
    shutil.move(str(source), str(destination))
    return {"source": str(source), "destination": str(destination), "overwritten": overwrite}


def host_file_delete_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.delete",
        category=ToolCategory.HIGH_RISK,
        description="删除文件或目录（高危，必须审批）",
        timeout_s=60.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "recursive": {"type": "boolean"},
            },
        },
        output_schema=_OBJECT,
        handler=run_file_delete,
    )


async def run_file_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(str(args["path"]), ctx)
    blocked = _sandbox_guard(ctx, target, AccessKind.WRITE)
    if blocked:
        return blocked
    if not target.exists():
        return _error("not_found", f"{target} 不存在")
    if target.is_dir():
        if not bool(args.get("recursive", False)):
            return _error("refused", "目标是目录且未声明 recursive=true")
        try:
            target.rmdir()
            removed = "empty_directory"
        except OSError:
            shutil.rmtree(target)
            removed = "directory_tree"
        return {"path": str(target), "removed": removed}
    target.unlink()
    return {"path": str(target), "removed": "file"}


def host_tool_definitions() -> list[ToolDefinition]:
    return [
        host_exec_definition(),
        host_file_list_definition(),
        host_file_read_definition(),
        host_file_write_definition(),
        host_file_move_definition(),
        host_file_delete_definition(),
    ]


__all__ = [
    "host_tool_definitions",
    "host_exec_definition",
    "run_host_exec",
    "host_file_list_definition",
    "host_file_read_definition",
    "host_file_write_definition",
    "host_file_move_definition",
    "host_file_delete_definition",
    "MAX_EXEC_TIMEOUT_S",
]
