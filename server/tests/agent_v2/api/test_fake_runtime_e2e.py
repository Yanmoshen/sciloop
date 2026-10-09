"""假运行时端到端测试（WP-07 fixture 清单 + §6 必须验证项）。

每个 fixture 都跑一遍完整链路：``turn/start`` -> 后台执行 -> 事件推送 -> 终态。
所有用例都**不依赖真实模型、真实网络或宿主机破坏性命令**（断言见文件末尾）。
"""

from __future__ import annotations

import pytest

from services.agent_threads_v2 import ThreadRepository

#: WP-07 规定的 12 个 fixture（名字与计划书一致）。
SCENARIOS = [
    "plain_chat_hello",
    "twenty_turns",
    "tool_parallel",
    "tool_serial",
    "approval_pending",
    "approval_resolved",
    "turn_interrupt",
    "compaction",
    "child_agents",
    "reconnect_gap",
    "tool_failure",
    "runtime_unavailable",
]

#: 本线补充场景（不在 WP-07 清单内，但各自覆盖一个独立路径）。
EXTRA_SCENARIOS = ["text_multi_turn", "plan", "error_retry", "error_fatal", "approval_flow"]

#: 能真正跑完一轮（或挂起等待）的场景；运行时不可用的场景单独测。
STARTABLE = [name for name in SCENARIOS if name != "runtime_unavailable"] + EXTRA_SCENARIOS


def test_all_declared_fixtures_are_loadable(api, service) -> None:
    available = service.host.scenario_names()
    missing = [name for name in SCENARIOS + EXTRA_SCENARIOS if name not in available]
    assert not missing, f"缺少 fixture：{missing}"
    for name in SCENARIOS + EXTRA_SCENARIOS:
        scenario = service.host.load(name)
        assert scenario.name == name
        assert scenario.script, f"{name} 必须有至少一条 model_response"
        described = scenario.to_dict()
        assert described["model_calls"] == len(scenario.script)


def test_plain_chat_hello_reaches_completed(api) -> None:
    """简单问候：收到助手正文与 turn/completed，不遗留 running（§7.3）。"""
    thread = api.start_thread("问候", scenario="plain_chat_hello")["thread"]
    thread_id = thread["thread_id"]
    result = api.call("turn/start", {"thread_id": thread_id, "text": "你好"}, idem="hello-1")
    turn_id = result["turn"]["turn_id"]
    api.wait_idle(thread_id)

    state = api.state(thread_id)
    assert state.turn(turn_id).status.value == "completed"
    assert state.active_turn is None
    texts = [
        item.payload["text"] for item in state.items.values() if item.type.value == "assistant_text"
    ]
    assert texts == ["你好！我在。想让我帮你处理什么？"]
    assert "model/completed" in api.event_types(thread_id)


def test_twenty_turns_keep_order_and_thread_switch_restores(api) -> None:
    """20 轮自然语言顺序正确；切到别的 Thread 再回来靠快照 + 游标恢复，不调模型。"""
    thread = api.start_thread("二十轮", scenario="twenty_turns")["thread"]
    thread_id = thread["thread_id"]
    for index in range(1, 21):
        api.start_turn(thread_id, f"第 {index} 轮问题")

    state = api.state(thread_id)
    texts = [item.payload["text"] for item in state.items.values() if item.type.value == "assistant_text"]
    assert len(texts) == 20
    assert texts == [f"第 {i} 轮：收到，我按顺序记下了这一轮的内容。" for i in range(1, 21)]
    assert state.active_turn is None

    # 切到另一个会话
    other = api.start_thread("另一个会话", scenario="plain_chat_hello")["thread"]
    api.start_turn(other["thread_id"], "你好")
    runtime = api.service.host.runtime_for(thread_id)
    calls_before = runtime.gateway.call_count

    # 切回来：先快照，再用最后游标订阅
    back = api.call("thread/resume", {"thread_id": thread_id})
    assert len(back["turns"]) == 20
    assert back["last_sequence"] == api.last_sequence(thread_id)
    restored = [item["payload"]["text"] for item in back["items"] if item["type"] == "assistant_text"]
    assert restored == texts, "快照必须完整重建正文"
    api.clear_notifications()
    api.subscribe(thread_id, after_sequence=back["last_sequence"])
    assert api.events_notifications(thread_id) == [], "没有新事件就不该补"
    assert runtime.gateway.call_count == calls_before, "切换/Tab 恢复绝不能再调用模型"


def test_tool_parallel_and_serial_flow(api, service) -> None:
    thread = api.start_thread("多工具并行串行", scenario="tool_parallel")["thread"]
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


def test_tool_serial_runs_one_at_a_time(api, service) -> None:
    """同时发起的两个副作用工具必须串行（不能并发改宿主机状态）。"""
    thread = api.start_thread("副作用串行", scenario="tool_serial")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "写文件并执行命令")

    runtime = service.host.runtime_for(thread_id)
    assert runtime.executor.max_concurrency == 1, "副作用工具不得并发执行"
    state = api.state(thread_id)
    calls = {call.name: call for call in state.tool_calls.values()}
    assert set(calls) == {"write_file", "run_command"}
    # 串行意味着先请求的先开始
    assert calls["write_file"].started_at <= calls["run_command"].started_at


def test_tool_failure_reports_real_reason(api) -> None:
    """工具失败必须如实显示失败原因，绝不伪造成功（§7.3）。"""
    thread = api.start_thread("工具失败", scenario="tool_failure")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "读取草稿")

    state = api.state(thread_id)
    call = next(iter(state.tool_calls.values()))
    assert call.status.value == "failed"
    assert call.error and call.error["code"] == "failed"
    assert "tool/failed" in api.event_types(thread_id)
    assert "tool/completed" not in api.event_types(thread_id)
    # 模型随后说明了真实原因
    texts = [item.payload["text"] for item in state.items.values() if item.type.value == "assistant_text"]
    assert texts[-1].startswith("读取失败")
    assert state.turn(state.turn_order[-1]).status.value == "completed"


def test_approval_pending_stops_at_the_card(api) -> None:
    thread = api.start_thread("等待审批", scenario="approval_pending")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="pend-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})

    state = api.state(thread_id)
    approval = next(iter(state.approvals.values()))
    assert approval.status.value == "pending"
    assert approval.action["tool"] == "run_command"
    assert "tool/completed" not in api.event_types(thread_id), "未审批不得先执行"
    assert "turn/completed" not in api.event_types(thread_id)
    assert state.active_turn is not None, "挂起的回合仍是活动回合"


def test_approval_resolved_supports_grant_and_deny(api) -> None:
    granted_thread = api.start_thread("批准后执行", scenario="approval_resolved")["thread"]
    granted_id = granted_thread["thread_id"]
    started = api.call("turn/start", {"thread_id": granted_id, "text": "执行命令"}, idem="res-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(granted_id, turn_id, {"waiting_approval"})
    approval = next(iter(api.state(granted_id).approvals.values()))
    api.call(
        "approval/resolve",
        {
            "thread_id": granted_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        idem="res-1",
    )
    api.wait_idle(granted_id)
    assert api.state(granted_id).turn(turn_id).status.value == "completed"
    assert "tool/completed" in api.event_types(granted_id)

    denied_thread = api.start_thread("拒绝后改道", scenario="approval_resolved")["thread"]
    denied_id = denied_thread["thread_id"]
    started = api.call("turn/start", {"thread_id": denied_id, "text": "执行命令"}, idem="res-t2")
    denied_turn = started["turn"]["turn_id"]
    api.wait_status(denied_id, denied_turn, {"waiting_approval"})
    approval = next(iter(api.state(denied_id).approvals.values()))
    api.call(
        "approval/resolve",
        {
            "thread_id": denied_id,
            "turn_id": denied_turn,
            "approval_id": approval.approval_id,
            "decision": "deny",
        },
        idem="res-2",
    )
    api.wait_idle(denied_id)
    state = api.state(denied_id)
    assert state.turn(denied_turn).status.value == "completed"
    call = next(iter(state.tool_calls.values()))
    assert call.status.value == "failed"
    assert call.error["code"] == "approval_denied"
    # 拒绝结果以「结构化错误」回喂模型：工具结果 Item 必须在上下文里（模型才有依据改道）。
    # 注意：假 provider 是固定脚本，无法按决定分支，所以这里断言**回喂的证据**而不是措辞。
    fed_back = [
        item
        for item in state.items.values()
        if item.type.value == "tool_result" and item.call_id == call.call_id
    ]
    assert fed_back and fed_back[-1].payload["error"]["code"] == "approval_denied"
    assert api.event_types(denied_id).count("model/requested") >= 2, "模型必须在拒绝后继续下一轮"


def test_runtime_unavailable_fails_cleanly(api) -> None:
    """运行时不可用：返回结构化错误，且不留下卡死的活动 Turn。"""
    thread = api.start_thread("运行时不可用", scenario="runtime_unavailable")["thread"]
    thread_id = thread["thread_id"]
    error = api.fails(
        "turn/start",
        {"thread_id": thread_id, "text": "开始"},
        expect="runtime_unavailable",
        idem="down-1",
    )
    assert error["data"]["method"] == "turn/start"

    state = api.state(thread_id)
    assert state.active_turn is None, "失败的回合必须收成终态，不能卡住线程"
    assert state.turn(state.turn_order[-1]).status.value == "failed"
    assert "turn/completed" not in api.event_types(thread_id)
    # 线程仍然可用（可以再发一次，依然得到同样的结构化错误）
    api.fails(
        "turn/start",
        {"thread_id": thread_id, "text": "再来"},
        expect="runtime_unavailable",
        idem="down-2",
    )


def test_interrupt_fixture_stops_without_success(api) -> None:
    thread = api.start_thread("中断", scenario="turn_interrupt")["thread"]
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


def test_child_agents_fixture_end_to_end(api) -> None:
    thread = api.start_thread("子 Agent", scenario="child_agents")["thread"]
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
    assert api.state(thread_id).active_turn is None


def test_reconnect_gap_fixture_backfills_by_cursor(api) -> None:
    """断线后从游标补齐事件，且不重新调用模型（§6）。"""
    thread = api.start_thread("断线恢复", scenario="reconnect_gap")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮")
    cursor = api.last_sequence(thread_id)
    api.run_turn(thread_id, "第二轮")
    api.run_turn(thread_id, "第三轮")

    runtime = api.service.host.runtime_for(thread_id)
    calls_before = runtime.gateway.call_count

    api.clear_notifications()
    api.subscribe(thread_id, after_sequence=cursor)
    delivered = [note["sequence"] for note in api.events_notifications(thread_id)]
    assert delivered, "必须补齐断线期间的事件"
    assert min(delivered) == cursor + 1
    assert delivered == sorted(set(delivered)), "补齐事件不得重复或乱序"
    assert runtime.gateway.call_count == calls_before, "补拉不得触发模型调用"

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
    thread = api.start_thread("离线确认", scenario="tool_parallel")["thread"]
    api.run_turn(thread["thread_id"], "跑工具")
    runtime = service.host.runtime_for(thread["thread_id"])
    assert type(runtime.gateway.provider).__name__ == "FakeProvider"
    assert type(runtime.executor).__name__ == "ScenarioToolExecutor"
    assert not hasattr(runtime.executor, "workdir")
    assert isinstance(service.repo, ThreadRepository)


@pytest.mark.parametrize("scenario", STARTABLE)
def test_each_scenario_keeps_event_invariants(api, scenario: str) -> None:
    """所有场景共用的事件不变量：序号连续、每条都带 thread_id、终态唯一。"""
    thread = api.start_thread(f"不变量-{scenario}", scenario=scenario)["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "跑一轮"}, idem=f"inv-{scenario}")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(
        thread_id,
        turn_id,
        {"completed", "failed", "interrupted", "waiting_approval", "waiting_input"},
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
