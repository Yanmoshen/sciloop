"""base instructions：直接、自然、按任务复杂度组织输出（Codex 风格）。

与被停用的旧风格对比（计划书 WP-03「删除或停用」清单）：

| 旧做法 | 现在 |
|---|---|
| 固定四段式 | 由任务复杂度决定结构；简单问题直接答 |
| 强制表格 | 只在多维度对比时才用表 |
| 强制 a/b/c 选项 | 有明确推荐就直接给推荐并说明代价 |
| 每轮末尾自动邀请研究 | 只在识别到研究意图时**询问是否进入研究**，且必须用户明确确认 |
| 把审批 token／内部思考／循环次数／重试细节写进提示词 | 一律不写，只写权限事实 |

``CHAIN_OFFER_SYSTEM`` 及其「未明确拒绝就视为同意」的口径在本模块中**不存在**，
并有测试守卫（``tests/agent_v2/runtime/test_prompt_v2.py``）。
"""

from __future__ import annotations

from services.system_prompt import SCILOOP_SYSTEM_PROMPT

from .blocks import BlockKind, PromptBlock

BASE_INSTRUCTIONS = SCILOOP_SYSTEM_PROMPT


def build_base_instructions() -> PromptBlock:
    """base instructions 区块（恒定内容，便于单测断言）。"""
    return PromptBlock(
        kind=BlockKind.BASE_INSTRUCTIONS,
        title="工作方式",
        body=BASE_INSTRUCTIONS,
        facts={"style": "direct", "structure": "complexity_driven"},
    )


#: 明确停用的旧口径关键字：出现即视为回归（测试会守卫）。
RETIRED_PHRASES: tuple[str, ...] = (
    "CHAIN_OFFER_SYSTEM",
    "未明确拒绝就视为同意",
    "默认进入研究",
    "四段式",
    "必须使用表格",
    "必须给出 a/b/c",
)


__all__ = ["BASE_INSTRUCTIONS", "RETIRED_PHRASES", "build_base_instructions"]
