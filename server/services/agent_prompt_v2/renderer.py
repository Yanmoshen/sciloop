"""提示词渲染器：固定顺序、按预算截断、可整体自检。

路由不再自己拼 system string——它只提供**事实**，由本模块决定：
1. 出现哪些区块（``base`` 恒在；``research`` 仅当已确认研究）；
2. 区块顺序（由 :data:`~.blocks.BLOCK_ORDER` 固定）；
3. 总长度预算（超预算时按「先砍任务上下文细节，后砍记忆」的顺序收缩，
   ``base instructions`` 永不被砍）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .blocks import BLOCK_ORDER, BlockKind, PromptBlock, render_blocks
from .context import (
    build_research_context,
    build_runtime_facts,
    build_task_context,
    scrub_facts,
    select_memories,
)
from .instructions import build_base_instructions

#: 默认总预算（字符）。base instructions 约占 500，其余留给事实。
DEFAULT_BUDGET = 6000


@dataclass
class RenderedPrompt:
    """渲染结果 + 自检信息（便于测试与审计）。"""

    text: str
    blocks: list[PromptBlock]
    truncated: bool = False

    @property
    def kinds(self) -> list[BlockKind]:
        return [b.kind for b in self.blocks if b.present]

    def contains(self, needle: str) -> bool:
        return needle in self.text

    def to_dict(self) -> dict[str, Any]:
        return {
            "length": len(self.text),
            "blocks": [b.kind.value for b in self.blocks if b.present],
            "truncated": self.truncated,
        }


def render_prompt(
    *,
    model: str | None = None,
    workspace: str | None = None,
    tools: Sequence[Mapping[str, Any]] = (),
    permission_facts: Mapping[str, Any] | None = None,
    turn_status: str | None = None,
    now: str | None = None,
    goal: str | None = None,
    project: str | None = None,
    approved: Sequence[str] = (),
    todos: Sequence[str] = (),
    blocked: Sequence[str] = (),
    errors: Sequence[str] = (),
    memories: Sequence[Any] = (),
    memory_scopes: Sequence[str] = (),
    memory_max_items: int = 8,
    research_confirmed: bool = False,
    research_phase: str | None = None,
    research_question: str | None = None,
    research_sources: Sequence[str] = (),
    extra_task_facts: Mapping[str, Any] | None = None,
    budget: int = DEFAULT_BUDGET,
) -> RenderedPrompt:
    """按固定区块顺序渲染 system prompt。"""
    memory_snippets = select_memories(
        memories, scopes=memory_scopes, max_items=memory_max_items
    )

    def assemble(mem: Sequence[str], detail_scale: int) -> list[PromptBlock]:
        return [
            build_base_instructions(),
            build_runtime_facts(
                model=model,
                workspace=workspace,
                tools=tools,
                permission_facts=permission_facts,
                turn_status=turn_status,
                now=now,
            ),
            build_task_context(
                goal=goal,
                project=project,
                approved=approved,
                todos=todos[:detail_scale] if detail_scale else [],
                blocked=blocked[:detail_scale] if detail_scale else [],
                errors=errors[:detail_scale] if detail_scale else [],
                memories=mem,
                extra=scrub_facts(extra_task_facts),
            ),
            build_research_context(
                research_confirmed=research_confirmed,
                phase=research_phase,
                question=research_question,
                sources=research_sources,
            ),
        ]

    blocks = assemble(memory_snippets, 99)
    text = render_blocks(blocks)
    truncated = False

    if len(text) > budget:
        # 收缩顺序：待办/阻塞/错误明细 → 记忆 → 研究举证来源
        truncated = True
        blocks = assemble(memory_snippets[:4], 5)
        text = render_blocks(blocks)
    if len(text) > budget:
        blocks = assemble([], 5)
        text = render_blocks(blocks)
    if len(text) > budget:
        blocks = assemble([], 0)
        text = render_blocks(blocks)

    return RenderedPrompt(text=text, blocks=blocks, truncated=truncated)


def prompt_block_order() -> tuple[BlockKind, ...]:
    """暴露固定顺序，供测试断言（不返回可变副本）。"""
    return BLOCK_ORDER


__all__ = ["DEFAULT_BUDGET", "RenderedPrompt", "prompt_block_order", "render_prompt"]
