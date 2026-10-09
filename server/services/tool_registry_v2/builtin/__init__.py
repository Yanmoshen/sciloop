# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""内置工具集合与默认注册表（Agent 2 / WP-01）。

    from services.tool_registry_v2.builtin import build_default_registry

    registry = build_default_registry(
        sandbox=sandbox, host=manager, approvals=approvals, agent_tree=tree,
        cwd=workspace, search=my_search_backend,
    )
    len(registry.names()) == 18   # 宿主机 6 + 服务 7 + 子 Agent 5
"""

from __future__ import annotations

from typing import Any

from services.agent_tree_tools_v2 import agent_tree_tool_definitions
from services.tool_registry_v2.models import ToolDefinition
from services.tool_registry_v2.registry import ToolRegistry

from .host_tools import host_tool_definitions
from .service_tools import service_tool_definitions

__all__ = [
    "default_tool_definitions",
    "build_default_registry",
    "host_tool_definitions",
    "service_tool_definitions",
]


def default_tool_definitions() -> list[ToolDefinition]:
    """全部内置工具声明（宿主机 6 + 服务 7 + 子 Agent 5 = 18）。"""
    return [
        *host_tool_definitions(),
        *service_tool_definitions(),
        *agent_tree_tool_definitions(),
    ]


def build_default_registry(**keyword: Any) -> ToolRegistry:
    """构造带全部内置工具的注册表。

    可识别的构造参数：``clock`` / ``sandbox`` / ``host`` / ``approvals`` /
    ``agent_tree`` / ``cwd`` / ``services`` / ``validate_output``；
    其余关键字参数一律当作后端注入（``search`` / ``fetch`` / ``knowledge_search`` …）。
    """
    constructor_keys = {
        "clock",
        "sandbox",
        "host",
        "approvals",
        "agent_tree",
        "services",
        "cwd",
        "default_cwd",
        "max_output_bytes",
        "validate_output",
    }
    init_kwargs = {key: value for key, value in keyword.items() if key in constructor_keys}
    if "cwd" in init_kwargs and "default_cwd" not in init_kwargs:
        init_kwargs["default_cwd"] = init_kwargs.pop("cwd")
    bindings = {key: value for key, value in keyword.items() if key not in constructor_keys}

    registry = ToolRegistry(**init_kwargs)
    registry.register_all(default_tool_definitions())
    if bindings:
        registry.bind(**bindings)
    return registry
