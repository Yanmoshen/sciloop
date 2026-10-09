# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""宿主机命令与文件工具（Agent 2 / WP-01、WP-06）。

- ``host.command``：**argv 直启**（绝不拼 shell），走 :mod:`services.host_execution_v2`；
- ``host.file.*``：列举 / 读取 / 写入 / 移动 / 删除，全部先过沙箱裁决。

错误一律用 ``{"__error__": {...}}`` 表达（注册表据此返回结构化失败），**不抛异常**。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from services.sandbox_v2 import SandboxManager

from ..models import (
    DEFAULT_MAX_OUTPUT_BYTES,
    IdempotencyMode,
    PermissionClass,
    SideEffect,
    ToolContext,
    ToolDefinition,
)
from ..schemas import (
    ANY_OBJECT,
    ARGV_PROP,
    BYTES_PROP,
    HOST_EXEC_OUTPUT,
    OPTIONAL_PATH_PROP,
    PATH_PROP,
    STRING_LIST_PROP,
    strict_object,
)

#: 命令行工具超时上限（可被参数覆盖，但不超过它）
MAX_EXEC_TIMEOUT_MS = 600_000

#: 宿主机命令/文件的审计字段（每次调用都要留档）
EXEC_AUDIT_FIELDS: tuple[str, ...] = ("argv", "cwd", "timeout_ms", "exit_code", "status")
FILE_AUDIT_FIELDS: tuple[str, ...] = ("path", "source", "destination", "bytes")


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"__error__": {"code": code, "message": message, **extra}}


def _resolve(path_value: str, ctx: ToolContext) -> Path:
    from services.sandbox_v2 import normalize

    return normalize(path_value, base=ctx.cwd)


def _sandbox_check_path(
    ctx: ToolContext, path: Path, access: str
) -> dict[str, Any] | None:
    """沙箱裁决 → 错误字典或 None。"""
    sandbox: SandboxManager | None = ctx.sandbox
    if sandbox is None:
        return None
    verdict = {
        "read": sandbox.check_read,
        "write": sandbox.check_write,
        "execute": sandbox.check_execute,
    }[access](path)
    if verdict.denied:
        return _error("sandbox_denied", verdict.reason, verdict=verdict.to_dict())
    if verdict.needs_approval:
        return _error("approval_required", verdict.reason, verdict=verdict.to_dict())
    return None


# ---------------------------------------------------------------------------- 命令
def host_command_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.command",
        permission=PermissionClass.EXEC,
        description="在宿主机上执行一条命令（argv 数组直启，不经过 shell 拼接）",
        version="1.0.0",
        side_effect=SideEffect.PROCESS,
        timeout_ms=MAX_EXEC_TIMEOUT_MS,
        max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        idempotency=IdempotencyMode.EXECUTION,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=EXEC_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("argv",),
            properties={
                "argv": ARGV_PROP,
                "cwd": OPTIONAL_PATH_PROP,
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": MAX_EXEC_TIMEOUT_MS},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            description="命令与参数，例如 [\"python\", \"script.py\"]",
        ),
        output_schema=HOST_EXEC_OUTPUT,
        metadata={"argv_only": True, "shell": False},
        handler=run_host_command,
    )


async def run_host_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    argv = [str(item) for item in args.get("argv") or []]
    if not argv:
        return _error("invalid_arguments", "argv 不能为空")
    cwd = args.get("cwd") or str(ctx.cwd)
    if ctx.host is None:
        return _error("backend_unavailable", "宿主机执行器未接入")

    if ctx.sandbox is not None:
        # 命令级检查：cwd + argv 里每个路径 + 重定向目标 + 可疑命令升级
        check = ctx.sandbox.check_command_paths(argv, cwd=cwd)
        if check.verdict.denied:
            return _error("sandbox_denied", check.verdict.reason, verdict=check.verdict.to_dict())
        if check.verdict.needs_approval:
            # 审批由运行时在**执行前**完成（approval_gate）；走到这里说明没批准
            return _error(
                "approval_required", check.verdict.reason, verdict=check.verdict.to_dict()
            )

    timeout_ms = args.get("timeout_ms")
    record = await ctx.host.execute(
        argv,
        thread_id=ctx.thread_id,
        turn_id=ctx.turn_id,
        call_id=ctx.call_id,
        cwd=cwd,
        env=dict(args.get("env") or {}) or None,
        timeout_s=float(timeout_ms) / 1000.0 if timeout_ms else None,
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
def host_file_list_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.list",
        permission=PermissionClass.READ,
        description="列举目录内容（只读）",
        side_effect=SideEffect.NONE,
        parallel_safe=True,
        idempotency=IdempotencyMode.NONE,
        cancellation_support=False,
        audit_fields=("path",),
        input_schema=strict_object(
            properties={
                "path": OPTIONAL_PATH_PROP,
                "recursive": {"type": "boolean"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 5000},
            }
        ),
        output_schema=ANY_OBJECT,
        handler=run_file_list,
    )


async def run_file_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(args.get("path") or ".", ctx)
    blocked = _sandbox_check_path(ctx, target, "read")
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
        permission=PermissionClass.READ,
        description="读取文本文件（只读）",
        side_effect=SideEffect.NONE,
        parallel_safe=True,
        idempotency=IdempotencyMode.NONE,
        cancellation_support=False,
        audit_fields=("path",),
        input_schema=strict_object(
            required=("path",),
            properties={"path": PATH_PROP, "max_bytes": BYTES_PROP},
        ),
        output_schema=ANY_OBJECT,
        handler=run_file_read,
    )


async def run_file_read(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(str(args["path"]), ctx)
    blocked = _sandbox_check_path(ctx, target, "read")
    if blocked:
        return blocked
    if not target.is_file():
        return _error("not_found", f"{target} 不是文件")
    max_bytes = int(args.get("max_bytes", 256 * 1024))
    raw = target.read_bytes()
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return {"path": str(target), "bytes": len(raw), "text": text, "truncated": truncated}


def host_file_write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="host.file.write",
        permission=PermissionClass.WORKSPACE_WRITE,
        description="写入文本文件（新建或覆盖正文；目录会自动创建）",
        side_effect=SideEffect.FILESYSTEM,
        timeout_ms=60_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=False,
        audit_fields=FILE_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("path", "content"),
            properties={
                "path": PATH_PROP,
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_file_write,
    )


async def run_file_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(str(args["path"]), ctx)
    blocked = _sandbox_check_path(ctx, target, "write")
    if blocked:
        return blocked
    content = str(args.get("content", ""))
    append = bool(args.get("append", False))
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if append else "w", encoding="utf-8") as handle:
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
        permission=PermissionClass.WORKSPACE_WRITE,
        description="移动或重命名文件/目录（覆盖既有目标需要审批）",
        side_effect=SideEffect.FILESYSTEM,
        timeout_ms=60_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=False,
        audit_fields=FILE_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("source", "destination"),
            properties={
                "source": PATH_PROP,
                "destination": PATH_PROP,
                "overwrite": {"type": "boolean"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_file_move,
    )


async def run_file_move(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    source = _resolve(str(args["source"]), ctx)
    destination = _resolve(str(args["destination"]), ctx)
    for path in (source, destination):
        blocked = _sandbox_check_path(ctx, path, "write")
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
        permission=PermissionClass.DANGEROUS,
        description="删除文件或目录（高危，必须审批）",
        side_effect=SideEffect.FILESYSTEM,
        timeout_ms=60_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=False,
        audit_fields=FILE_AUDIT_FIELDS,
        input_schema=strict_object(
            required=("path",),
            properties={"path": PATH_PROP, "recursive": {"type": "boolean"}},
        ),
        output_schema=ANY_OBJECT,
        handler=run_file_delete,
    )


async def run_file_delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    target = _resolve(str(args["path"]), ctx)
    blocked = _sandbox_check_path(ctx, target, "write")
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
        host_command_definition(),
        host_file_list_definition(),
        host_file_read_definition(),
        host_file_write_definition(),
        host_file_move_definition(),
        host_file_delete_definition(),
    ]


__all__ = [
    "host_tool_definitions",
    "host_command_definition",
    "run_host_command",
    "host_file_list_definition",
    "host_file_read_definition",
    "host_file_write_definition",
    "host_file_move_definition",
    "host_file_delete_definition",
    "MAX_EXEC_TIMEOUT_MS",
    "EXEC_AUDIT_FIELDS",
    "FILE_AUDIT_FIELDS",
    "STRING_LIST_PROP",
]
