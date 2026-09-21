# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SciLoop 自建 MCP server：把自家能力按规范协议暴露给自家 agent。

启动（stdio，由后端拉起或本机 agent 直连）：

    python -m mcp_server.server

授权**全部来自启动环境**（见 `guard.grant_from_env`），调用方无法在参数里放宽边界。
将来要对外暴露（给别的机器/网页端），只需把 `run(transport="stdio")` 换成
`"streamable-http"` —— 协议与工具定义一行不用改。
"""

from __future__ import annotations

import importlib.metadata as metadata

from mcp.server.mcpserver import MCPServer

from mcp_server.guard import Guard, grant_from_env
from mcp_server.tools import register

SERVER_NAME = "sciloop"

INSTRUCTIONS = """\
这是 SciLoop（可审计科研流水线）暴露给 agent 的能力入口。

- `query_library`：查论文库与本研究对话的既有产物（只读，自动放行）。
- `run_command`：在工作区内执行白名单程序（**需要研究者批准令牌**）。

边界不由调用方决定：可写目录、可执行程序、批准令牌都在 server 启动时从环境读入。
越权会被拒绝并带上错误码；被拒也会记入审计。
"""


def _version() -> str:
    try:
        return metadata.version("sciloop-server")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def build_server(guard: Guard) -> MCPServer:
    """组装 server。工具注册与授权分离，便于测试里塞不同授权。"""

    server = MCPServer(
        name=SERVER_NAME,
        title="SciLoop",
        description="SciLoop 的科研能力 MCP 入口（论文库查询 / 受控命令执行）",
        instructions=INSTRUCTIONS,
        version=_version(),
    )
    register(server, guard)
    return server


def main() -> None:
    """stdio 入口。授权取自环境变量，本进程不做任何交互式询问。"""

    build_server(Guard(grant=grant_from_env())).run(transport="stdio")


if __name__ == "__main__":
    main()
