"""工具调度与工具 Item 验收（对应验收书 §5 的并行/串行与失败回喂条目）。

- 多个只读工具可以并行；
- 副作用工具不会并行执行（并发峰值必须为 1）；
- 同一个 call_id 不会被重复执行（重复副作用不会由运行时再次提交）；
- 工具失败、超时和参数错误会回喂模型。
"""

from __future__ import annotations

from _helpers import Harness, run

from contracts.agent_v2 import (
    ErrorClass,
    FakeToolExecutor,
    ItemType,
    StopReason,
    StreamCompleted,
    ToolCall,
    ToolCallStatus,
    ToolKind,
    ToolSpec,
    TurnStatus,
    error_response,
    text_response,
    tool_call_response,
)
from services.agent_runtime_v2 import ToolScheduler


def _two_calls(kind_name_a: str, kind_name_b: str) -> list[list]:
    """构造一轮「两个工具调用」的模型流。"""
    return [
        [
            *tool_call_response(kind_name_a, {"p": "1"}, call_id="c_a")[:-1],
            *tool_call_response(kind_name_b, {"p": "2"}, call_id="c_b")[:-1],
            StreamCompleted(StopReason.TOOL_USE),
        ],
        text_response("都完成了"),
    ]


# ---------------------------------------------------------------------------- 并行 / 串行
def test_read_only_tools_run_in_parallel(tmp_path):
    h = Harness.create(tmp_path)
    h.executor.delay_s = 0.01
    thread = h.thread()
    outcome, _, _ = h.play(
        thread.thread_id, "并行读", _two_calls("read_file", "list_dir")
    )
    assert outcome.status is TurnStatus.COMPLETED
    assert h.scheduler.report.max_concurrency_read_only >= 2, "只读工具必须并行"
    assert len(h.executor.calls) == 2


def test_side_effect_tools_never_run_in_parallel(tmp_path):
    h = Harness.create(tmp_path)
    h.executor.delay_s = 0.01
    thread = h.thread()
    outcome, _, _ = h.play(
        thread.thread_id, "串行写", _two_calls("write_file", "run_command")
    )
    assert outcome.status is TurnStatus.COMPLETED
    report = h.scheduler.report
    assert report.max_concurrency_side_effect == 1, "副作用工具必须串行"
    assert report.max_concurrency_read_only == 0


def test_mixed_batch_keeps_result_order(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    outcome, _, _ = h.play(thread.thread_id, "混合", _two_calls("read_file", "write_file"))
    assert outcome.status is TurnStatus.COMPLETED
    report = h.scheduler.report
    assert [r.call_id for r in report.results] == ["c_a", "c_b"], "结果顺序必须与请求一致"
    assert report.max_concurrency_read_only >= 1
    assert report.max_concurrency_side_effect == 1


# ---------------------------------------------------------------------------- 重复调用保护
def test_duplicate_call_id_is_executed_only_once(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    same = "call_dup"
    script = [
        tool_call_response("write_file", {"p": "x"}, call_id=same),
        tool_call_response("write_file", {"p": "x"}, call_id=same),  # 模型重复请求
        text_response("完成"),
    ]
    outcome, _, _ = h.play(thread.thread_id, "重复调用", script)
    assert outcome.status is TurnStatus.COMPLETED
    assert len(h.executor.calls) == 1, "重复 call_id 不得重复执行副作用"
    assert h.scheduler.report.duplicates, "重复必须被识别并记录"


def test_duplicate_call_returns_cached_result(tmp_path):
    h = Harness.create(tmp_path)
    calls = [
        ToolCall(
            call_id="c1",
            name="read_file",
            kind=ToolKind.READ_ONLY,
            status=ToolCallStatus.REQUESTED,
            thread_id="th_" + "0" * 23,
            turn_id="tu_" + "0" * 23,
        )
    ]
    scheduler = ToolScheduler(h.executor, clock=h.clock)
    cache = {}
    first = run(scheduler.execute_all(calls, cache=cache))
    second = run(scheduler.execute_all(calls, cache=cache))
    assert len(h.executor.calls) == 1
    assert second.duplicates == ["c1"]
    assert first.by_call_id()["c1"].status == second.by_call_id()["c1"].status


# ---------------------------------------------------------------------------- 失败回喂
def test_tool_failure_is_fed_back_to_the_model(tmp_path):
    h = Harness.create(tmp_path)
    h.executor.results["read_file"] = ("failed", {})
    thread = h.thread()
    script = [tool_call_response("read_file", {"p": "missing"}, call_id="c1"), text_response("我知道文件不存在了")]
    turn = h.start(thread.thread_id, "读缺失文件")
    provider = h.provider(script)
    outcome = run(h.runtime(provider).run(thread.thread_id, turn.turn_id))

    assert outcome.status is TurnStatus.COMPLETED, "工具失败不应中断循环"
    assert "tool/failed" in h.event_types(thread.thread_id, turn_id=turn.turn_id)
    tool_message = [m for m in provider.calls[1].messages if m.get("role") == "tool"][0]
    assert "failed" in tool_message["content"], "失败结果必须回喂模型"


def test_tool_timeout_is_reported_and_fed_back(tmp_path):
    h = Harness.create(tmp_path)
    h.executor.specs_list = [
        ToolSpec(name="slow_tool", kind=ToolKind.SIDE_EFFECT, timeout_s=0.01),
        ToolSpec(name="read_file", kind=ToolKind.READ_ONLY),
        ToolSpec(name="write_file", kind=ToolKind.SIDE_EFFECT),
    ]
    h.executor.delay_s = 0.05
    thread = h.thread()
    script = [tool_call_response("slow_tool", {}, call_id="c1"), text_response("超时就超时")]
    turn = h.start(thread.thread_id, "会超时的工具")
    provider = h.provider(script)
    runtime = h.runtime(provider)
    outcome = run(runtime.run(thread.thread_id, turn.turn_id))

    assert outcome.status is TurnStatus.COMPLETED
    state = h.repo.state(thread.thread_id)
    assert state.tool_calls["c1"].status is ToolCallStatus.TIMEOUT
    assert "tool/timeout" in h.event_types(thread.thread_id, turn_id=turn.turn_id)
    tool_message = [m for m in provider.calls[1].messages if m.get("role") == "tool"][0]
    assert "timeout" in tool_message["content"]


def test_unknown_tool_is_invalid_arguments_and_never_executed(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    script = [tool_call_response("not_registered", {}, call_id="c1"), text_response("换一个工具")]
    turn = h.start(thread.thread_id, "调用不存在的工具")
    provider = h.provider(script)
    outcome = run(h.runtime(provider).run(thread.thread_id, turn.turn_id))

    assert outcome.status is TurnStatus.COMPLETED
    assert h.executor.calls == [], "未知工具不得被交给执行器"
    state = h.repo.state(thread.thread_id)
    assert state.tool_calls["c1"].status is ToolCallStatus.INVALID_ARGUMENTS
    assert "tool/invalid_arguments" in h.event_types(thread.thread_id, turn_id=turn.turn_id)
    tool_message = [m for m in provider.calls[1].messages if m.get("role") == "tool"][0]
    assert "unknown_tool" in tool_message["content"]


def test_executor_crash_becomes_failed_not_a_broken_loop(tmp_path):
    h = Harness.create(tmp_path)
    h.executor.fail_names = {"write_file"}
    thread = h.thread()
    script = [tool_call_response("write_file", {"p": "x"}, call_id="c1"), text_response("执行器崩了但我还活着")]
    turn = h.start(thread.thread_id, "执行器崩溃")
    provider = h.provider(script)
    outcome = run(h.runtime(provider).run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.COMPLETED
    state = h.repo.state(thread.thread_id)
    assert state.tool_calls["c1"].status is ToolCallStatus.FAILED
    assert state.tool_calls["c1"].error["code"] == "executor_error"


def test_tool_items_are_recorded_in_the_transcript(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    outcome, _, turn = h.play(
        thread.thread_id, "记录工具", [tool_call_response("read_file", {"p": "a"}, call_id="c1"), text_response("好")]
    )
    state = h.repo.state(thread.thread_id)
    call_items = [i for i in state.items_for_turn(turn.turn_id) if i.type is ItemType.TOOL_CALL]
    result_items = [i for i in state.items_for_turn(turn.turn_id) if i.type is ItemType.TOOL_RESULT]
    assert call_items and call_items[0].call_id == "c1"
    assert result_items and result_items[0].call_id == "c1"
    assert outcome.status is TurnStatus.COMPLETED


def test_scheduler_falls_back_to_side_effect_for_unknown_tools(tmp_path):
    """未知工具的副作用等级必须保守判定为 side_effect（宁可串行也别猜可并行）。"""
    h = Harness.create(tmp_path)
    scheduler = ToolScheduler(FakeToolExecutor(specs_list=[ToolSpec(name="x", kind=ToolKind.READ_ONLY)]), clock=h.clock)
    assert scheduler.kind_of("x") is ToolKind.READ_ONLY
    assert scheduler.kind_of("mystery") is ToolKind.SIDE_EFFECT


def test_retryable_model_error_after_tool_result_still_fails_cleanly(tmp_path):
    """工具已执行后模型再报致命错误：Turn 失败，但工具记录与租约状态必须干净。"""
    h = Harness.create(tmp_path)
    thread = h.thread()
    script = [tool_call_response("read_file", {"p": "a"}, call_id="c1"), error_response(ErrorClass.FATAL, "boom")]
    turn = h.start(thread.thread_id, "工具后失败")
    provider = h.provider(script)
    outcome = run(h.runtime(provider).run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.FAILED
    state = h.repo.state(thread.thread_id)
    assert state.tool_calls["c1"].status is ToolCallStatus.SUCCEEDED
    h.assert_no_lease_leak()
