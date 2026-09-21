# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""后端侧 MCP client：拉起 SciLoop 自建 server 并按协议调用工具。

这一层是「SciLoop 内部使用 MCP」的接缝：后端不直接 import 工具函数，
而是走标准协议 —— 于是同一个工具既能被自家 agent 用，也能（换 transport 后）被外部 agent 用，
不需要两套实现。

标准输出被协议占用（stdio 传 JSON-RPC），所以**绝不能**在这里 print 调试信息：
server 侧任何打到 stdout 的日志都会污染协议帧，表现为连接挂起或 JSON 解析失败。
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@dataclass
class ToolCall:
    """一次工具调用的结果。**`ok` 与 `error_code` 分开**，便于上层分支。"""

    tool: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    error_code: str | None = None
    raw_error: str = ""

    @property
    def approval_required(self) -> bool:
        return self.error_code == "approval_required"


def server_params(
    *,
    workspace: str | os.PathLike[str] | None = None,
    read_roots: tuple[str, ...] = (),
    allow_exec: bool = False,
    allow_net: bool = False,
    allowed_hosts: tuple[str, ...] = (),
    approval_tokens: tuple[str, ...] = (),
    actor: str = "agent",
    python: str | None = None,
    repo_server_dir: str | os.PathLike[str] | None = None,
) -> StdioServerParameters:
    """构造启动参数。**授权只在这里给出**，与被调用方隔离。

    ⚠️ 每个门读的环境变量都必须在**这里**写进去：`guard.grant_from_env()` 只认
    `SCILOOP_MCP_*`，而子进程的环境由本函数构造。此前漏了 `ALLOW_NET` / `ALLOWED_HOSTS`，
    于是"配了白名单就把 fetch_url 摆给模型"与"子进程里 allow_net 恒为 False"两边错位 ——
    工具摆得出去、调用必被 `tool_denied`。授权链断在最不起眼的一环。
    """

    env = {
        "SCILOOP_MCP_ACTOR": actor,
        "SCILOOP_MCP_ALLOW_EXEC": "1" if allow_exec else "0",
        "SCILOOP_MCP_ALLOW_NET": "1" if allow_net else "0",
        "SCILOOP_MCP_APPROVAL_TOKENS": ",".join(approval_tokens),
        "PYTHONUNBUFFERED": "1",
    }
    if workspace is not None:
        env["SCILOOP_MCP_WORKSPACE"] = str(workspace)
    if read_roots:
        env["SCILOOP_MCP_READ_ROOTS"] = os.pathsep.join(read_roots)
    if allowed_hosts:
        env["SCILOOP_MCP_ALLOWED_HOSTS"] = ",".join(allowed_hosts)

    return StdioServerParameters(
        command=python or sys.executable,
        args=["-m", "mcp_server.server"],
        env=env,
        cwd=str(repo_server_dir) if repo_server_dir is not None else None,
    )


async def list_tools(params: StdioServerParameters) -> list[dict[str, Any]]:
    """按协议列出工具声明（不是读本地代码，是走一次 MCP 握手）。

    ⚠️ **`input_schema` 必须带回去**（mcp 2.x 是蛇形字段名，旧教程里的 `inputSchema`
    在 Pydantic 模型上取不到）。少了它，模型看到的声明就是"一个不接受任何参数的工具"：
    它只能从描述文字里猜参数名，猜错就被协议层按 schema 拒（`rejected arguments`），
    而现象是"工具明明在，却总调不对" —— 链条上最不起眼的一环又一次把授权/信息吞掉了。
    """

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": getattr(tool, "input_schema", None)
                or getattr(tool, "inputSchema", None)
                or {"type": "object"},
            }
            for tool in listed.tools
        ]


async def call_tool(
    name: str, arguments: dict[str, Any], params: StdioServerParameters
) -> ToolCall:
    """调用一个工具。返回结构化结果，**不抛异常给上层**（错误码放 `error_code`）。"""

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(name, arguments)

    texts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    joined = "\n".join(texts)
    payload = _first_json(joined)

    # ⚠️ mcp 2.x 是 Pydantic 模型，字段是蛇形的 `is_error`（旧教程里的 `isError` 会 AttributeError）
    if result.is_error:
        return ToolCall(
            tool=name,
            ok=False,
            text=joined,
            error_code=_error_code(joined),
            raw_error=joined,
        )
    return ToolCall(tool=name, ok=True, data=payload or {"text": joined}, text=joined)


def _first_json(text: str) -> dict[str, Any] | None:
    """工具若返回结构化内容，MCP 会把它序列化成 JSON 文本，这里取回来。"""

    stripped = text.strip()
    for candidate in (stripped, *[line for line in stripped.splitlines() if line.strip()]):
        if candidate.startswith("{"):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _error_code(text: str) -> str | None:
    """从 `[code] message` 前缀里取回错误码；取不到就明确返回 None（不猜）。"""

    # **不能只判开头**：SDK 会在我们的错误前面再加一层
    # `Error executing tool <name>: [code] ...`，所以必须在全文里找方括号。
    match = re.search(r"\[([a-z_]{3,40})\]", text)
    return match.group(1) if match else None


def default_repo_server_dir() -> Path:
    """`server/` 目录（即导入根），供 cwd 使用。"""

    return Path(__file__).resolve().parents[1]
