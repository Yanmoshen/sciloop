# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""会话标题的取值（三层兜底）单测。

要钉住的核心事实：**模型的思考过程绝不能变成会话标题**。
2026-09-22 实测 42 个会话里 17 个标题是"我们需要回答用户。用户要求为研究需求拟标…"
（全部来自同一个爱自言自语的模型）。
"""

from __future__ import annotations

from services import conversations


def test_picks_the_title_not_the_monologue() -> None:
    """最常见的那种回复：先自言自语几句，再给标题。"""

    content = (
        "我们需要回答用户。用户要求为研究需求拟一个标题。\n"
        "让我先看研究需求的内容。\n"
        "子词分词公平性评测\n"
    )
    assert conversations.pick_title_line(content) == "子词分词公平性评测"


def test_all_monologue_returns_empty_so_caller_can_fall_back() -> None:
    """整段都是思考过程 → 返回空串（由调用方用研究者输入兜底），不许硬塞独白。"""

    content = "我们需要回答用户。用户要求作为科研项目命名。让我想想怎么概括。首先看关键词。"
    assert conversations.pick_title_line(content) == ""


def test_strips_quotes_and_book_marks() -> None:
    assert conversations.pick_title_line("《长上下文检索评测》") == "长上下文检索评测"
    assert conversations.pick_title_line('"推荐系统可复现评测"') == "推荐系统可复现评测"


def test_prefers_a_title_like_line_over_a_long_sentence() -> None:
    """候选里有长句和短标题时，挑像标题的那个。"""

    content = "这是一个比较长的句子，写了很多东西，明显不像标题。\n图神经网络推荐评测\n"
    assert conversations.pick_title_line(content) == "图神经网络推荐评测"


def test_truncates_a_too_long_candidate() -> None:
    """像标题但太长 → 截断（不超过上限）。"""

    long_line = "这是一个非常长的标题" * 4
    picked = conversations.pick_title_line(long_line, max_chars=20)
    assert picked and len(picked) <= 20


def test_empty_content_is_empty() -> None:
    assert conversations.pick_title_line("") == ""
    assert conversations.pick_title_line("   \n  \n") == ""


def test_system_prompt_forbids_self_talk() -> None:
    """系统提示必须明确禁止自言自语 —— 这是第一层兜底，省不得。"""

    assert "不要输出任何思考过程" in conversations.TITLE_SYSTEM
    assert "我们需要回答用户" in conversations.TITLE_SYSTEM
    assert "20" in conversations.TITLE_SYSTEM
