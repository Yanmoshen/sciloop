# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""依赖注入型工具：搜索 / 抓取 / 知识库 / 技能 / MCP 桥接（Agent 2 / WP-06）。

这些能力的实现属于其他线（搜索内核、知识库服务、技能执行器、MCP 客户端），
本模块只负责**工具入口 + 参数校验 + 结构化结果**，真实后端通过
``ToolRegistry.bind(...)`` 注入：

    registry.bind(
        search=my_search,          # async (query, mode, limit) -> dict
        fetch=my_fetch,            # async (url, max_bytes) -> dict
        kb_query=my_kb_query,      # async (payload) -> dict
        kb_write=my_kb_write,      # async (payload) -> dict
        skill_load=my_skill_load,  # async (name) -> dict
        skill_run=my_skill_run,    # async (name, args) -> dict
        mcp_call=my_mcp_call,      # async (tool, arguments) -> dict
    )

未注入时返回 ``backend_unavailable``（**结构化失败**，不伪造成功、不伪造网络调用），
因此全部测试都可以完全离线运行。
"""

from __future__ import annotations

from typing import Any

from ..definition import IdempotencyMode, ToolCategory, ToolContext, ToolDefinition

_OBJECT = {"type": "object"}


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
def web_search_definition() -> ToolDefinition:
    return ToolDefinition(
        name="web.search",
        category=ToolCategory.READ_ONLY,
        description="联网检索（mode=academic 查学术，mode=web 搜网页）",
        parallel=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "mode": {"type": "string", "enum": ["academic", "web"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
        output_schema=_OBJECT,
        handler=run_web_search,
    )


async def run_web_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx,
        "search",
        str(args["query"]),
        str(args.get("mode", "web")),
        int(args.get("limit", 10)),
    )


def web_fetch_definition() -> ToolDefinition:
    return ToolDefinition(
        name="web.fetch",
        category=ToolCategory.READ_ONLY,
        description="抓取指定网页并转成文本（只读）",
        parallel=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "max_bytes": {"type": "integer", "minimum": 1},
            },
        },
        output_schema=_OBJECT,
        handler=run_web_fetch,
    )


async def run_web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx, "fetch", str(args["url"]), int(args.get("max_bytes", 512 * 1024))
    )


# ---------------------------------------------------------------------------- 知识库
def kb_query_definition() -> ToolDefinition:
    return ToolDefinition(
        name="kb.query",
        category=ToolCategory.READ_ONLY,
        description="查询知识库条目（只读）",
        parallel=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string"},
                "bucket": {
                    "type": "string",
                    "enum": ["literature", "idea", "experiment", "paper", "memory"],
                },
                "folder": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
        output_schema=_OBJECT,
        handler=run_kb_query,
    )


async def run_kb_query(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(ctx, "kb_query", dict(args))


def kb_write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="kb.write",
        category=ToolCategory.WORKSPACE_WRITE,
        description="写入知识库条目（新建资料 / 摘录 / 记忆）",
        timeout_s=60.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "content"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
                "bucket": {
                    "type": "string",
                    "enum": ["literature", "idea", "experiment", "paper", "memory"],
                },
                "folder": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "project_id": {"type": ["integer", "null"]},
            },
        },
        output_schema=_OBJECT,
        handler=run_kb_write,
    )


async def run_kb_write(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(ctx, "kb_write", dict(args))


# ---------------------------------------------------------------------------- 技能
def skill_load_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill.load",
        category=ToolCategory.READ_ONLY,
        description="加载技能说明（只读）",
        parallel=True,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "version": {"type": "string"},
            },
        },
        output_schema=_OBJECT,
        handler=run_skill_load,
    )


async def run_skill_load(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return await _call_backend(
        ctx, "skill_load", str(args["name"]), args.get("version")
    )


def skill_run_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill.run",
        category=ToolCategory.EXECUTION,
        description="执行技能脚本（走宿主机执行通道，需要审批）",
        timeout_s=600.0,
        idempotency=IdempotencyMode.EXECUTION,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "args": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
            },
        },
        output_schema=_OBJECT,
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
        category=ToolCategory.EXECUTION,
        description="桥接调用 MCP 工具（逐个工具桥接时应改为继承该工具的权限类别）",
        timeout_s=120.0,
        idempotency=IdempotencyMode.CALL_ID,
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["tool"],
            "properties": {
                "tool": {"type": "string", "minLength": 1},
                "arguments": {"type": "object"},
                "server": {"type": "string"},
            },
        },
        output_schema=_OBJECT,
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
        web_search_definition(),
        web_fetch_definition(),
        kb_query_definition(),
        kb_write_definition(),
        skill_load_definition(),
        skill_run_definition(),
        mcp_call_definition(),
    ]


__all__ = [
    "service_tool_definitions",
    "web_search_definition",
    "web_fetch_definition",
    "kb_query_definition",
    "kb_write_definition",
    "skill_load_definition",
    "skill_run_definition",
    "mcp_call_definition",
]
