# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""tool_registry_v2：工具声明、校验、调度与执行入口（Agent 2 / WP-01）。

    from services.tool_registry_v2 import ToolRegistry, ToolGateway
    from services.tool_registry_v2.builtin import build_default_registry

    registry = build_default_registry(sandbox=sandbox, host=host, approvals=approvals)
    gateway = ToolGateway(registry)
    gateway.list_tools()               # Agent 3 侧
    gateway.evaluate(call)             # Agent 1 侧
"""

from __future__ import annotations

from . import schemas
from .events import (
    EVENT_TYPE_BY_STATUS,
    event_type_for,
    lifecycle_events,
    output_event,
    result_event,
    started_event,
)
from .facade import ALLOW, REQUIRE, ApprovalEventStream, ToolGateway
from .mapping import (
    LEGACY_TO_NEW,
    LEGACY_TOOLS,
    MAPPING_NOTES,
    NEW_ONLY_TOOLS,
    legacy_tools_for,
    mapping_table,
    new_tools_for,
)
from .models import (
    APPROVAL_CLASSES,
    CONTRACT_KIND,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    IdempotencyMode,
    PermissionClass,
    SideEffect,
    ToolContext,
    ToolDefinition,
    ToolHandler,
)
from .registry import ToolRegistry
from .scheduler import ToolScheduler

__all__ = [
    "ToolRegistry",
    "ToolScheduler",
    "ToolGateway",
    "ApprovalEventStream",
    "ALLOW",
    "REQUIRE",
    "ToolDefinition",
    "PermissionClass",
    "SideEffect",
    "IdempotencyMode",
    "ToolContext",
    "ToolHandler",
    "CONTRACT_KIND",
    "APPROVAL_CLASSES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "schemas",
    "EVENT_TYPE_BY_STATUS",
    "event_type_for",
    "started_event",
    "output_event",
    "result_event",
    "lifecycle_events",
    "LEGACY_TOOLS",
    "LEGACY_TO_NEW",
    "NEW_ONLY_TOOLS",
    "MAPPING_NOTES",
    "new_tools_for",
    "legacy_tools_for",
    "mapping_table",
]
