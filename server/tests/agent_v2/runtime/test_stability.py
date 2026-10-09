"""性能与稳定性验收（对应验收书 §8 性能和稳定性验收）。

- 创建/恢复 Thread 不调用模型；
- 长对话压缩后至少可以继续十轮 fake 多工具 Turn；
- 取消、超时和模型失败不会遗留活动 Turn 锁；
- 所有异步任务可取消，测试进程结束后无后台任务泄漏。
"""

from __future__ import annotations

import asyncio

import pytest
from _helpers import Harness, run

from contracts.agent_v2 import (
    CancelToken,
    ConcurrentTurnError,
    ErrorClass,
    StopReason,
    StreamCompleted,
    ToolCallStatus,
    TurnStatus,
    error_response,
    text_response,
    tool_call_response,
    usage_item,
)
from services.agent_threads_v2 import ThreadRepository


# ---------------------------------------------------------------------------- §8.1
def test_creating_and_restoring_threads_never_calls_the_model(tmp_path):
    h = Harness.create(tmp_path)
    provider = h.provider(h.text_script("不该出现"))
    thread = h.thread("纯创建")

    h.repo.state(thread.thread_id)          # 恢复状态
    ThreadRepository(h.root, clock=h.clock).state(thread.thread_id)  # 新实例恢复
    h.repo.snapshot(thread.thread_id)       # 写快照
    h.repo.fork_thread(thread.thread_id, name="分叉")                # 分叉

    assert provider.call_count == 0, "创建/恢复/快照/分叉都不得调用模型"


# ---------------------------------------------------------------------------- §8.2
def test_ten_multi_tool_turns_continue_after_compaction(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("压缩后继续")
    h.play(thread.thread_id, "热身问题", h.text_script("热身回答" * 30), key="warmup")

    svc = h.compaction(h.provider(h.text_script("热身阶段的摘要")), token_budget=10)
    assert run(svc.compact(thread.thread_id, trigger="manual")).ok

    rounds = 10
    for i in range(rounds):
        script = [
            [
                *tool_call_response("read_file", {"p": f"f{i}"}, call_id=f"r{i}")[:-1],
                *tool_call_response("list_dir", {"p": "."}, call_id=f"d{i}")[:-1],
                StreamCompleted(StopReason.TOOL_USE),
            ],
            [usage_item(5, 5), *text_response(f"第{i}轮完成")],
        ]
        outcome, _, turn = h.play(thread.thread_id, f"第{i}轮", script, key=f"round-{i}")
        assert outcome.status is TurnStatus.COMPLETED, f"第{i}轮应该完成"
        assert turn.status is TurnStatus.COMPLETED

    state = h.repo.state(thread.thread_id)
    assert len(state.turns) == rounds + 1
    assert all(t.status is TurnStatus.COMPLETED for t in state.turns.values())
    assert len(h.executor.calls) == rounds * 2
    h.assert_sequence_is_contiguous(thread.thread_id)
    h.assert_no_lease_leak()
    # 压缩摘要仍在，且原始历史一条未丢
    assert [s for s in svc.summaries(thread.thread_id) if s["active"]]


# ---------------------------------------------------------------------------- §8.3
def test_cancel_timeout_and_failure_leave_no_active_turn_lock(tmp_path):
    scenarios = {}

    # 取消
    h = Harness.create(tmp_path / "cancel")
    thread = h.thread("取消")
    token = CancelToken()
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "x"}], idempotency_key="c")
    provider = h.provider([[text_response("片段")[0]] * 3])
    runtime = h.runtime(provider)

    async def scenario_cancel(rt= runtime, tid=thread.thread_id, tu=turn.turn_id, tk=token):
        task = asyncio.create_task(rt.run(tid, tu, cancel=tk))
        await asyncio.sleep(0)
        tk.cancel("stop")
        return await task

    scenarios["cancel"] = (h, thread, turn, run(scenario_cancel()))

    # 超时
    h2 = Harness.create(tmp_path / "timeout")
    t2 = h2.thread("超时")
    turn2 = h2.repo.start_turn(t2.thread_id, inputs=[{"text": "x"}], idempotency_key="t")
    provider2 = h2.provider(h.text_script("慢回答"))
    provider2.delay_s = 0.05
    outcome2 = run(
        h2.runtime(provider2).run(t2.thread_id, turn2.turn_id, timeout_s=0.01)
    )
    scenarios["timeout"] = (h2, t2, turn2, outcome2)

    # 模型失败
    h3 = Harness.create(tmp_path / "fail")
    t3 = h3.thread("失败")
    turn3 = h3.repo.start_turn(t3.thread_id, inputs=[{"text": "x"}], idempotency_key="f")
    provider3 = h3.provider([error_response(ErrorClass.FATAL, "boom")])
    outcome3 = run(h3.runtime(provider3).run(t3.thread_id, turn3.turn_id))
    scenarios["failure"] = (h3, t3, turn3, outcome3)

    expected_status = {
        "cancel": TurnStatus.INTERRUPTED,
        "timeout": TurnStatus.INTERRUPTED,
        "failure": TurnStatus.FAILED,
    }
    for name, (harness, thread, turn, outcome) in scenarios.items():
        assert outcome.status is expected_status[name], f"{name} 的终态应为 {expected_status[name]}"
        state = harness.repo.state(thread.thread_id)
        assert state.turns[turn.turn_id].status is expected_status[name]
        assert state.thread.active_turn_id is None, f"{name} 后不得残留活动 Turn 指针"
        assert harness.repo.turn_lock_free(thread.thread_id), f"{name} 后租约必须已释放"
        harness.assert_no_lease_leak()
        # 终态之后可以立刻开新 Turn（锁没有卡住）
        nxt = harness.repo.start_turn(thread.thread_id, inputs=[{"text": "下一轮"}], idempotency_key=f"{name}-next")
        assert nxt.status is TurnStatus.RUNNING
        harness.repo.interrupt_turn(thread.thread_id, nxt.turn_id)
        harness.assert_no_lease_leak()


def test_lease_is_exclusive_across_repository_instances(tmp_path):
    """跨实例（等价于跨进程）也必须互斥。"""
    h = Harness.create(tmp_path)
    thread = h.thread("跨实例锁")
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "x"}], idempotency_key="k")
    other = ThreadRepository(h.root, clock=h.clock)
    with pytest.raises(ConcurrentTurnError):
        other.start_turn(thread.thread_id, inputs=[{"text": "抢锁"}], idempotency_key="k2")
    h.repo.complete_turn(thread.thread_id, turn.turn_id)
    # 释放后另一个实例可以接手
    nxt = other.start_turn(thread.thread_id, inputs=[{"text": "接手"}], idempotency_key="k3")
    assert nxt.status is TurnStatus.RUNNING
    other.complete_turn(thread.thread_id, nxt.turn_id)


# ---------------------------------------------------------------------------- §8.4
def test_no_dangling_asyncio_tasks_after_a_turn(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("任务泄漏")

    async def scenario():
        before = {t for t in asyncio.all_tasks() if not t.done()}
        turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "跑"}], idempotency_key="k")
        provider = h.provider(
            [
                [
                    *tool_call_response("read_file", {"p": "a"}, call_id="c1")[:-1],
                    StreamCompleted(StopReason.TOOL_USE),
                ],
                text_response("收工"),
            ]
        )
        outcome = await h.runtime(provider).run(thread.thread_id, turn.turn_id)
        await asyncio.sleep(0)  # 让所有回调/handle 落地
        after = {t for t in asyncio.all_tasks() if not t.done()}
        return outcome, after - before

    outcome, leaked = run(scenario())
    assert outcome.status is TurnStatus.COMPLETED
    assert not leaked, f"不得残留后台任务：{leaked}"
    h.assert_no_lease_leak()


def test_cancelled_turn_leaves_no_dangling_tasks(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("取消后无残留")

    async def scenario():
        token = CancelToken()
        turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "跑"}], idempotency_key="k")
        provider = h.provider(
            [
                [
                    *tool_call_response("write_file", {"p": "a"}, call_id="c1")[:-1],
                    StreamCompleted(StopReason.TOOL_USE),
                ]
            ]
        )
        h.executor.delay_s = 0.02
        task = asyncio.create_task(h.runtime(provider).run(thread.thread_id, turn.turn_id, cancel=token))
        await asyncio.sleep(0.005)
        token.cancel("stop")
        outcome = await task
        await asyncio.sleep(0)
        remaining = [
            t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()
        ]
        return outcome, remaining

    outcome, remaining = run(scenario())
    assert outcome.status is TurnStatus.INTERRUPTED
    assert not remaining, f"取消后不得残留任务：{remaining}"
    h.assert_no_lease_leak()


# ---------------------------------------------------------------------------- 综合回归
def test_long_session_keeps_sequence_and_state_consistent(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("长会话")
    for i in range(6):
        script = [
            [
                *tool_call_response("read_file", {"p": f"a{i}"}, call_id=f"c{i}")[:-1],
                StreamCompleted(StopReason.TOOL_USE),
            ],
            [*text_response(f"完成{i}"), usage_item(3, 2)],
        ]
        h.play(thread.thread_id, f"任务{i}", script, key=f"s{i}")

    h.assert_sequence_is_contiguous(thread.thread_id)
    state = h.repo.state(thread.thread_id)
    assert len(state.turns) == 6
    assert all(t.status is TurnStatus.COMPLETED for t in state.turns.values())
    assert all(tc.status is ToolCallStatus.SUCCEEDED for tc in state.tool_calls.values())
    # 事件数与状态数一致：item 条数 = 状态里的 item 条数
    item_events = len([e for e in h.store_for(thread.thread_id).read_all() if e.type == "item/added"])
    assert item_events == len(state.items)


def test_state_recovery_is_deterministic_across_instances(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("确定性")
    h.play(thread.thread_id, "一次对话", h.text_script("回答"), key="k")

    first = h.repo.state(thread.thread_id).to_dict()
    second = ThreadRepository(h.root, clock=h.clock).state(thread.thread_id).to_dict()
    assert first["turns"] == second["turns"]
    assert first["items"] == second["items"]
    assert first["thread"]["thread_id"] == second["thread"]["thread_id"]
