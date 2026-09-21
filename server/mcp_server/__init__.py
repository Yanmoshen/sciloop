# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SciLoop 自建 MCP server（工具自己提供，给自家 agent 用）。

三块：
- `guard`  —— 边界层：能力 / 路径 / 审批 / 审计 四道门，授权来自进程启动配置
- `tools`  —— 暴露给 agent 的工具实现（只读业务工具 + 需批准的执行工具）
- `server` —— MCP 组装与 stdio 入口
- `client` —— 后端侧 client（走标准协议，不直接 import 工具函数）
"""

from __future__ import annotations

__all__ = ["client", "guard", "server", "tools"]
