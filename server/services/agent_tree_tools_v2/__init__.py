# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""agent_tree_tools_v2：子 Agent 工具入口（Agent 2 / WP-05 §4.5）。

    from services.agent_tree_tools_v2 import agent_tree_tool_definitions

    registry.register_all(agent_tree_tool_definitions())
"""

from __future__ import annotations

from .adapter import (
    agent_tree_tool_definitions,
    close_agent_definition,
    interrupt_agent_definition,
    run_close_agent,
    run_interrupt_agent,
    run_send_message,
    run_spawn_agent,
    run_wait_agent,
    send_message_definition,
    spawn_agent_definition,
    wait_agent_definition,
)

__all__ = [
    "agent_tree_tool_definitions",
    "spawn_agent_definition",
    "send_message_definition",
    "wait_agent_definition",
    "interrupt_agent_definition",
    "close_agent_definition",
    "run_spawn_agent",
    "run_send_message",
    "run_wait_agent",
    "run_interrupt_agent",
    "run_close_agent",
]
