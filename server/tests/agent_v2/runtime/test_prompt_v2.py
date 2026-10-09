"""分层系统提示词 + 研究确认门验收（计划书 WP-03 / §7.2 提示词和正文）。

对应验收项：

- 新 runtime 不使用 CHAIN_OFFER_SYSTEM 和旧自动研究邀请；
- 普通问候直接回答；复杂任务才结构化；
- preamble、工具事件和最终正文分离（渲染层只提供 system prompt，正文由运行时落 Item）；
- 正文不含审批 token、内部思考、循环次数、重试细节、调试提示；
- 研究意图先确认，未确认没有检索副作用。
"""

from __future__ import annotations

import pytest
from _helpers import Harness

from contracts.agent_v2 import EventType, ItemType, TurnStatus
from services.agent_prompt_v2 import (
    BLOCK_ORDER,
    RETIRED_PHRASES,
    BlockKind,
    build_research_context,
    build_runtime_facts,
    build_task_context,
    prompt_block_order,
    render_prompt,
    scrub_facts,
    select_memories,
)
from services.agent_runtime_v2 import (
    FabricatedCitation,
    IllegalResearchTransition,
    ResearchEvidence,
    ResearchNode,
    ResearchPhase,
    detect_research_intent,
    is_affirmative,
    is_negative,
)


# ---------------------------------------------------------------------------- 区块与顺序
def test_block_order_is_fixed_and_renderer_honours_it():
    assert prompt_block_order() == BLOCK_ORDER
    assert BLOCK_ORDER[0] is BlockKind.BASE_INSTRUCTIONS
    assert BLOCK_ORDER[-1] is BlockKind.RESEARCH_CONTEXT

    rendered = render_prompt(
        model="m", workspace="/w", goal="g", research_confirmed=True, research_phase="planning"
    )
    positions = [rendered.text.index(f"## {title}") for title in ("工作方式", "运行环境", "任务上下文", "科研任务")]
    assert positions == sorted(positions), "区块必须按固定顺序出现"


def test_each_block_is_independently_testable():
    facts = build_runtime_facts(model="m1", workspace="/ws", tools=[{"name": "read_file", "kind": "read_only"}])
    assert "m1" in facts.body and "/ws" in facts.body
    assert "read_file" in facts.body and "只读" in facts.body

    task = build_task_context(goal="目标A", todos=["t1"], errors=["e1"])
    assert "目标A" in task.body and "t1" in task.body and "e1" in task.body
    assert task.facts["goal"] == "目标A"

    plain = build_research_context(research_confirmed=False)
    assert plain.present is False, "未确认研究时该区块必须为空"
    confirmed = build_research_context(research_confirmed=True, phase="planning", sources=["arXiv:2401.1"])
    assert confirmed.present is True
    assert "绝不编造" in confirmed.body
    assert "arXiv:2401.1" in confirmed.body


def test_research_block_only_appears_when_confirmed():
    before = render_prompt(goal="想做个文献调研")
    assert BlockKind.RESEARCH_CONTEXT not in before.kinds
    assert "科研任务" not in before.text

    after = render_prompt(goal="想做个文献调研", research_confirmed=True, research_phase="planning")
    assert BlockKind.RESEARCH_CONTEXT in after.kinds
    assert "绝不编造论文" in after.text


# ---------------------------------------------------------------------------- 清洗
def test_scrub_drops_internal_keys_and_ids():
    scrubbed = scrub_facts(
        {
            "owner_token": "abc",
            "loop_index": 3,
            "retry_count": 2,
            "debug": {"x": 1},
            "goal": "正常目标",
            "thread_ref": "th_001a11f9e146f9d7352ed39",
            "note": "含 traceback 的文本",
        }
    )
    assert "owner_token" not in scrubbed
    assert "loop_index" not in scrubbed and "retry_count" not in scrubbed and "debug" not in scrubbed
    assert scrubbed["goal"] == "正常目标"
    assert "thread_ref" not in scrubbed, "稳定 ID 不得进提示词"
    assert "note" not in scrubbed, "含 traceback 的值必须整条丢弃"


def test_prompt_never_contains_tokens_or_debug_state():
    rendered = render_prompt(
        model="m",
        permission_facts={"owner_token": "SECRET-TOKEN", "已授权范围": "工作区内读写"},
        goal="目标",
        extra_task_facts={"loop_index": 7, "retry_count": 3, "call_id": "call_001a11f9e147062d0e4f9ad"},
        now="2026-10-09T23:00:00.000Z",
    )
    assert "SECRET-TOKEN" not in rendered.text
    assert "loop_index" not in rendered.text and "retry_count" not in rendered.text
    assert "call_" not in rendered.text
    assert "已授权范围" in rendered.text
    assert "2026-10-09T23:00:00.000Z" in rendered.text


def test_retired_style_phrases_are_absent():
    rendered = render_prompt(goal="随便聊聊", model="m")
    for phrase in RETIRED_PHRASES:
        assert phrase not in rendered.text, f"旧口径不得出现：{phrase}"
    # 旧自动研究邀请的典型措辞也不应出现
    for banned in ("是否要我继续深入研究", "要不要我进入研究阶段", "默认进入研究"):
        assert banned not in rendered.text


def test_base_instructions_are_complexity_driven_not_fixed_template():
    text = render_prompt(goal="你好").text
    assert "按复杂度组织输出" in text
    assert "不要固定套用" in text
    assert "只在多维度对比" in text


# ---------------------------------------------------------------------------- 记忆过滤
def test_memories_are_filtered_by_scope_and_length(tmp_path):
    h = Harness.create(tmp_path)
    long_text = "很长的记忆" * 100
    h.memory.write("user", "u1", "用户偏好中文", origin="user")
    h.memory.write("project", "p1", long_text, origin="auto")

    records = h.memory.list_all()
    only_user = select_memories(records, scopes=["user"])
    assert only_user == ["用户偏好中文"]

    limited = select_memories(records, max_items=5, snippet_limit=20)
    assert any(text.endswith("…") for text in limited), "超长记忆必须被截断"

    rendered = render_prompt(memories=h.memory.conversation("none"), memory_scopes=["user"])
    assert "相关记忆" not in rendered.text or "用户偏好中文" not in rendered.text


def test_prompt_budget_truncates_details_not_base_instructions(tmp_path):
    rendered = render_prompt(
        goal="目标",
        todos=[f"待办{i}" for i in range(50)],
        blocked=[f"阻塞{i}" for i in range(50)],
        budget=900,
    )
    assert rendered.truncated is True
    assert "工作方式" in rendered.text, "base instructions 永不被砍"
    assert len(rendered.text) < 2000


# ---------------------------------------------------------------------------- 研究确认门
def test_research_intent_detection_and_answer_parsing():
    assert detect_research_intent("帮我做文献调研")
    assert detect_research_intent("系统梳理一下联邦学习的隐私风险")
    assert not detect_research_intent("今天天气怎么样")
    assert is_affirmative("好的") and is_affirmative("开始") and is_affirmative("yes")
    assert is_negative("不用") and is_negative("算了") and is_negative("no")
    # 沉默 / 含糊都不算同意
    assert not is_affirmative("")
    assert not is_affirmative("嗯……看情况吧")


def test_research_intent_asks_first_and_has_no_retrieval_side_effect(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("研究门")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "帮我做文献调研"}], idempotency_key="k")

    node.request_confirmation(turn.turn_id, question="联邦学习的隐私风险")

    state = h.repo.state(thread.thread_id)
    assert node.phase is ResearchPhase.CONFIRMATION_REQUIRED
    assert node.confirmed is False
    assert state.turns[turn.turn_id].status is TurnStatus.WAITING_INPUT
    types = h.event_types(thread.thread_id, turn_id=turn.turn_id)
    assert "input/requested" in types and "turn/waiting_input" in types
    # 未确认就没有任何检索副作用：没有工具调用、没有来源记录
    assert not state.tool_calls
    assert node.evidence() == []
    assert "tool/started" not in h.event_types(thread.thread_id)


def test_unanswered_confirmation_does_not_start_research(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("未回答")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    node.request_confirmation(turn.turn_id, question="Q")

    with pytest.raises(IllegalResearchTransition):
        node.advance(turn.turn_id, ResearchPhase.RETRIEVING)  # 未确认不得检索

    # 沉默之后状态仍是等待确认
    state = h.repo.state(thread.thread_id)
    assert state.turns[turn.turn_id].status is TurnStatus.WAITING_INPUT
    assert node.phase is ResearchPhase.CONFIRMATION_REQUIRED


def test_ambiguous_and_negative_answers_do_not_confirm(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("含糊回答")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    node.request_confirmation(turn.turn_id, question="Q")

    with pytest.raises(IllegalResearchTransition):
        node.confirm(turn.turn_id, answer="嗯……看情况吧")
    with pytest.raises(IllegalResearchTransition):
        node.confirm(turn.turn_id, answer="不用")
    assert node.confirmed is False

    # 明确否认 -> 回普通聊天
    node.deny(turn.turn_id, answer="不用")
    assert node.phase is ResearchPhase.IDLE and node.confirmed is False


def test_explicit_confirmation_enters_planning(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("确认")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    node.request_confirmation(turn.turn_id, question="Q")
    state = node.confirm(turn.turn_id, answer="好的")

    assert state["confirmed"] is True and state["phase"] == ResearchPhase.PLANNING.value
    # 确认后才允许检索
    node.advance(turn.turn_id, ResearchPhase.RETRIEVING)
    assert node.phase is ResearchPhase.RETRIEVING

    # 确认状态写进 Thread 设置，可跨重启读回
    from services.agent_threads_v2 import ThreadRepository

    fresh = ThreadRepository(h.root, clock=h.clock)
    assert ResearchNode(fresh, thread.thread_id, clock=h.clock).confirmed is True


def test_ui_confirmation_without_text_is_explicit_action(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("界面确认")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    node.request_confirmation(turn.turn_id, question="Q")
    assert node.confirm(turn.turn_id, by="user")["confirmed"] is True
    # 非用户来源且无明确回答 -> 拒绝
    h2 = Harness.create(tmp_path / "auto")
    t2 = h2.thread("自动确认")
    n2 = ResearchNode(h2.repo, t2.thread_id, clock=h2.clock)
    turn2 = h2.repo.start_turn(t2.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    n2.request_confirmation(turn2.turn_id, question="Q")
    with pytest.raises(IllegalResearchTransition):
        n2.confirm(turn2.turn_id, by="system")


# ---------------------------------------------------------------------------- 来源纪律
def test_evidence_requires_a_verifiable_identifier():
    with pytest.raises(FabricatedCitation):
        ResearchEvidence(source_id="   ")
    with pytest.raises(FabricatedCitation):
        ResearchEvidence(source_id="", url="")


def test_inaccessible_source_is_marked_uncertain_not_dropped(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("来源")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")

    node.record_evidence(
        turn.turn_id,
        ResearchEvidence(source_id="arXiv:2401.00001", url="https://arxiv.org/abs/2401.00001", title="A"),
    )
    node.record_evidence(
        turn.turn_id,
        ResearchEvidence(source_id="10.1000/xyz", url="", title="B", accessible=False),
    )
    evidence = node.evidence()
    assert len(evidence) == 2, "不可访问的来源要保留并标注，而不是丢掉"
    uncertain = [e for e in evidence if e["uncertain"]]
    assert len(uncertain) == 1 and uncertain[0]["source_id"] == "10.1000/xyz"


def test_conclusion_citations_must_reference_recorded_evidence(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("引用")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    node.record_evidence(turn.turn_id, ResearchEvidence(source_id="arXiv:2401.00001", url="u"))

    with pytest.raises(FabricatedCitation):
        node.conclusion(turn.turn_id, text="结论", citations={"[1]": "arXiv:9999.99999"})

    item = node.conclusion(turn.turn_id, text="结论", citations={"[1]": "arXiv:2401.00001"})
    assert item.payload["kind"] == "research_conclusion"
    assert item.type is ItemType.PLAN
    assert node.summaries()[0]["text"] == "结论"


def test_illegal_research_transitions_are_rejected(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("非法迁移")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "x"}], idempotency_key="k")
    with pytest.raises(IllegalResearchTransition):
        node.advance(turn.turn_id, ResearchPhase.SYNTHESIZING)  # idle -> synthesizing 不合法
    node.request_confirmation(turn.turn_id, question="Q")
    with pytest.raises(IllegalResearchTransition):
        node.confirm(turn.turn_id, answer="嗯")  # 含糊


def test_research_events_use_only_frozen_v1_names(tmp_path):
    """本轮刻意不新增事件名：研究门只用 v1 已有的 turn/waiting_input + input/requested。"""
    h = Harness.create(tmp_path)
    thread = h.thread("事件名")
    node = ResearchNode(h.repo, thread.thread_id, clock=h.clock)
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "做个研究"}], idempotency_key="k")
    node.request_confirmation(turn.turn_id, question="Q")
    types = set(h.event_types(thread.thread_id))
    assert "input/requested" in types
    assert "turn/waiting_input" in types
    assert not any(t.startswith("research/") for t in types), "不得自造契约外事件名"
    assert EventType.INPUT_REQUESTED.value in types
