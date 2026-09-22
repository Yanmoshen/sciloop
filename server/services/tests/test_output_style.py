# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""输出风格规范：**接线必须成立，且不能溢到 JSON 契约那边去**。

要钉住两件事：
1. 规范里那几条硬要求（三级标题 / 表格列数 / 不用 emoji / 先给结论）真的还在 ——
   它是一段字符串，最容易被顺手改掉而没人发现；
2. `REPLY_SYSTEM` **确实带上了**它（真实事故模式：改了常量却忘了接线）；
3. 研究节点的 `SYSTEM_PROMPT` **绝不能**被拼上它 —— 那个节点只输出 JSON，
   加 Markdown 排版规范会直接与解析契约冲突。
"""

from __future__ import annotations

from api.v1 import chat as chat_api
from services.output_style import OUTPUT_SECTION_ORDER, OUTPUT_STYLE
from services.research import prompts as research_prompts


def test_style_pins_the_hard_requirements() -> None:
    for fragment in ("###", "表格", "3–4 列", "不用 emoji", "先给结论", "加粗"):
        assert fragment in OUTPUT_STYLE, fragment


def test_style_carries_the_four_section_order() -> None:
    assert OUTPUT_SECTION_ORDER == ("直接结论", "详细拆解", "对比与选型", "落地建议")
    for section in OUTPUT_SECTION_ORDER:
        assert section in OUTPUT_STYLE, section


def test_reply_system_actually_includes_the_style() -> None:
    """接线用例：常量改了但没人拼进去，是这类"规范"最常见的失效方式。"""

    assert OUTPUT_STYLE in chat_api.REPLY_SYSTEM


def test_research_node_prompt_stays_json_only() -> None:
    """负面用例：研究节点只输出 JSON，排版规范不能溢过去。"""

    assert OUTPUT_STYLE not in research_prompts.SYSTEM_PROMPT
    assert "只输出一个 JSON 对象" in research_prompts.SYSTEM_PROMPT
