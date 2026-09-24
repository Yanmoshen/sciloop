# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""卡片口径：**一律中文 + 占位值「未提及」**（用户 2026-09-24 确认）。

两条最容易出大事的约束：

1. **占位值必须在"不可作为证据"的集合里** —— 否则「未提及」会被当成真引用去定位，
   等于让证据链造假。这是本文件的重点。
2. ``evidence_quote`` **不翻译** —— 它是逐字证据，翻译后无法与原文核对，定位必然失败。
"""

from __future__ import annotations

from services.parsing.card_builder import SYSTEM_PROMPT, UNKNOWN_PLACEHOLDER, unknown_fields
from services.parsing.locator import UNKNOWN_VALUES, is_usable_quote


def test_system_prompt_requires_chinese_but_not_for_quotes() -> None:
    for fragment in (
        "所有字段的值用中文",
        "保留英文标准名称",
        "逐字照抄原文",
        "不得翻译",
        "未提及",
    ):
        assert fragment in SYSTEM_PROMPT, fragment


def test_placeholder_is_chinese_and_registered_as_unusable_quote() -> None:
    """**关键回归**：占位值必须同时满足"中文可读"与"不可作为证据"。

    漏掉 ``locator.UNKNOWN_VALUES`` 里的同步，占位值就会被当作可用引用 ——
    这类错误在界面上看不出来，却会污染证据链。
    """

    assert UNKNOWN_PLACEHOLDER == "未提及"
    assert UNKNOWN_PLACEHOLDER in UNKNOWN_VALUES
    assert is_usable_quote(UNKNOWN_PLACEHOLDER) is False


def test_legacy_english_placeholder_still_unusable() -> None:
    """老卡片（改口径之前入库的）里的 ``unknown`` 仍然必须判为不可用。"""

    assert is_usable_quote("unknown") is False


def test_unknown_fields_counts_chinese_placeholder() -> None:
    """「未提及」要能驱动"证据不足"提示；否则前端会以为这些字段有内容。"""

    card = {
        "research_problem": "研究问题",
        "core_method": UNKNOWN_PLACEHOLDER,
        "experimental_setup": {"datasets": [UNKNOWN_PLACEHOLDER], "baselines": ["X"], "metrics": ["M"]},
    }
    flagged = unknown_fields(card)

    assert "core_method" in flagged
    assert "experimental_setup.datasets" in flagged
    assert "research_problem" not in flagged
