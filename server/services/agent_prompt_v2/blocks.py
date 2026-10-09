"""提示词区块模型：固定顺序、可单独测试、可被清洗器过滤。

设计目标（计划书 WP-03）：

- 提示词由**固定区块**拼成，路由不再自己拼 system string；
- 每个区块可单独构造与断言，因此可测试；
- 只有事实进提示词，**内部术语与调试状态一律不得进入**（权限由 Agent 2 的执行器
  强制，模型不能通过提示词取得权限）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BlockKind(StrEnum):
    """区块类型。值即渲染出的标题标识，顺序由 :data:`BLOCK_ORDER` 决定。"""

    BASE_INSTRUCTIONS = "base_instructions"
    RUNTIME_FACTS = "runtime_facts"
    TASK_CONTEXT = "task_context"
    RESEARCH_CONTEXT = "research_context"


#: 固定区块顺序——渲染器只按这个顺序输出，不接受调用方自定义顺序。
BLOCK_ORDER: tuple[BlockKind, ...] = (
    BlockKind.BASE_INSTRUCTIONS,
    BlockKind.RUNTIME_FACTS,
    BlockKind.TASK_CONTEXT,
    BlockKind.RESEARCH_CONTEXT,
)


@dataclass
class PromptBlock:
    """一个提示词区块。``body`` 为空表示该区块不出现。"""

    kind: BlockKind
    title: str
    body: str = ""
    #: 结构化的原始事实，便于测试断言「什么进了提示词」
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return bool(self.body.strip())

    def render(self) -> str:
        if not self.present:
            return ""
        return f"## {self.title}\n{self.body.strip()}"


def render_blocks(blocks: list[PromptBlock]) -> str:
    """按 :data:`BLOCK_ORDER` 渲染，缺失的区块跳过，空区块不出现。"""
    by_kind: dict[BlockKind, PromptBlock] = {}
    for block in blocks:
        # 同类型重复时后者覆盖前者（避免拼接出两份互相矛盾的同一区块）
        by_kind[block.kind] = block
    parts: list[str] = []
    for kind in BLOCK_ORDER:
        block = by_kind.get(kind)
        if block is None:
            continue
        text = block.render()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


__all__ = ["BlockKind", "BLOCK_ORDER", "PromptBlock", "render_blocks"]
