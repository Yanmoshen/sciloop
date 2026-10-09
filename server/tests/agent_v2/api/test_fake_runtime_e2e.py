"""假运行时端到端测试（验收书 §7）。

每个 fixture 场景都跑一遍完整链路：``turn/start`` -> 后台执行 -> 事件推送 -> 终态。
所有用例都**不依赖真实模型、真实网络或宿主机破坏性命令**（断言部分见本文件末尾）。
"""

from __future__ import annotations

import pytest

from services.agent_threads_v2 import ThreadRepository

SCENARIOS = [
    "text_multi_turn",
    "tools_parallel",
    "approval_flow",
    "interrupt",
    "compaction",
    "subagent",
    "plan",
    "resume_after_disconnect",
    "error_retry",
    "error_fatal",
]


def test_all_declared_fixtures_are_loadable(api, service) -> None:
    available = service.host.scenario_names()
    missing = [name for name in SCENARIOS if name not in available]
    assert not missing, f"缺少 fixture：{missing}"
    for name in SCENARIOS:
        scenario = service.host.load(name)
        assert scenario.name == name
        assert scenario.script, f"{name} 必须有至少一条 model_response"
        described = scenario.to_dict()
        assert described["model_calls"] == len(scenario.script)


def test_text_multi_turn_flow(api) -> None:
    thread = api.start_thread("纯文本多轮", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮问题")
    api.run_turn(thread_id, "第二轮问题")

    types = api.event_types(thread_id)
    assert types.count("turn/started") == 2
    assert types.count("turn/completed") == 2
    assert "model/delta" in types and "model/reasoning_delta" in types
    assert "model/usage" in types
    texts = [
        item.payload["text"]
        for item in api.state(thread_id).items.values()
        if item.type.value == "assistant_text"
    ]
    assert len(texts) == 2
    assert "第二轮" in texts[-1]
    assert api.state(thread_id).active_turn is None


def test_tools_parallel_and_serial_flow(api, service) -> None:
    thread = api.start_thread("多工具并行串行", scenario="tools_parallel")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "跑工具")

    runtime = service.host.runtime_for(thread_id)
    assert runtime.executor.max_concurrency >= 2, "只读工具必须真的并行执行"
    assert len(runtime.executor.calls_for("read_file")) == 1
    assert len(runtime.executor.calls_for("write_file")) == 1

    types = api.event_types(thread_id)
    assert "tool/started" in types
    assert "tool/completed" in types
    assert "tool/output" in types, "工具增量输出必须以事件形式出现"

    state = api.state(thread_id)
    calls = {call.name: call for call in state.tool_calls.values()}
    assert set(calls) == {"read_file", "list_dir", "write_file"}
    for call in calls.values():
        assert call.status.value == "succeeded"
        assert call.duration_ms is not None
        assert call.started_at and call.finished_at
    assert calls["write_file"].arguments["path"] == "notes/summary.md"


def test_approval_flow_fixture(api) -> None:
    thread = api.start_thread("审批等待与解决", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="e2e-ap-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    approval = next(iter(api.state(thread_id).approvals.values()))
    api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        idem="e2e-ap-1",
    )
    api.wait_idle(thread_id)

    types = api.event_types(thread_id)
    assert "approval/requested" in types
    assert "approval/granted" in types
    assert types.index("approval/requested") < types.index("turn/waiting_approval")
    assert types.index("approval/granted") < types.index("tool/started")
    assert "turn/completed" in types


def test_interrupt_fixture_stops_without_success(api) -> None:
    thread = api.start_thread("中断", scenario="interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="e2e-int-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_sequence(thread_id, 4)
    api.call("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}, idem="e2e-int-1")
    api.wait_idle(thread_id)

    types = api.event_types(thread_id)
    assert "turn/interrupted" in types
    assert "turn/completed" not in types
    assert api.state(thread_id).turn(turn_id).status.value == "interrupted"


def test_compaction_fixture_end_to_end(api) -> None:
    thread = api.start_thread("压缩", scenario="compaction")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮")
    api.run_turn(thread_id, "第二轮")
    result = api.call("thread/compact", {"thread_id": thread_id, "trigger": "manual"}, idem="e2e-cmp")[
        "result"
    ]
    assert result["ok"] is True
    assert result["tokens_before"] > result["tokens_after"]
    # 压缩后继续对话：上下文用摘要替换，仍能正常完成一轮
    api.run_turn(thread_id, "第三轮")
    assert api.state(thread_id).active_turn is None


def test_subagent_fixture_end_to_end(api) -> None:
    thread = api.start_thread("子 Agent", scenario="subagent")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "拆成三路")
    listed = api.call("agent/list", {"thread_id": thread_id})["children"]
    statuses = {child["name"]: child["status"] for child in listed}
    assert statuses == {"文献调研": "completed", "复现实验": "failed", "写作": "running"}
    assert len(api.state(thread_id).children) == 3


def test_plan_fixture_end_to_end(api) -> None:
    thread = api.start_thread("计划", scenario="plan")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "先给计划")

    plans = [item for item in api.state(thread_id).items.values() if item.type.value == "plan"]
    assert len(plans) == 2, "计划更新必须作为新的 PLAN Item 追加，历史不被覆盖"
    first, latest = plans
    assert first.item_id != latest.item_id
    assert [step["status"] for step in first.payload["steps"]] == [
        "completed",
        "in_progress",
        "pending",
    ]
    assert [step["status"] for step in latest.payload["steps"]] == [
        "completed",
        "completed",
        "in_progress",
    ]
    assert api.event_types(thread_id).count("item/added") >= 2
    # 计划不影响执行：一轮里两次计划更新之后仍然正常收尾
    assert api.state(thread_id).active_turn is None


def test_resume_after_disconnect_fixture(api) -> None:
    """断线后从游标补齐事件，且不重新调用模型。"""
    thread = api.start_thread("断线恢复", scenario="resume_after_disconnect")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮")
    cursor = api.last_sequence(thread_id)
    api.run_turn(thread_id, "第二轮")
    api.run_turn(thread_id, "第三轮")

    runtime = api.service.host.runtime_for(thread_id)
    calls_before = runtime.gateway.call_count

    # 模拟重连：用最后游标订阅，只应收到游标之后的事件
    api.clear_notifications()
    api.subscribe(thread_id, after_sequence=cursor)
    delivered = [note["sequence"] for note in api.events_notifications(thread_id)]
    assert delivered, "必须补齐断线期间的事件"
    assert min(delivered) == cursor + 1
    assert delivered == sorted(set(delivered)), "补齐事件不得重复或乱序"
    assert runtime.gateway.call_count == calls_before, "补拉不得触发模型调用"

    # 全量重放一次，确认没有丢事件
    replay = api.call("thread/events/replay", {"thread_id": thread_id, "after_sequence": 0})
    assert [event["sequence"] for event in replay["events"]] == list(
        range(1, replay["last_sequence"] + 1)
    )


def test_error_retry_fixture_recovers(api) -> None:
    thread = api.start_thread("可重试错误", scenario="error_retry")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "会先失败一次")

    types = api.event_types(thread_id)
    assert "model/failed" in types
    assert "model/retry_scheduled" in types, "可重试错误必须调度重试"
    assert "turn/completed" in types, "重试成功后 Turn 必须完成"
    assert "turn/failed" not in types
    texts = [
        item.payload["text"]
        for item in api.state(thread_id).items.values()
        if item.type.value == "assistant_text"
    ]
    assert texts and "重试成功" in texts[-1]


def test_error_fatal_fixture_fails_turn(api) -> None:
    thread = api.start_thread("不可恢复错误", scenario="error_fatal")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "会失败"}, idem="e2e-fatal")
    turn_id = started["turn"]["turn_id"]
    api.wait_idle(thread_id)

    state = api.state(thread_id)
    turn = state.turn(turn_id)
    assert turn.status.value == "failed"
    assert turn.error and turn.error["error_class"] == "fatal"
    types = api.event_types(thread_id)
    assert "turn/failed" in types
    assert "turn/completed" not in types
    errors = [item for item in state.items.values() if item.type.value == "error"]
    assert errors and "401" in errors[-1].payload["message"], "失败原因必须如实展示"


def test_runtime_is_offline_only(api, service) -> None:
    """断言运行时确实是假的：无真实网络、无宿主机命令执行。"""
    described = service.describe()
    assert described["host"] == "fake"
    thread = api.start_thread("离线确认", scenario="tools_parallel")["thread"]
    api.run_turn(thread["thread_id"], "跑工具")
    runtime = service.host.runtime_for(thread["thread_id"])
    assert type(runtime.gateway.provider).__name__ == "FakeProvider"
    assert type(runtime.executor).__name__ == "ScenarioToolExecutor"
    # 工具执行器只记录调用，从不真正触碰文件系统
    assert not hasattr(runtime.executor, "workdir")
    assert isinstance(service.repo, ThreadRepository)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_each_scenario_keeps_event_invariants(api, scenario: str) -> None:
    """所有场景共用的事件不变量：序号连续、每条都带 thread_id、终态唯一。"""
    thread = api.start_thread(f"不变量-{scenario}", scenario=scenario)["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "跑一轮"}, idem=f"inv-{scenario}")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(
        thread_id, turn_id, {"completed", "failed", "interrupted", "waiting_approval", "waiting_input"}
    )
    sequences = api.sequences(thread_id)
    assert sequences == list(range(1, len(sequences) + 1))
    for event in api.events(thread_id):
        assert event.thread_id == thread_id
        assert event.event_id.startswith("ev_")
    terminal = [
        event.type for event in api.events(thread_id) if event.type in {"turn/completed", "turn/failed"}
    ]
    assert len(terminal) <= 1, "一个 Turn 最多一个终态事件"
    if terminal:
        assert api.state(thread_id).active_turn is None
