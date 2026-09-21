# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""把 SciLoop 的 MCP 工具接到对话循环上。

分工：`mcp_server` 是**工具侧**（工具声明 + 四道边界门），本模块是**对话侧**——
负责「把哪些工具摆给模型」「模型的 tool_calls 怎么执行」「执行过程怎么回报给前端」。

**只读自主、写/执行需批准**（已确认的口径）：本模块当前只暴露**只读工具**给模型自主调用。
`run_command` 这类写盘/执行工具需要研究者批准令牌，而批准入口（谁批、在哪批）还没建，
所以**现在不摆给模型** —— 摆出去却没有审批链路，等于把边界开了个洞。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp_server.client import call_tool, server_params

#: 单轮对话里最多允许的「模型要求调工具」轮数。上限存在的意义是**防死循环**：
#: 模型可能反复要求调同一个工具，没有上限就会一直烧 token。
MAX_TOOL_ROUNDS = 3

#: 摆给模型自主调用的工具白名单（**只读**）。写/执行类不在这里，见模块 docstring。
AUTONOMOUS_TOOLS = ("query_library", "fetch_url")

#: 工具名 → 对话里那句话（给用户看的，不是给模型看的）
TOOL_LABELS = {
    "query_library": "查询论文库",
    "fetch_url": "抓取网页",
}


def agent_workspace() -> Path:
    """agent 的工作区（也是它唯一被允许写入的根）。

    放在 `server/.cache/agent-workspace`：与 ingest / reader / translate 的 `.cache`
    口径一致（`parents[2]` == server 根），且容器内该目录已由 compose 挂载。
    """

    return Path(__file__).resolve().parents[2] / ".cache" / "agent-workspace"


def _allowed_hosts() -> tuple[str, ...]:
    """出网白名单。**空 = 不摆出联网工具** —— 没配就默认没有联网能力。"""

    raw = os.environ.get("SCILOOP_AGENT_ALLOWED_HOSTS", "")
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def mcp_params() -> Any:
    """给 MCP client 的启动参数。授权在这里给出（不来自调用方）。"""

    import sys

    workspace = agent_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    return server_params(
        python=sys.executable,
        repo_server_dir=Path(__file__).resolve().parents[2],
        workspace=workspace,
        actor="agent:chat",
        allow_exec=False,  # 执行类要研究者批准，本轮不开
    )


async def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI 兼容的工具声明（只含当前允许自主调用的那些）。

    工具清单以**运行中的 MCP server** 为准（不硬编码参数），这样工具改了声明这里自动跟上；
    拿不到就返回空列表 —— 摆不出工具不该让整段对话失败。

    **必须是 async**：调用点在对话的 async 生成器里，用 `asyncio.run()` 会直接抛
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    """

    from mcp_server.client import list_tools

    wanted = set(AUTONOMOUS_TOOLS)
    if not _allowed_hosts():
        # 没配出网白名单 → 不摆 fetch_url（摆了也一定会被边界拒，不如不摆）
        wanted.discard("fetch_url")
    try:
        listed = await list_tools(mcp_params())
    except Exception:  # noqa: BLE001 - 工具侧不可用不该拖垮对话
        return []

    import json as _json

    schemas: list[dict[str, Any]] = []
    for item in listed:
        name = item.get("name")
        if name not in wanted:
            continue
        # MCP 的 inputSchema 就是 JSON Schema，直接当 OpenAI 的 parameters 用
        raw = item.get("inputSchema") or item.get("input_schema") or {"type": "object"}
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": item.get("description") or TOOL_LABELS.get(name, name),
                    "parameters": _json.loads(_json.dumps(raw)),
                },
            }
        )
    return schemas


def tool_row(call: dict[str, Any], phase: str, detail: str = "") -> dict[str, Any]:
    """工具调用在对话里的呈现（走现有 SSE `row` 事件，前端已能渲染）。"""

    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    label = TOOL_LABELS.get(name, name or "工具")
    if phase == "start":
        return {"kind": "tool", "tone": "info", "text": f"调用「{label}」{detail}".strip()}
    if phase == "ok":
        return {"kind": "tool", "tone": "ok", "text": f"「{label}」完成：{detail}"}
    return {"kind": "tool", "tone": "warn", "text": f"「{label}」未完成：{detail}"}


def _arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def run_tool_call(call: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """执行一次工具调用，返回 `(给模型看的结果, 一句话摘要)`。

    **失败也要把结果回给模型**：模型只有知道"这个工具失败了、原因是什么"，
    才会换个方式或如实告知用户；把失败吞掉会让它继续胡编。
    """

    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    if name not in AUTONOMOUS_TOOLS:
        return {"ok": False, "error": f"工具 {name} 不允许自主调用"}, f"{name} 不在自主白名单内"

    result = await call_tool(name, _arguments(call), mcp_params())
    if result.ok:
        data = result.data
        summary = _summarize(name, data)
        return {"ok": True, "tool": name, "result": data}, summary
    return (
        {"ok": False, "tool": name, "error": result.text or result.raw_error},
        result.raw_error or "工具返回错误",
    )


def _summarize(name: str, data: dict[str, Any]) -> str:
    """摘要只说事实（条数、状态码、字节数），不替模型解释内容。"""

    if name == "query_library":
        for key in ("papers", "items", "results", "matches"):
            value = data.get(key)
            if isinstance(value, list):
                return f"命中 {len(value)} 条"
        return "查询完成"
    if name == "fetch_url":
        status = data.get("status_code")
        size = data.get("bytes")
        return f"HTTP {status}，{size} 字节" if status is not None else "抓取完成"
    return "完成"


def tool_message_content(payload: dict[str, Any]) -> str:
    """把工具结果转成 `role=tool` 消息的正文。**截断但不撒谎**。"""

    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > 8000:
        return text[:8000] + "…（内容过长已截断）"
    return text
