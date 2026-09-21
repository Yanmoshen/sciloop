# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""暴露给 agent 的 MCP 工具。

两个工具各代表一类，故意选得不一样，好把边界的两种形态都立住：

- `query_library`：**只读业务工具**——真去查 SciLoop 的论文库与对话链（走 `services.research.lookup`），
  自动放行。它证明"SciLoop 自己的能力已经能通过 MCP 被 agent 调用"。
- `run_command`：**高风险执行工具**——`argv` 逐项传入（不经过 shell）、命中程序白名单、
  且必须带**研究者批准令牌**。它是合规 8.3「研究者接管/批准」的落点。

工具实现**不自己判权限**，一律先过 `Guard` 的四道门；这样边界只有一处，不会各写各的。
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as McpToolError

from mcp_server.guard import Guard, GuardError

#: 可执行白名单。**故意很短**：够跑脚本/测试/检索即可，不是通用 shell。
ALLOWED_BINARIES = (
    "python", "python3", "pytest", "ruff", "git",
    "ls", "cat", "head", "tail", "wc", "grep", "rg",
    "sed", "awk", "sort", "uniq", "find",
)

#: 单次执行可回传的输出上限。
MAX_OUTPUT_BYTES = 32 * 1024

#: 单次抓取回传的字节上限。
MAX_FETCH_BYTES = 256 * 1024


async def _through_guard(
    guard: Guard,
    tool: str,
    params: dict[str, Any],
    body: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """统一出入口：计时 → 执行 → **成功与拒绝都审计**。

    拒绝也记是有意的：一条「agent 试图执行但被边界挡下」的记录，
    比一条成功调用更能证明边界在工作。
    """

    started = time.perf_counter()
    try:
        data = await body()
    except GuardError as exc:
        guard.audit(
            tool=tool,
            params=params,
            ok=False,
            code=exc.code,
            ms=(time.perf_counter() - started) * 1000,
            detail=exc.detail,
        )
        # **必须转成 SDK 认的 ToolError**：实测普通异常会被包成 UnexpectedToolError
        # 直接往客户端抛异常（客户端拿不到结构化错误、只知道"炸了"），
        # 而 SDK 的 ToolError 才会变成一个 is_error 的工具结果，把错误码原样带回去。
        raise McpToolError(f"[{exc.code}] {exc.message}") from exc
    guard.audit(
        tool=tool,
        params=params,
        ok=True,
        code=None,
        ms=(time.perf_counter() - started) * 1000,
        touched=list(data.get("touched", [])),
    )
    return data


def _as_dict(value: Any) -> dict[str, Any]:
    """把业务返回值安全地变成可 JSON 序列化的 dict（不猜字段名）。"""

    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    return {"text": str(value)}


def register(server: MCPServer, guard: Guard) -> None:
    """把工具注册到 MCP server 上。**每个工具先过 Guard，再碰真实世界。**"""

    @server.tool(
        name="query_library",
        description=(
            "查询 SciLoop 论文库与本研究对话的既有产物（论文、项目、制品、对话链）。"
            "只读、自动放行。用于回答「这个方向库里已有哪些材料/做过什么」。"
        ),
    )
    async def query_library(topic: str, text: str, conversation_id: str = "") -> dict[str, Any]:
        async def body() -> dict[str, Any]:
            guard.require("query_library", ("read",))
            from db.session import AsyncSessionLocal
            from services.research.lookup import run_lookup

            if AsyncSessionLocal is None:
                raise GuardError(
                    "tool_failed",
                    "数据库会话工厂不可用（AsyncSessionLocal 为 None）：无法查询论文库",
                )
            async with AsyncSessionLocal() as session:
                result = await run_lookup(
                    session, topic=topic, text=text, conversation_id=conversation_id
                )
            data = _as_dict(result)
            data["topic"] = topic
            return {"touched": [], **data}

        return await _through_guard(
            guard,
            "query_library",
            {"topic": topic, "text": text, "conversation_id": conversation_id},
            body,
        )

    @server.tool(
        name="run_command",
        description=(
            "在授权工作区内执行一个白名单程序（argv 逐项传入，不经过 shell）。"
            "需要研究者批准令牌；非零退出码会原样回报，不算工具失败。"
        ),
    )
    async def run_command(
        argv: list[str],
        approval_token: str | None = None,
        cwd: str | None = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        params = {"argv": argv, "approval_token": approval_token, "cwd": cwd, "timeout_s": timeout_s}

        async def body() -> dict[str, Any]:
            guard.require("run_command", ("read", "exec"))
            guard.require_approval("run_command", approval_token)

            if not isinstance(argv, list) or not argv or not all(isinstance(i, str) for i in argv):
                # 实测：走 MCP 时这一步**收不到** —— 协议层按工具 schema 先拒了
                # （`Tool 'run_command' rejected arguments: ['argv']`）。
                # 留着是纵深防御：本函数也可能被同进程直接调用，那时没有协议层兜底。
                raise GuardError(
                    "tool_bad_params",
                    "argv 必须是「非空字符串列表」；不要传整条 shell 字符串",
                )
            binary = os.path.basename(argv[0])
            guard.assert_binary("run_command", binary, ALLOWED_BINARIES)
            resolved = shutil.which(argv[0])
            if resolved is None:
                raise GuardError("tool_failed", f"找不到可执行文件：{argv[0]}")

            workdir = guard.resolve("run_command", cwd or ".", for_write=False)
            if not workdir.is_dir():
                raise GuardError("tool_failed", f"工作目录不存在：{workdir}")

            budget = max(0.5, min(float(timeout_s), 300.0))
            try:
                proc = subprocess.run(  # noqa: S603 - argv 逐项传入、程序已过白名单，不经 shell
                    [resolved, *argv[1:]],
                    cwd=str(workdir),
                    capture_output=True,
                    timeout=budget,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise GuardError(
                    "tool_timeout", f"命令在 {budget}s 内未结束，已终止：{' '.join(argv)}"
                ) from None

            stdout, out_cut = _clip(proc.stdout)
            stderr, err_cut = _clip(proc.stderr)
            return {
                "argv": argv,
                "cwd": str(workdir),
                # ok 与 exit_code 分开给：命令跑成了、只是返回非零，不是工具失败
                "exit_code": proc.returncode,
                "ok": proc.returncode == 0,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": out_cut,
                "stderr_truncated": err_cut,
                "touched": [],
            }

        return await _through_guard(guard, "run_command", params, body)

    @server.tool(
        name="fetch_url",
        description=(
            "抓取一个网址的内容（只允许授权白名单内的主机）。"
            "**不自动跟随重定向** —— 否则一个 302 就能绕过白名单。"
        ),
    )
    async def fetch_url(url: str, max_bytes: int | None = None) -> dict[str, Any]:
        params = {"url": url, "max_bytes": max_bytes}

        async def body() -> dict[str, Any]:
            guard.require("fetch_url", ("net",))
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise GuardError("tool_bad_params", f"只接受 http/https 绝对网址，收到：{url}")
            guard.assert_host("fetch_url", parsed.hostname, parsed.port)

            # 延迟导入：抓取才需要 httpx，门禁类测试（拒绝路径）不该被依赖拖住
            import httpx

            limit = max(1, min(int(max_bytes or MAX_FETCH_BYTES), MAX_FETCH_BYTES))
            try:
                async with httpx.AsyncClient(
                    follow_redirects=False,  # 见 assert_host：跟重定向等于让白名单失效
                    timeout=httpx.Timeout(20.0),
                ) as client:
                    response = await client.get(url)
            except httpx.HTTPError as exc:
                raise GuardError("tool_failed", f"抓取失败：{type(exc).__name__}: {exc}") from exc

            raw = response.content[:limit]
            return {
                "url": str(response.url),
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "bytes": len(raw),
                "truncated": len(response.content) > len(raw),
                "text": raw.decode("utf-8", errors="replace"),
                "redirect_to": response.headers.get("location"),
                "touched": [],
            }

        return await _through_guard(guard, "fetch_url", params, body)


def _clip(raw: bytes) -> tuple[str, bool]:
    clipped = raw[:MAX_OUTPUT_BYTES]
    return clipped.decode("utf-8", errors="replace"), len(raw) > len(clipped)
