# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Agent v2 对外 API 与前端协议（Agent 3 / WP-09 + WP-10）。

本包是**新增的 v2 命名空间**：不修改旧 ``server/api/v1/`` 的任何行为，也不修改
``server/main.py``；挂载由协调 Agent 通过本包的 :mod:`api.v2.agent.mount` 完成。
"""

from __future__ import annotations

__all__: list[str] = []
