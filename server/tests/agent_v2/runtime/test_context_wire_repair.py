"""Provider round-trip regressions from the real workbench journey."""
from _helpers import Harness, run

from contracts.agent_v2 import ItemType, StreamCompleted, ToolCallCompleted, TurnStatus
from contracts.agent_v2.models import ReasoningDelta, TextDelta
from services.agent_runtime_v2 import build_context


def test_multiple_calls_share_one_assistant_response_and_keep_real_reasoning(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    outcome, provider, _ = h.play(thread.thread_id, "读两份文件", [
        [ReasoningDelta("先读取这两份文件"), TextDelta("我来看看。"),
         ToolCallCompleted("a", "read_file", {"path": "a"}),
         ToolCallCompleted("b", "read_file", {"path": "b"}), StreamCompleted("tool_use")],
        [TextDelta("内容已整理。"), StreamCompleted("end_turn")],
    ])
    assert outcome.status is TurnStatus.COMPLETED
    messages = provider.calls[1].messages
    assistant = next(m for m in messages if m.get("tool_calls"))
    assert assistant["reasoning_content"] == "先读取这两份文件"
    assert assistant["content"] == "我来看看。"
    assert [c["id"] for c in assistant["tool_calls"]] == ["a", "b"]
    pos = messages.index(assistant)
    assert {m["tool_call_id"] for m in messages[pos + 1:pos + 3]} == {"a", "b"}


def test_collaboration_result_cannot_split_a_tool_batch(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.start(thread.thread_id, "整理说明")
    h.repo.add_item(thread.thread_id, ItemType.TOOL_CALL, {"name": "agent.spawn", "arguments": {}}, turn_id=turn.turn_id, call_id="spawn")
    h.repo.add_item(thread.thread_id, ItemType.SUBAGENT_RESULT, {"summary": "整理好了"})
    h.repo.add_item(thread.thread_id, ItemType.TOOL_RESULT, {"status": "succeeded", "output": {"ok": True}}, turn_id=turn.turn_id, call_id="spawn")
    messages = build_context(h.repo.state(thread.thread_id))
    pos = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
    assert messages[pos + 1]["tool_call_id"] == "spawn"
    assert "整理好了" in messages[pos + 2]["content"]


def test_interrupted_history_has_explicit_unknown_result_without_reexecution(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.start(thread.thread_id, "写文件")
    h.repo.add_item(thread.thread_id, ItemType.TOOL_CALL, {"name": "write_file", "arguments": {}}, turn_id=turn.turn_id, call_id="unanswered")
    h.repo.interrupt_turn(thread.thread_id, turn.turn_id)
    messages = build_context(h.repo.state(thread.thread_id))
    assert messages[-1]["role"] == "tool"
    assert "result_unavailable" in messages[-1]["content"]
    assert not h.executor.calls


def test_compacted_window_drops_orphan_tool_result_instead_of_relabeling_it(tmp_path):
    """Prompt normalization must remove outputs whose call is outside the window."""
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.start(thread.thread_id, "读取资料")
    # The call is covered by a compaction checkpoint while its late result is
    # still present in the tail. Codex drops this orphan during normalization.
    h.repo.add_item(
        thread.thread_id,
        ItemType.TOOL_CALL,
        {"name": "read_file", "arguments": {}, "response_id": "old"},
        turn_id=turn.turn_id,
        call_id="old-call",
    )
    compaction = h.compaction(h.provider(h.text_script("摘要")), token_budget=1)
    assert run(compaction.compact(thread.thread_id, trigger="manual")).ok
    # A late result after the checkpoint has no matching call in the active
    # model window and must be removed as an orphan.
    h.repo.add_item(
        thread.thread_id,
        ItemType.TOOL_RESULT,
        {"status": "succeeded", "output": "old result"},
        turn_id=turn.turn_id,
        call_id="old-call",
    )
    messages = build_context(h.repo.state(thread.thread_id))
    assert all("old result" not in str(message) for message in messages)


def test_active_child_receives_mailbox_correction(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread()
    child = h.tree.create_child(parent.thread_id, "摘要")
    h.start(child.thread_id, "写三行摘要")
    h.tree.send_message(parent.thread_id, child.thread_id, "改成两行，保留平台名称。")
    messages = build_context(h.repo.state(child.thread_id))
    assert messages[-1]["content"] == "[协作消息] 改成两行，保留平台名称。"


def test_resolved_approval_does_not_remain_pending_in_model_context(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.start(thread.thread_id, "请一位助手读说明")
    approval = h.repo.wait_for_approval(
        thread.thread_id, turn.turn_id, call_id="spawn", action={"tool": "agent.spawn"}, risk="low",
    )
    assert "等待研究者决定" in build_context(h.repo.state(thread.thread_id))[-1]["content"]
    h.repo.resolve_approval(thread.thread_id, turn.turn_id, approval.approval_id, granted=True)
    content = build_context(h.repo.state(thread.thread_id))[-1]["content"]
    assert "granted" in content
    assert "等待研究者决定" not in content
    assert "是否成功以工具结果为准" in content
