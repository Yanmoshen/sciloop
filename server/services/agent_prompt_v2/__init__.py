"""agent_prompt_v2：分层系统提示词（计划书 WP-03）。

路由不再自己拼 system string，只提供事实；本包负责：
出现哪些区块、区块顺序、长度预算，以及**清洗掉不该给模型看的东西**。

    from services.agent_prompt_v2 import render_prompt

    rendered = render_prompt(
        model="…", workspace="…", tools=specs,
        permission_facts={"已授权范围": "工作区内读写"},
        goal="梳理联邦学习的隐私风险",
        research_confirmed=False,
    )
    system_prompt = rendered.text

区块顺序固定为：base instructions → runtime facts → task context → research context。
``research context`` 只在 ``research_confirmed`` 为真时出现。
"""

from __future__ import annotations

from .blocks import BLOCK_ORDER, BlockKind, PromptBlock, render_blocks
from .context import (
    FORBIDDEN_KEYS,
    RESEARCH_RULES,
    build_research_context,
    build_runtime_facts,
    build_task_context,
    scrub_facts,
    select_memories,
)
from .instructions import BASE_INSTRUCTIONS, RETIRED_PHRASES, build_base_instructions
from .renderer import DEFAULT_BUDGET, RenderedPrompt, prompt_block_order, render_prompt

__all__ = [
    "BLOCK_ORDER",
    "BlockKind",
    "PromptBlock",
    "render_blocks",
    "FORBIDDEN_KEYS",
    "RESEARCH_RULES",
    "build_research_context",
    "build_runtime_facts",
    "build_task_context",
    "scrub_facts",
    "select_memories",
    "BASE_INSTRUCTIONS",
    "RETIRED_PHRASES",
    "build_base_instructions",
    "DEFAULT_BUDGET",
    "RenderedPrompt",
    "prompt_block_order",
    "render_prompt",
]
