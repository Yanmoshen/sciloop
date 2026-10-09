# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""Agent v2 API 层（Agent 3 / WP-09）。

    from api.v2.agent import AgentV2Service, build_router, install

    service = AgentV2Service(conversations_root="knowledge-base/conversations")
    response = await service.facade.dispatch(request, service.new_call_context())

组成：

- :mod:`~api.v2.agent.facade` —— 协议方法到领域调用的门面（无业务逻辑）；
- :mod:`~api.v2.agent.host` —— 离线运行时宿主（fixture 驱动的模型与工具替身）；
- :mod:`~api.v2.agent.service` —— 服务装配、连接管理、事件推送与关闭语义；
- :mod:`~api.v2.agent.ws` —— WebSocket 会话循环；
- :mod:`~api.v2.agent.router` / :mod:`~api.v2.agent.mount` —— 路由与挂载补丁。
"""

from __future__ import annotations

from .auth import AccessDecision, check_websocket
from .facade import AgentFacade, CallContext
from .fixtures import Scenario, available_scenarios, load_scenario
from .host import FakeRuntimeHost, ScenarioToolExecutor, normalize_command_prefix
from .mount import PATCH_SNIPPET, install
from .router import MOUNT_PREFIX, WS_PATH, build_router
from .service import (
    AgentV2Service,
    default_conversations_root,
    default_memories_root,
    get_service,
    set_service,
)

__all__ = [
    "AgentFacade",
    "CallContext",
    "AgentV2Service",
    "FakeRuntimeHost",
    "ScenarioToolExecutor",
    "Scenario",
    "load_scenario",
    "available_scenarios",
    "normalize_command_prefix",
    "AccessDecision",
    "check_websocket",
    "build_router",
    "install",
    "PATCH_SNIPPET",
    "MOUNT_PREFIX",
    "WS_PATH",
    "get_service",
    "set_service",
    "default_conversations_root",
    "default_memories_root",
]
