# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""agent_tree_tools_v2：子 Agent 工具入口与校验（Agent 2 / WP-06）。

    from services.agent_tree_tools_v2 import agent_tree_tool_definitions, validate

    registry.register_all(agent_tree_tool_definitions())
    validate("agent.spawn", {"name": "x", "task": "y"})   # -> []
"""

from __future__ import annotations

from .entrypoints import (
    agent_tree_tool_definitions,
    close_definition,
    interrupt_definition,
    run_close,
    run_interrupt,
    run_send,
    run_spawn,
    run_wait,
    send_definition,
    spawn_definition,
    wait_definition,
)
from .validation import (
    MAX_CONTENT_LEN,
    MAX_NAME_LEN,
    VALIDATORS,
    is_own_child,
    validate,
)

__all__ = [
    "agent_tree_tool_definitions",
    "spawn_definition",
    "send_definition",
    "wait_definition",
    "interrupt_definition",
    "close_definition",
    "run_spawn",
    "run_send",
    "run_wait",
    "run_interrupt",
    "run_close",
    "validate",
    "is_own_child",
    "VALIDATORS",
    "MAX_NAME_LEN",
    "MAX_CONTENT_LEN",
]
