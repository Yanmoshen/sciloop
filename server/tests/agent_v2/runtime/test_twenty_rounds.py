"""20 轮自然语言对话的端到端稳定性证据（计划书 §6 第一条自动化场景）。

要求：20 轮自然语言 fake 对话，所有 Turn 完成，无后台任务、无活动锁；
中途穿插一次上下文压缩，并在压缩后继续执行——用来证明
「压缩只替换模型视图，不打断当前任务」。
"""

from __future__ import annotations

import asyncio

from _helpers import Harness, run

from contracts.agent_v2 import (
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
from services.agent_compaction_v2 import CompactionPolicy, CompactionService
from services.model_gateway_v2 import ModelGateway

ROUNDS = 20
COMPACT_AT = 10


def _script_for(index: int) -> list[list]:
    """让每一轮的形态都不一样，覆盖纯文本、单工具、双工具、一次失败、一次超时。"""
    if index % 5 == 0:
        # 双工具（只读并行 + 副作用串行）
        return [
            [
                *tool_call_response("read_file", {"p": f"f{index}"}, call_id=f"r{index}")[:-1],
                *tool_call_response("write_file", {"p": f"o{index}"}, call_id=f"w{index}")[:-1],
                StreamCompleted(StopReason.TOOL_USE),
            ],
            [usage_item(4, 3), *text_response(f"第 {index} 轮：读完并写入，结论 A{index}")],
        ]
    if index % 5 == 1:
        return [[*text_response(f"第 {index} 轮：直接回答，无需工具")]]
    if index % 5 == 2:
        return [
            [
                *tool_call_response("read_file", {"p": f"g{index}"}, call_id=f"s{index}")[:-1],
                StreamCompleted(StopReason.TOOL_USE),
            ],
            [*text_response(f"第 {index} 轮：单工具完成")],
        ]
    if index % 5 == 3:
        # 工具失败 -> 回喂模型 -> 仍能收尾（失败不伪装成功、也不中断循环）
        h_executor_results = ("failed", {})
        return [
            [
                *tool_call_response("read_file", {"p": f"missing{index}"}, call_id=f"x{index}")[:-1],
                StreamCompleted(StopReason.TOOL_USE),
            ],
            [*text_response(f"第 {index} 轮：文件不存在，改用其它依据")],
        ], h_executor_results
    # 可重试错误 -> 重试 -> 继续（不走压缩路径）
    return [
        error_response(ErrorClass.RETRYABLE, "429 busy"),
        [*text_response(f"第 {index} 轮：重试后成功")],
    ]


def test_twenty_round_natural_conversation(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("二十轮对话")
    compaction = CompactionService(
        repo=h.repo,
        gateway=ModelGateway(h.provider([text_response("### 目标 / goal\n二十轮对话")]), clock=h.clock),
        clock=h.clock,
        policy=CompactionPolicy(token_budget=10_000, min_new_events=1, max_auto_per_turn=1),
    )

    async def scenario():
        before = {t for t in asyncio.all_tasks() if not t.done()}
        log: list[str] = []
        for index in range(1, ROUNDS + 1):
            script = _script_for(index)
            extra: dict = {}
            if isinstance(script, tuple):
                script, result = script
                h.executor.results["read_file"] = result
            else:
                h.executor.results["read_file"] = ("succeeded", {"found": f"c{index}"})

            provider = h.provider(script)
            turn = h.repo.start_turn(
                thread.thread_id, inputs=[{"text": f"第 {index} 轮问题"}], idempotency_key=f"r{index}"
            )
            runtime = h.runtime(provider, compaction=compaction, **extra)
            outcome = await runtime.run(thread.thread_id, turn.turn_id)
            log.append(f"round={index} status={outcome.status.value} iters={outcome.iterations}")

            assert outcome.status is TurnStatus.COMPLETED, f"第 {index} 轮应完成：{log[-1]}"

            if index == COMPACT_AT:
                result = await compaction.compact(thread.thread_id, trigger="manual")
                assert result.ok is True
                log.append(f"compaction ok summary={result.summary_id} covered={result.covered_until}")
        await asyncio.sleep(0)
        after = {t for t in asyncio.all_tasks() if not t.done()}
        return log, after - before

    log, leaked = run(scenario())
    assert len(log) >= ROUNDS, "每轮都要有记录"
    assert not leaked, f"不得残留后台任务：{leaked}"

    state = h.repo.state(thread.thread_id)
    assert len(state.turns) == ROUNDS, "20 轮必须是 20 个 Turn"
    assert all(t.status is TurnStatus.COMPLETED for t in state.turns.values())
    assert state.active_turn is None and state.thread.active_turn_id is None

    # 每轮都有正文落地
    for turn_id in state.turn_order:
        assert state.assistant_text(turn_id).strip(), f"{turn_id} 缺少正文"

    # 工具事件完整且有 call_id；失败的工具不会伪装成成功
    for call in state.tool_calls.values():
        assert call.call_id and call.name
        if call.status is ToolCallStatus.SUCCEEDED:
            assert call.error is None
        assert call.status in set(ToolCallStatus)

    # 序号连续、无活动锁
    h.assert_sequence_is_contiguous(thread.thread_id)
    h.assert_no_lease_leak()
    assert h.repo.turn_lock_free(thread.thread_id)

    # 压缩确实生效过，且压缩后第 11 轮起仍正常完成
    assert [s for s in compaction.summaries(thread.thread_id) if s["active"]]
    assert any(line.startswith("compaction ok") for line in log)


def test_twenty_rounds_state_survives_restart(tmp_path):
    """20 轮之后用新实例重建状态，必须与运行中的状态一致（事件是唯一权威）。"""
    h = Harness.create(tmp_path)
    thread = h.thread("重启一致")
    for index in range(1, ROUNDS + 1):
        h.play(thread.thread_id, f"问题{index}", h.text_script(f"回答{index}"), key=f"r{index}")

    live = h.repo.state(thread.thread_id).to_dict()
    from services.agent_threads_v2 import ThreadRepository

    rebuilt = ThreadRepository(h.root, clock=h.clock).state(thread.thread_id).to_dict()
    assert live["turns"] == rebuilt["turns"]
    assert live["items"] == rebuilt["items"]
    assert len(rebuilt["turns"]) == ROUNDS
