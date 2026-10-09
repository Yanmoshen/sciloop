"""Agent Tree 验收（对应验收书 §6 Agent Tree 验收）。

- 子 Agent 有独立 Thread/Turn/Item；
- 父子消息具有来源和顺序；
- 父 Agent 可以等待多个子 Agent；
- 子 Agent 失败或中断不会自动终止父 Agent；
- 子 Agent 结果可以作为结构化 Item 回传。
"""

from __future__ import annotations

import pytest
from _helpers import Harness, run

from contracts.agent_v2 import (
    CancelToken,
    ErrorClass,
    ItemType,
    ThreadNotFound,
    TurnStatus,
    error_response,
    text_response,
)


def _run_child(h: Harness, child_id: str, text: str, script, *, key: str):
    turn = h.repo.start_turn(child_id, inputs=[{"text": text}], idempotency_key=key)
    provider = h.provider(script)
    runtime = h.runtime(provider)
    return run(runtime.run(child_id, turn.turn_id)), turn


# ---------------------------------------------------------------------------- §6.1
def test_child_agent_has_independent_thread_turn_and_items(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "子")

    assert child.thread_id != parent.thread_id
    assert child.parent_thread_id == parent.thread_id
    assert h.tree.path(child.thread_id) == [parent.thread_id, child.thread_id]

    outcome, turn = _run_child(h, child.thread_id, "子任务", h.text_script("子结果"), key="c1")
    assert outcome.status is TurnStatus.COMPLETED

    child_state = h.repo.state(child.thread_id)
    parent_state = h.repo.state(parent.thread_id)
    assert len(child_state.turns) == 1
    assert child_state.assistant_text(turn.turn_id) == "子结果"
    assert not parent_state.turns, "子 Agent 的 Turn 不得出现在父线程"
    assert all(i.thread_id == child.thread_id for i in child_state.items.values())
    # 父子关系在父线程可查
    assert h.tree.children(parent.thread_id) == [child.thread_id]


def test_nested_grandchild_keeps_full_path(tmp_path):
    h = Harness.create(tmp_path)
    root = h.thread("根")
    mid = h.tree.create_child(root.thread_id, "中间")
    leaf = h.tree.create_child(mid.thread_id, "叶子")
    assert h.tree.path(leaf.thread_id) == [root.thread_id, mid.thread_id, leaf.thread_id]
    assert h.tree.is_ancestor(root.thread_id, leaf.thread_id)
    assert h.tree.is_ancestor(mid.thread_id, leaf.thread_id)
    assert not h.tree.is_ancestor(leaf.thread_id, mid.thread_id)


# ---------------------------------------------------------------------------- §6.2
def test_mailbox_records_source_and_order(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "子")

    h.tree.send_message(parent.thread_id, child.thread_id, "第一条指令")
    h.tree.send_message(parent.thread_id, child.thread_id, "第二条指令")
    h.tree.reply(child.thread_id, "收到，开始执行")

    child_mail = h.tree.mailbox(child.thread_id)
    assert [m["content"] for m in child_mail] == ["第一条指令", "第二条指令"]
    assert [m["sequence"] for m in child_mail] == sorted(m["sequence"] for m in child_mail)
    assert all(m["from_thread_id"] == parent.thread_id for m in child_mail)

    parent_mail = h.tree.mailbox(parent.thread_id)
    assert [m["content"] for m in parent_mail] == ["收到，开始执行"]
    assert parent_mail[0]["from_thread_id"] == child.thread_id

    # 游标读取：只取新消息
    cursor = child_mail[-1]["sequence"]
    h.tree.send_message(parent.thread_id, child.thread_id, "第三条指令")
    assert [m["content"] for m in h.tree.mailbox(child.thread_id, after_sequence=cursor)] == ["第三条指令"]


def test_message_idempotency_key_prevents_duplicates(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "子")
    h.tree.send_message(parent.thread_id, child.thread_id, "只发一次", idempotency_key="m1")
    h.tree.send_message(parent.thread_id, child.thread_id, "只发一次", idempotency_key="m1")
    assert len(h.tree.mailbox(child.thread_id)) == 1


# ---------------------------------------------------------------------------- §6.3
def test_parent_waits_for_multiple_children(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    c1 = h.tree.create_child(parent.thread_id, "子一")
    c2 = h.tree.create_child(parent.thread_id, "子二")
    c3 = h.tree.create_child(parent.thread_id, "子三")

    _run_child(h, c1.thread_id, "任务一", h.text_script("结果一"), key="c1")
    _run_child(h, c2.thread_id, "任务二", [error_response(ErrorClass.FATAL, "子二崩了")], key="c2")
    h.tree.report_result(c1.thread_id, summary="结果一", status="completed")

    outcomes = run(
        h.tree.wait_for([c1.thread_id, c2.thread_id, c3.thread_id], timeout_s=2.0)
    )
    assert set(outcomes) == {c1.thread_id, c2.thread_id, c3.thread_id}
    assert outcomes[c1.thread_id].status == "completed"
    assert outcomes[c2.thread_id].status == "failed"
    # 一个子 Agent 失败不影响等待其它子 Agent（c3 甚至还没跑过，视为 idle→超时/空闲）
    assert outcomes[c1.thread_id].ok is True


def test_child_failure_does_not_terminate_the_parent(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "会失败的子")
    _run_child(h, child.thread_id, "必然失败", [error_response(ErrorClass.FATAL, "boom")], key="c")

    outcomes = run(h.tree.wait_for([child.thread_id], timeout_s=2.0))
    assert outcomes[child.thread_id].status == "failed"

    parent_state = h.repo.state(parent.thread_id)
    assert parent_state.thread.status == "active", "父线程必须保持活动"
    assert parent_state.active_turn is None

    # 父 Agent 仍可正常起自己的 Turn
    outcome, _, _ = h.play(parent.thread_id, "父继续干活", h.text_script("父照常工作"))
    assert outcome.status is TurnStatus.COMPLETED


def test_child_interrupt_does_not_terminate_the_parent(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "被中断的子")
    token = CancelToken()
    h.tree.register_cancel(child.thread_id, token)
    turn = h.repo.start_turn(child.thread_id, inputs=[{"text": "长任务"}], idempotency_key="c")
    runtime = h.runtime(h.provider([[text_response("慢慢来")[0]] * 3]))

    async def scenario():
        import asyncio

        task = asyncio.create_task(runtime.run(child.thread_id, turn.turn_id, cancel=token))
        await asyncio.sleep(0)
        h.tree.interrupt_child(child.thread_id, reason="parent_cancel")
        return await task

    outcome = run(scenario())
    assert outcome.status is TurnStatus.INTERRUPTED
    assert h.tree.child_status(child.thread_id) == "interrupted"
    # 父线程不受影响，且收到通知事件
    parent_state = h.repo.state(parent.thread_id)
    assert parent_state.thread.status == "active"
    parent_events = h.event_types(parent.thread_id)
    assert "agent/child_interrupted" in parent_events
    h.assert_no_lease_leak()


# ---------------------------------------------------------------------------- §6.4
def test_child_result_is_returned_as_structured_item(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "子")
    _run_child(h, child.thread_id, "统计", h.text_script("42 篇"), key="c")

    item = h.tree.report_result(
        child.thread_id,
        summary="统计完成：42 篇",
        status="completed",
        idempotency_key="res-1",
    )
    assert item.type is ItemType.SUBAGENT_RESULT
    assert item.subagent_thread_id == child.thread_id
    assert item.payload["summary"] == "统计完成：42 篇"
    assert item.payload["status"] == "completed"

    # 幂等：重复回传不产生第二个 Item
    again = h.tree.report_result(
        child.thread_id, summary="统计完成：42 篇", status="completed", idempotency_key="res-1"
    )
    assert again.item_id == item.item_id

    # 结构化结果可以进入父线程的模型上下文
    from services.agent_runtime_v2 import build_context

    context = build_context(h.repo.state(parent.thread_id))
    assert any("42 篇" in str(m.get("content", "")) for m in context)
    assert "agent/child_completed" in h.event_types(parent.thread_id)

    outcomes = run(h.tree.wait_for([child.thread_id], timeout_s=1.0))
    assert outcomes[child.thread_id].result_item_id == item.item_id
    assert outcomes[child.thread_id].summary == "统计完成：42 篇"


def test_child_without_parent_cannot_report(tmp_path):
    h = Harness.create(tmp_path)
    orphan = h.thread("孤儿子线程")
    with pytest.raises(ThreadNotFound):
        h.tree.report_result(orphan.thread_id, summary="无处可回")


def test_parent_tools_can_spawn_children_via_injected_executor(tmp_path):
    """父 Agent 通过工具入口产生子 Agent（工具实现属 Agent 2，这里只验证运行时可承载）。"""
    from contracts.agent_v2 import ToolKind, ToolSpec
    from contracts.agent_v2.models import ToolCall, ToolResult

    h = Harness.create(tmp_path)
    parent = h.thread("父")

    class SpawnExecutor:
        """最小示例执行器：spawn_agent 工具会真的建一个子线程。"""

        def __init__(self, tree, created: list[str]) -> None:
            self.tree = tree
            self.created = created

        def specs(self):
            return [ToolSpec(name="spawn_agent", kind=ToolKind.SIDE_EFFECT, description="派生子 Agent")]

        def spec(self, name):
            return self.specs()[0] if name == "spawn_agent" else None

        async def execute(self, call: ToolCall, cancel=None) -> ToolResult:
            from contracts.agent_v2.enums import ToolCallStatus

            child = self.tree.create_child(call.thread_id, str(call.arguments.get("name", "子")))
            self.created.append(child.thread_id)
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.SUCCEEDED,
                output={"child_thread_id": child.thread_id},
            )

    created: list[str] = []
    h.executor = SpawnExecutor(h.tree, created)
    from services.agent_runtime_v2 import ToolScheduler

    h.scheduler = ToolScheduler(h.executor, clock=h.clock)  # type: ignore[assignment]

    from contracts.agent_v2 import tool_call_response

    outcome, _, _ = h.play(
        parent.thread_id,
        "派生一个子 Agent",
        [tool_call_response("spawn_agent", {"name": "调研子"}, call_id="c1"), text_response("已派生")],
    )
    assert outcome.status is TurnStatus.COMPLETED
    assert len(created) == 1
    assert h.tree.children(parent.thread_id) == created
    assert h.tree.path(created[0]) == [parent.thread_id, created[0]]


def test_child_state_is_isolated_from_parent_events(tmp_path):
    h = Harness.create(tmp_path)
    parent = h.thread("父")
    child = h.tree.create_child(parent.thread_id, "子")
    _run_child(h, child.thread_id, "子任务", h.text_script("子输出"), key="c")

    child_events = h.store_for(child.thread_id).read_all()
    assert child_events, "子线程必须有独立事件流"
    assert all(e.thread_id == child.thread_id for e in child_events)
    assert not any(e.thread_id == parent.thread_id for e in child_events)
    h.assert_sequence_is_contiguous(child.thread_id)
    h.assert_sequence_is_contiguous(parent.thread_id)
