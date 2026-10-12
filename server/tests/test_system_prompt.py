"""Guard the shared instructions and the runtime-specific output contract."""

import json
from pathlib import Path

from api.v1 import chat
from services.agent_prompt_v2 import BASE_INSTRUCTIONS, RETIRED_PHRASES
from services.output_style import OUTPUT_STYLE
from services.research import messages, phrasing, prompts
from services.research.contracts import NODE_OUTPUT_SCHEMAS
from services.system_prompt import SCILOOP_SYSTEM_PROMPT, load_system_prompt


def test_bundled_prompt_matches_reviewed_copy():
    reviewed = Path(__file__).resolve().parents[2] / "prompts/sciloop-system-prompt.zh.md"
    assert load_system_prompt() == reviewed.read_text(encoding="utf-8").strip()


def test_all_entry_points_use_the_shared_prompt():
    assert chat.REPLY_SYSTEM.strip() == (SCILOOP_SYSTEM_PROMPT + "\n\n" + OUTPUT_STYLE).strip()
    assert BASE_INSTRUCTIONS == SCILOOP_SYSTEM_PROMPT
    assert prompts.SYSTEM_PROMPT.startswith(SCILOOP_SYSTEM_PROMPT + "\n\n")
    assert phrasing.PHRASING_SYSTEM.startswith(SCILOOP_SYSTEM_PROMPT + "\n\n")
    assert messages.NODE_CONCLUSION_SYSTEM.startswith(SCILOOP_SYSTEM_PROMPT + "\n\n")
    assert chat.reply_system_prompt() == chat.REPLY_SYSTEM
    assert chat.reply_system_prompt({"plain_chat": True}).endswith(chat.PLAIN_CHAT_SYSTEM)


def test_retired_rules_are_not_injected():
    for text in (chat.reply_system_prompt(), prompts.SYSTEM_PROMPT, BASE_INSTRUCTIONS):
        for phrase in (*RETIRED_PHRASES, "默认现在开始", "不要试图写完整篇论文"):
            assert phrase not in text
    assert "不要试图写完整篇论文" not in prompts.NODE_GUIDES["paper_writing"]
    assert "1500" not in prompts.NODE_GUIDES["paper_writing"]


def test_research_confirmation_blocks_tools_during_planning():
    prompt = load_system_prompt()
    assert "先讨论选题" in prompt
    assert "不得调用查询论文库、学术搜索、网页搜索" in prompt
    assert "只有研究者明确说“开始文献调研”" in prompt


def test_research_confirmation_is_enforced_before_tool_discovery():
    # The prompt is guidance; this guard is the deterministic server boundary
    # that prevents a model/provider from searching before explicit consent.
    assert chat._research_gate_blocks_tools(
        "我想讨论一个选题，先不要检索", {}
    )
    assert chat._research_gate_blocks_tools(
        "我想用中位数比较", {"research_confirmation_pending": {"node": "literature_review"}}
    )
    assert not chat._research_gate_blocks_tools("现在可以搜索", {})


def test_writing_keeps_json_contract_and_full_manuscript_requirements(monkeypatch):
    monkeypatch.setattr(prompts, "_skills_block", lambda: "")
    monkeypatch.setattr(prompts, "_load_long_guide", lambda node: "")
    messages = prompts.build_messages(node="paper_writing", user_text="写完整论文")
    assert messages[0]["content"] == prompts.SYSTEM_PROMPT
    assert "只输出一个" in messages[0]["content"]
    assert "完整论文初稿" in messages[1]["content"]
    assert "LaTeX" in messages[1]["content"]
    assert "关键缺口未补齐不得 done" in messages[0]["content"]
    schema = json.dumps(NODE_OUTPUT_SCHEMAS["paper_writing"], ensure_ascii=False, indent=2)
    assert schema in messages[1]["content"]


def test_writing_supplement_does_not_automatically_advance():
    guide = Path(__file__).resolve().parents[2] / "prompts/06-paper-writing.md"
    text = guide.read_text(encoding="utf-8")
    assert "进入 `research_retrospective`" not in text
    assert "等待用户验收" in text
    assert "默认交付主 `.tex`" in text
