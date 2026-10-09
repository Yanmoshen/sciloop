# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""tool_registry_v2：工具声明、校验与执行入口（Agent 2 / WP-04）。

本包实现契约 ``ToolExecutor`` 协议（``specs`` / ``spec`` / ``execute``），
直接可注入 Agent 1 的 ``ToolScheduler``：

    from services.agent_runtime_v2 import ToolScheduler
    from services.tool_registry_v2 import ToolRegistry

    registry = ToolRegistry(sandbox=sandbox, host=host, approvals=approvals)
    scheduler = ToolScheduler(registry)
"""

from __future__ import annotations

from .definition import (
    CONTRACT_KIND,
    DEFAULT_MAX_OUTPUT_BYTES,
    IdempotencyMode,
    ToolCategory,
    ToolContext,
    ToolDefinition,
    ToolHandler,
)
from .events import (
    EVENT_TYPE_BY_STATUS,
    event_type_for,
    lifecycle_events,
    output_event,
    result_event,
    started_event,
)
from .mapping import (
    LEGACY_TO_NEW,
    LEGACY_TOOLS,
    MAPPING_NOTES,
    NEW_ONLY_TOOLS,
    legacy_tools_for,
    mapping_table,
    new_tools_for,
)
from .registry import ToolRegistry

__all__ = [
    "ToolRegistry",
    "ToolDefinition",
    "ToolCategory",
    "ToolContext",
    "ToolHandler",
    "IdempotencyMode",
    "CONTRACT_KIND",
    "DEFAULT_MAX_OUTPUT_BYTES",
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
