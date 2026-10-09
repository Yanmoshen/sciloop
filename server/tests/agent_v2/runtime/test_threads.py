"""Thread / Turn 验收（对应验收书 §3 线程和 Turn 验收）。

- 同一 Thread 同时只能有一个活动 Turn；
- 启动、追加、继续、排队、恢复、暂停、中断、完成和失败状态均有事件；
- 中断模型流时不会产生最终成功事件；
- 中断工具时进程由执行器接口收到取消信号；
- 服务重启后可从事件和快照恢复状态；
- 重复提交同一个输入幂等，不产生重复 Turn。
"""

from __future__ import annotations

import pytest
from _helpers import Harness, run

from contracts.agent_v2 import (
    CancelToken,
    ConcurrentTurnError,
    IllegalTurnTransition,
    ItemType,
    TurnStatus,
    text_response,
    tool_call_response,
)
from services.agent_threads_v2 import ThreadRepository


# ---------------------------------------------------------------------------- §3.1
def test_only_one_active_turn_per_thread(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    first = h.repo.start_turn(thread.thread_id, inputs=[{"text": "第一"}] , idempotency_key="k1")
    with pytest.raises(ConcurrentTurnError):
        h.repo.start_turn(thread.thread_id, inputs=[{"text": "第二"}], idempotency_key="k2")
    # 排队不占用活动名额
    queued = h.repo.queue_turn(thread.thread_id, inputs=[{"text": "排队"}], idempotency_key="k3")
    assert queued.status is TurnStatus.QUEUED
    with pytest.raises(ConcurrentTurnError):
        h.repo.promote_queued_turn(thread.thread_id)
    h.repo.complete_turn(thread.thread_id, first.turn_id)
    promoted = h.repo.promote_queued_turn(thread.thread_id)
    assert promoted is not None and promoted.status is TurnStatus.RUNNING
    h.repo.complete_turn(thread.thread_id, promoted.turn_id)
    h.assert_no_lease_leak()


# ---------------------------------------------------------------------------- §3.2
def test_every_lifecycle_state_emits_its_event(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    h.repo.queue_turn(thread.thread_id, inputs=[{"text": "排队"}], idempotency_key="q")

    t_run = h.repo.start_turn(thread.thread_id, inputs=[{"text": "跑"}], idempotency_key="r")
    h.repo.emit_event(thread.thread_id, "model/delta", payload={"text": "片段"}, turn_id=t_run.turn_id)
    h.repo.pause_turn(thread.thread_id, t_run.turn_id, reason="user_pause")
    h.repo.resume_turn(thread.thread_id, t_run.turn_id)
    h.repo.wait_for_input(thread.thread_id, t_run.turn_id, "请补充")
    h.repo.continue_turn(thread.thread_id, t_run.turn_id, "补充内容")
    approval = h.repo.wait_for_approval(
        thread.thread_id, t_run.turn_id, action={"tool": "run_command"}, risk="high"
    )
    h.repo.resolve_approval(thread.thread_id, t_run.turn_id, approval.approval_id, granted=True)
    h.repo.complete_turn(thread.thread_id, t_run.turn_id)

    t_fail = h.repo.start_turn(thread.thread_id, inputs=[{"text": "失败"}], idempotency_key="f")
    h.repo.fail_turn(thread.thread_id, t_fail.turn_id, error={"code": "boom", "message": "x"})

    t_int = h.repo.start_turn(thread.thread_id, inputs=[{"text": "中断"}], idempotency_key="i")
    h.repo.interrupt_turn(thread.thread_id, t_int.turn_id, reason="user_stop")

    types = set(h.event_types(thread.thread_id))
    for expected in (
        "turn/queued",
        "turn/started",
        "turn/paused",
        "turn/resumed",
        "turn/waiting_input",
        "turn/waiting_approval",
        "turn/completed",
        "turn/failed",
        "turn/interrupted",
        "item/added",
        "approval/requested",
        "approval/granted",
        "input/requested",
        "input/provided",
    ):
        assert expected in types, f"缺少事件 {expected}"
    h.assert_no_lease_leak()


def test_transitions_are_validated_and_illegal_ones_raise(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "x"}], idempotency_key="a")
    h.repo.complete_turn(thread.thread_id, turn.turn_id)

    with pytest.raises(IllegalTurnTransition):
        h.repo.resume_turn(thread.thread_id, turn.turn_id)  # completed -> running
    with pytest.raises(IllegalTurnTransition):
        h.repo.interrupt_turn(thread.thread_id, turn.turn_id)  # completed -> interrupted

    t2 = h.repo.start_turn(thread.thread_id, inputs=[{"text": "y"}], idempotency_key="b")
    h.repo.interrupt_turn(thread.thread_id, t2.turn_id)
    with pytest.raises(IllegalTurnTransition):
        h.repo.complete_turn(thread.thread_id, t2.turn_id)  # interrupted -> completed 被禁止
    h.assert_no_lease_leak()


# ---------------------------------------------------------------------------- §3.3
def test_interrupting_model_stream_produces_no_success_event(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    token = CancelToken()
    provider = h.provider([[text_response("很长" * 5)[0]] * 3])  # 多段，便于中途中断

    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "打断我"}], idempotency_key="k")
    runtime = h.runtime(provider)

    async def scenario():
        import asyncio

        task = asyncio.create_task(runtime.run(thread.thread_id, turn.turn_id, cancel=token))
        await asyncio.sleep(0)
        token.cancel("user_interrupt")
        return await task

    outcome = run(scenario())
    state = h.repo.state(thread.thread_id)
    assert outcome.status is TurnStatus.INTERRUPTED
    assert state.turns[turn.turn_id].status is TurnStatus.INTERRUPTED
    types = h.event_types(thread.thread_id, turn_id=turn.turn_id)
    assert "turn/completed" not in types
    assert "model/completed" not in types
    assert "turn/interrupted" in types
    h.assert_no_lease_leak()
    h.assert_sequence_is_contiguous(thread.thread_id)


def test_interrupting_tools_delivers_cancel_signal_to_executor(tmp_path):
    """中断工具时，执行器接口必须收到取消信号（而不是被硬杀）。

    为了让"中断发生在工具执行中"这件事**确定性发生**，这里用一个在执行期间
    主动发出取消信号的执行器（等价于研究者在命令跑的过程中点了停止），
    而不是靠 sleep 抢时序。
    """
    from contracts.agent_v2 import FakeToolExecutor, ToolCallStatus

    class CancellingExecutor(FakeToolExecutor):
        def __init__(self, token, **kw):
            super().__init__(**kw)
            self._token = token

        async def execute(self, call, cancel=None):
            self._token.cancel("stop_tool")  # 执行中收到停止指令
            return await super().execute(call, cancel)

    h = Harness.create(tmp_path)
    token = CancelToken()
    h.executor = CancellingExecutor(token)
    h.scheduler = type(h.scheduler)(h.executor, clock=h.clock)
    thread = h.thread()
    provider = h.provider([tool_call_response("write_file", {"p": "x"}, call_id="c1")])
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "写文件"}], idempotency_key="k")
    outcome = run(h.runtime(provider).run(thread.thread_id, turn.turn_id, cancel=token))

    assert outcome.status is TurnStatus.INTERRUPTED
    calls = list(h.repo.state(thread.thread_id).tool_calls.values())
    assert calls, "工具调用记录必须存在"
    assert calls[0].status is ToolCallStatus.CANCELLED, "执行器应收到取消信号并如实返回 cancelled"
    types = h.event_types(thread.thread_id, turn_id=turn.turn_id)
    assert "turn/completed" not in types, "中断不得产生成功事件"
    assert "tool/cancelled" in types
    assert h.repo.turn_lock_free(thread.thread_id)
    h.assert_no_lease_leak()


# ---------------------------------------------------------------------------- §3.4
def test_state_recovers_after_restart_from_events_and_snapshot(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("重启恢复")
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "你好"}], idempotency_key="k")
    runtime = h.runtime(h.provider(h.text_script("第一段回答")))
    run(runtime.run(thread.thread_id, turn.turn_id))

    # 只靠事件恢复
    fresh = ThreadRepository(h.root, clock=h.clock)
    state = fresh.state(thread.thread_id)
    assert state.thread.name == "重启恢复"
    assert state.turns[turn.turn_id].status is TurnStatus.COMPLETED
    assert state.assistant_text(turn.turn_id) == "第一段回答"
    assert len(state.items_of_type(ItemType.USER_INPUT)) == 1

    # 写快照后再恢复，状态必须一致
    seq = fresh.snapshot(thread.thread_id)
    assert seq == state.last_sequence
    recovered = ThreadRepository(h.root, clock=h.clock).state(thread.thread_id)
    assert recovered.turns[turn.turn_id].status is TurnStatus.COMPLETED
    assert recovered.assistant_text(turn.turn_id) == "第一段回答"

    # 快照之后新增事件，仍能被重放
    t2 = fresh.start_turn(thread.thread_id, inputs=[{"text": "第二轮"}], idempotency_key="k2")
    fresh.complete_turn(thread.thread_id, t2.turn_id)
    after = ThreadRepository(h.root, clock=h.clock).state(thread.thread_id)
    assert len(after.turns) == 2


def test_state_cache_file_is_not_the_source_of_truth(tmp_path):
    """删掉状态缓存后仍能从事件完整重建（事件才是唯一权威）。"""
    h = Harness.create(tmp_path)
    thread = h.thread("缓存非权威")
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "hi"}], idempotency_key="k")
    h.repo.complete_turn(thread.thread_id, turn.turn_id)
    store = h.store_for(thread.thread_id)
    cache = store.dir / "thread.json"
    assert cache.is_file()
    cache.unlink()
    rebuilt = ThreadRepository(h.root, clock=h.clock).state(thread.thread_id)
    assert rebuilt.turns[turn.turn_id].status is TurnStatus.COMPLETED
    assert rebuilt.thread.name == "缓存非权威"


# ---------------------------------------------------------------------------- §3.5
def test_duplicate_start_with_same_key_is_idempotent(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    first = h.repo.start_turn(thread.thread_id, inputs=[{"text": "同一次提交"}], idempotency_key="same")
    second = h.repo.start_turn(thread.thread_id, inputs=[{"text": "同一次提交"}], idempotency_key="same")
    assert second.turn_id == first.turn_id
    state = h.repo.state(thread.thread_id)
    assert len(state.turns) == 1, "重复提交不得产生第二个 Turn"
    assert len(state.items_of_type(ItemType.USER_INPUT)) == 1


def test_idempotent_input_append_does_not_duplicate_items(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "x"}], idempotency_key="t")
    a = h.repo.append_input(thread.thread_id, turn.turn_id, "同一条输入", idempotency_key="dup-input")
    b = h.repo.append_input(thread.thread_id, turn.turn_id, "同一条输入", idempotency_key="dup-input")
    assert a.item_id == b.item_id
    assert len(h.repo.state(thread.thread_id).items_of_type(ItemType.USER_INPUT)) == 2  # x + 1


# ---------------------------------------------------------------------------- 分叉
def test_fork_copies_history_with_independent_identity(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("原线程")
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "第一轮"}], idempotency_key="a")
    h.repo.complete_turn(thread.thread_id, turn.turn_id)
    t2 = h.repo.start_turn(thread.thread_id, inputs=[{"text": "第二轮"}], idempotency_key="b")
    h.repo.complete_turn(thread.thread_id, t2.turn_id)

    forked = h.repo.fork_thread(thread.thread_id, at_sequence=None, name="分叉线程")
    state = h.repo.state(forked.thread_id)
    assert forked.thread_id != thread.thread_id
    assert state.thread.name == "分叉线程"
    assert state.active_turn is None, "分叉不得继承活动 Turn"
    assert len(state.turns) == 2
    assert forked.forked_from["thread_id"] == thread.thread_id
    # 分叉线程自身的事件流可独立重放
    assert all(e.thread_id == forked.thread_id for e in h.store_for(forked.thread_id).read_all())
    h.assert_sequence_is_contiguous(forked.thread_id)


def test_fork_normalizes_active_turns(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("带活动 Turn")
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "跑着"}], idempotency_key="a")
    forked = h.repo.fork_thread(thread.thread_id, name="分叉后空闲")
    state = h.repo.state(forked.thread_id)
    assert state.active_turn is None
    assert forked.thread_id != turn.thread_id
    assert h.repo.turn_lock_free(forked.thread_id)


def test_thread_creation_does_not_touch_the_model(tmp_path):
    """验收书 §8：创建/恢复 Thread 不调用模型。"""
    h = Harness.create(tmp_path)
    provider = h.provider(h.text_script("不应被调用"))
    thread = h.thread("只创建")
    h.repo.state(thread.thread_id)
    ThreadRepository(h.root, clock=h.clock).state(thread.thread_id)
    assert provider.call_count == 0, "创建/恢复线程不得调用模型"
