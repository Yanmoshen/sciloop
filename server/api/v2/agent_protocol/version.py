# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""协议版本常量。

协议版本与契约版本**分开**：

- 契约 ``agent.v2.contract.v1`` 定义持久化对象与事件（冻结在
  ``server/contracts/agent_v2/``，本线只读）；
- 协议 ``agent.v2.protocol.v1`` 定义 WebSocket 信封与方法（本目录）。

两者可以独立演进：信封格式变化不动事件格式，反之亦然。
"""

from __future__ import annotations

PROTOCOL_VERSION = "agent.v2.protocol.v1"

#: 本构建能读懂的协议版本。
READABLE_PROTOCOL_VERSIONS: tuple[str, ...] = (PROTOCOL_VERSION,)


def is_readable(version: object) -> bool:
    """判断 ``version`` 是否为本构建可读的协议版本。"""
    return isinstance(version, str) and version in READABLE_PROTOCOL_VERSIONS


__all__ = ["PROTOCOL_VERSION", "READABLE_PROTOCOL_VERSIONS", "is_readable"]
