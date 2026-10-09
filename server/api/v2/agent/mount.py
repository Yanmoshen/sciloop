# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""挂载补丁（Agent 3 不直接修改 ``server/main.py``）。

计划书 §3 明确：全局路由与应用挂载由最终协调 Agent 集成，Agent 3 只提供挂载补丁。
协调时在 ``server/main.py`` 的 ``ROUTER_REGISTRY`` 之后加三行即可：

.. code-block:: python

    # agent v2（Agent 3 交付）：WebSocket 双向会话 + 前端 agent-v2 store
    from api.v2.agent.mount import install as install_agent_v2
    install_agent_v2(app)

``install`` 只做两件事：初始化 :class:`~api.v2.agent.service.AgentV2Service` 单例，
并把 :func:`~api.v2.agent.router.build_router` 挂到 ``/api/v2``。它**不触碰**任何
v1 路由、不修改 CORS、不注册中间件，因此与现有功能零冲突。

注意：本模块只被显式调用时才有副作用，导入本身不改任何全局状态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from .router import MOUNT_PREFIX, WS_PATH, build_router
from .service import (
    AgentV2Service,
    default_conversations_root,
    default_memories_root,
    get_service,
    set_service,
)

#: 供协调 Agent 直接复制的挂载片段。
PATCH_SNIPPET = (
    "from api.v2.agent.mount import install as install_agent_v2\n"
    "install_agent_v2(app)"
)


def install(
    app: FastAPI,
    *,
    service: AgentV2Service | None = None,
    conversations_root: str | Path | None = None,
    memories_root: str | Path | None = None,
    **overrides: Any,
) -> AgentV2Service:
    """把 agent v2 挂到应用上，并返回服务实例。

    幂等：重复调用只挂一次路由（通过检查应用里是否已有本路由的路径）。
    """
    if service is not None:
        set_service(service)
        resolved = service
    else:
        resolved = get_service(
            conversations_root=conversations_root or default_conversations_root(),
            memories_root=memories_root or default_memories_root(),
            **overrides,
        )
    existing = {getattr(route, "path", "") for route in app.routes}
    if WS_PATH in existing or f"{MOUNT_PREFIX}/agent/health" in existing:
        return resolved
    app.include_router(build_router(get_service), prefix=MOUNT_PREFIX)
    return resolved


__all__ = ["install", "PATCH_SNIPPET", "WS_PATH", "MOUNT_PREFIX"]
