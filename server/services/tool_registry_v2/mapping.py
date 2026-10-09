# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""旧工具能力 → 新工具能力的映射表（Agent 2 / WP-04）。

计划书 §4.1：「工具名称可以重新设计，但必须提供旧能力到新能力的映射表，供集成阶段使用」。
这份表是**集成期唯一的对照口径**，并由测试保证"旧白名单里的每个工具都已映射"。
"""

from __future__ import annotations

from typing import Any

#: 旧 Agent 的工具白名单（来自 ``services/agent/mcp_tools.py``，只读引用其名字）
LEGACY_TOOLS: tuple[str, ...] = (
    "query_library",
    "search_academic",
    "search_web",
    "fetch_url",
    "load_skill",
    "run_command",
    "run_on_computer",
    "files_on_computer",
)

#: 旧 → 新（一个旧能力可以对应多个新工具）
LEGACY_TO_NEW: dict[str, tuple[str, ...]] = {
    "query_library": ("kb.query",),
    "search_academic": ("web.search",),
    "search_web": ("web.search",),
    "fetch_url": ("web.fetch",),
    "load_skill": ("skill.load",),
    "run_skill": ("skill.run",),
    "run_command": ("host.exec",),
    "run_on_computer": ("host.exec",),
    "files_on_computer": (
        "host.file.list",
        "host.file.read",
        "host.file.write",
        "host.file.move",
        "host.file.delete",
    ),
}

#: 新增能力（旧 Agent 没有）
NEW_ONLY_TOOLS: tuple[str, ...] = (
    "kb.write",
    "mcp.call",
    "spawn_agent",
    "send_message",
    "wait_agent",
    "interrupt_agent",
    "close_agent",
)

#: 说明：旧 ``files_on_computer`` 的 ``copy`` 分支在新体系里对应
#: ``host.file.read`` + ``host.file.write``（先读后写），不再有"复制"这个独立动作。
MAPPING_NOTES: tuple[str, ...] = (
    "run_command 与 run_on_computer 合并为 host.exec（不再区分容器/真机概念）",
    "files_on_computer 的 copy 动作由 host.file.read + host.file.write 组合表达",
    "search_academic 与 search_web 合并为 web.search，用 mode 区分",
    "审批令牌不再是模型可见参数，因此 run_command 的 token 概念在新体系里消失",
)


def new_tools_for(legacy: str) -> tuple[str, ...]:
    """旧工具名 → 新工具名（未知返回空元组）。"""
    return LEGACY_TO_NEW.get(legacy, ())


def legacy_tools_for(new_name: str) -> tuple[str, ...]:
    """新工具名 → 它替代了哪些旧工具。"""
    return tuple(
        legacy for legacy, targets in LEGACY_TO_NEW.items() if new_name in targets
    )


def mapping_table() -> list[dict[str, Any]]:
    """交付用映射表（含方向与说明）。"""
    rows: list[dict[str, Any]] = [
        {"legacy": legacy, "new": list(LEGACY_TO_NEW.get(legacy, ())), "direction": "replaced"}
        for legacy in LEGACY_TOOLS
    ]
    rows.extend(
        {"legacy": None, "new": [name], "direction": "new"} for name in NEW_ONLY_TOOLS
    )
    rows[-1]["notes"] = list(MAPPING_NOTES)
    return rows


__all__ = [
    "LEGACY_TOOLS",
    "LEGACY_TO_NEW",
    "NEW_ONLY_TOOLS",
    "MAPPING_NOTES",
    "new_tools_for",
    "legacy_tools_for",
    "mapping_table",
]
