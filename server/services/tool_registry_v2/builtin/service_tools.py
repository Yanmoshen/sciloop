# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""依赖注入型工具：搜索 / 抓取 / 知识库 / 技能 / MCP 桥接（Agent 2 / WP-01、WP-06）。

这些能力的实现属于其他线（搜索内核、知识库服务、技能执行器、MCP 客户端），
本模块只负责**工具入口 + 参数校验 + 结构化结果**，真实后端通过
``ToolRegistry.bind(...)`` 注入：

    registry.bind(
        search=my_search,            # async (query, mode, limit) -> dict
        fetch=my_fetch,              # async (url, max_bytes) -> dict
        knowledge_search=my_search2, # async (payload) -> dict
        knowledge_write=my_write,    # async (payload) -> dict
        skill_list=my_skill_list,    # async (folder) -> dict
        skill_run=my_skill_run,      # async (name, args, cwd) -> dict
        mcp_call=my_mcp_call,        # async (tool, arguments, server) -> dict
    )

未注入时返回 ``backend_unavailable``（**结构化失败**，不伪造成功、不伪造网络调用），
因此全部测试都可以完全离线运行。
"""

from __future__ import annotations

from typing import Any

from ..models import (
    DEFAULT_TIMEOUT_MS,
    IdempotencyMode,
    PermissionClass,
    SideEffect,
    ToolContext,
    ToolDefinition,
)
from ..schemas import ANY_OBJECT, STRING_LIST_PROP, strict_object

#: 知识库分类（与既有知识库桶保持一致）
BUCKETS: tuple[str, ...] = ("literature", "idea", "experiment", "paper", "memory")


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"__error__": {"code": code, "message": message, **extra}}


async def _call_backend(
    ctx: ToolContext, key: str, *positional: Any, **keyword: Any
) -> dict[str, Any]:
    """调用注入的后端；缺失或抛错都收敛成结构化错误。"""
    backend = ctx.service(key)
    if backend is None:
        return _error(
            "backend_unavailable",
            f"后端 {key!r} 未接入（由集成方通过 ToolRegistry.bind 注入）",
            backend=key,
        )
    try:
        result = backend(*positional, **keyword)
        if hasattr(result, "__await__"):
            result = await result
    except Exception as exc:  # noqa: BLE001 - 后端异常必须回喂模型
        return _error("backend_error", f"{type(exc).__name__}: {exc}", backend=key)
    return dict(result or {})


# ---------------------------------------------------------------------------- 搜索
def search_query_definition() -> ToolDefinition:
    return ToolDefinition(
        name="search.query",
        permission=PermissionClass.READ,
        description="联网检索（mode=academic 查学术，mode=web 搜网页）",
        side_effect=SideEffect.NETWORK,
        parallel_safe=True,
        idempotency=IdempotencyMode.NONE,
        cancellation_support=True,
        audit_fields=("query", "mode", "limit"),
        input_schema=strict_object(
            required=("query",),
            properties={
                "query": {"type": "string", "minLength": 1},
                "mode": {"type": "string", "enum": ["academic", "web"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_search_query,
    )


async def run_search_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx,
        "search",
        str(args["query"]),
        str(args.get("mode", "web")),
        int(args.get("limit", 10)),
    )


def search_fetch_definition() -> ToolDefinition:
    return ToolDefinition(
        name="search.fetch",
        permission=PermissionClass.READ,
        description="抓取指定网页并转成文本（只读）",
        side_effect=SideEffect.NETWORK,
        parallel_safe=True,
        idempotency=IdempotencyMode.NONE,
        cancellation_support=True,
        audit_fields=("url", "max_bytes"),
        input_schema=strict_object(
            required=("url",),
            properties={
                "url": {"type": "string", "minLength": 1},
                "max_bytes": {"type": "integer", "minimum": 1},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_search_fetch,
    )


async def run_search_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx, "fetch", str(args["url"]), int(args.get("max_bytes", 512 * 1024))
    )


# ---------------------------------------------------------------------------- 知识库
def knowledge_search_definition() -> ToolDefinition:
    return ToolDefinition(
        name="knowledge.search",
        permission=PermissionClass.READ,
        description="查询知识库条目（只读）",
        side_effect=SideEffect.NONE,
        parallel_safe=True,
        idempotency=IdempotencyMode.NONE,
        cancellation_support=False,
        audit_fields=("query", "bucket", "folder", "limit"),
        input_schema=strict_object(
            properties={
                "query": {"type": "string"},
                "bucket": {"type": "string", "enum": list(BUCKETS)},
                "folder": STRING_LIST_PROP,
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            }
        ),
        output_schema=ANY_OBJECT,
        handler=run_knowledge_search,
    )


async def run_knowledge_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(ctx, "knowledge_search", dict(args))


def knowledge_write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="knowledge.write",
        permission=PermissionClass.WORKSPACE_WRITE,
        description="写入知识库条目（新建资料 / 摘录 / 记忆）",
        side_effect=SideEffect.FILESYSTEM,
        timeout_ms=DEFAULT_TIMEOUT_MS,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=False,
        audit_fields=("name", "bucket", "folder", "tags", "project_id"),
        input_schema=strict_object(
            required=("name", "content"),
            properties={
                "name": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
                "bucket": {"type": "string", "enum": list(BUCKETS)},
                "folder": STRING_LIST_PROP,
                "tags": STRING_LIST_PROP,
                "project_id": {"type": ["integer", "null"]},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_knowledge_write,
    )


async def run_knowledge_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(ctx, "knowledge_write", dict(args))


# ---------------------------------------------------------------------------- 技能
def skill_list_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill.list",
        permission=PermissionClass.READ,
        description="列出可用技能（只读，仅元数据）",
        side_effect=SideEffect.NONE,
        parallel_safe=True,
        idempotency=IdempotencyMode.NONE,
        cancellation_support=False,
        audit_fields=("folder",),
        input_schema=strict_object(properties={"folder": {"type": "string"}}),
        output_schema=ANY_OBJECT,
        handler=run_skill_list,
    )


async def run_skill_list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(ctx, "skill_list", args.get("folder"))


def skill_run_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill.run",
        permission=PermissionClass.EXEC,
        description="执行技能脚本（走宿主机执行通道，需要审批）",
        side_effect=SideEffect.PROCESS,
        timeout_ms=600_000,
        idempotency=IdempotencyMode.EXECUTION,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=("name", "args", "cwd"),
        input_schema=strict_object(
            required=("name",),
            properties={
                "name": {"type": "string", "minLength": 1},
                "args": STRING_LIST_PROP,
                "cwd": {"type": "string"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_skill_run,
    )


async def run_skill_run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx,
        "skill_run",
        str(args["name"]),
        [str(item) for item in args.get("args") or []],
        args.get("cwd"),
    )


# ---------------------------------------------------------------------------- MCP
def mcp_call_definition() -> ToolDefinition:
    return ToolDefinition(
        name="mcp.call",
        permission=PermissionClass.EXEC,
        description="桥接调用 MCP 工具（逐个工具桥接时应改为继承该工具的权限类别）",
        side_effect=SideEffect.EXTERNAL,
        timeout_ms=120_000,
        idempotency=IdempotencyMode.CALL_ID,
        parallel_safe=False,
        cancellation_support=True,
        audit_fields=("tool", "arguments", "server"),
        input_schema=strict_object(
            required=("tool",),
            properties={
                "tool": {"type": "string", "minLength": 1},
                "arguments": {"type": "object"},
                "server": {"type": "string"},
            },
        ),
        output_schema=ANY_OBJECT,
        handler=run_mcp_call,
    )


async def run_mcp_call(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx,
        "mcp_call",
        str(args["tool"]),
        dict(args.get("arguments") or {}),
        args.get("server"),
    )


def service_tool_definitions() -> list[ToolDefinition]:
    return [
        search_query_definition(),
        search_fetch_definition(),
        knowledge_search_definition(),
        knowledge_write_definition(),
        skill_list_definition(),
        skill_run_definition(),
        mcp_call_definition(),
    ]


__all__ = [
    "service_tool_definitions",
    "search_query_definition",
    "search_fetch_definition",
    "knowledge_search_definition",
    "knowledge_write_definition",
    "skill_list_definition",
    "skill_run_definition",
    "mcp_call_definition",
    "BUCKETS",
]
