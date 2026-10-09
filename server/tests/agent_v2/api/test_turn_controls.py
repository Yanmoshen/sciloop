"""Thread / Turn 控件测试（验收书 §3）。

覆盖：创建、恢复、设置更新、启动 Turn、追加、继续、中断、恢复、归档（删除），
以及「同一活动 Turn 的重复控制请求不产生重复状态」「刷新页面后状态可重建」
「计划只作为事件显示，不出现简单/复杂模式开关」。
"""

from __future__ import annotations

from api.v2.agent_protocol import ErrorCode
from contracts.agent_v2.enums import TurnStatus


def test_thread_lifecycle_controls(api) -> None:
    created = api.start_thread("控件全流程", scenario="text_multi_turn", cwd="D:/work")
    thread_id = created["thread"]["thread_id"]
    assert created["thread"]["name"] == "控件全流程"
    assert created["thread"]["cwd"] == "D:/work"
    assert created["scenario"] == "text_multi_turn"

    listed = api.call("thread/list", {})
    assert [row["thread_id"] for row in listed["threads"]] == [thread_id]

    updated = api.call(
        "thread/settings/update",
        {"thread_id": thread_id, "model": "fake-2", "settings": {"theme": "dark"}},
        idem="settings-1",
    )
    assert updated["thread"]["model"] == "fake-2"
    assert updated["thread"]["settings"]["theme"] == "dark"
    # 场景设置在合并语义下必须保留（否则后续扩展会被静默清掉）
    assert updated["thread"]["settings"]["scenario"] == "text_multi_turn"

    archived = api.call("thread/delete", {"thread_id": thread_id}, idem="delete-1")
    assert archived["archived"] is True
    assert archived["thread"]["status"] == "archived"
    assert api.call("thread/list", {})["threads"] == []
    assert len(api.call("thread/list", {"include_archived": True})["threads"]) == 1
    assert api.find_events(thread_id, "thread/archived")


def test_settings_update_requires_something_to_change(api) -> None:
    thread = api.start_thread("空更新", scenario="text_multi_turn")["thread"]
    api.fails(
        "thread/settings/update",
        {"thread_id": thread["thread_id"]},
        expect=ErrorCode.INVALID_PARAMS.value,
        idem="settings-empty",
    )


def test_resume_rebuilds_full_state_after_refresh(api) -> None:
    """刷新页面后只靠 thread/resume 就能重建界面（不调用模型）。"""
    thread = api.start_thread("刷新重建", scenario="tools_parallel")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "跑一轮带工具的")
    snapshot = api.call("thread/resume", {"thread_id": thread_id})

    assert snapshot["thread"]["thread_id"] == thread_id
    assert snapshot["turns"], "必须回放 Turn"
    assert snapshot["items"], "必须回放 Item"
    assert snapshot["tool_calls"], "必须回放工具调用"
    assert snapshot["last_sequence"] == api.last_sequence(thread_id)
    item_types = {item["type"] for item in snapshot["items"]}
    assert {"user_input", "assistant_text", "tool_call", "tool_result"} <= item_types
    done = [turn for turn in snapshot["turns"] if turn["status"] == "completed"]
    assert done, "Turn 终态必须能在快照里重建"


def test_active_turn_state_rebuild_mid_flight(api) -> None:
    """活动 Turn 刷新后仍能重建（含 running 状态与已产出的增量）。"""
    thread = api.start_thread("中途刷新", scenario="interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="mid-1")
    turn_id = started["turn"]["turn_id"]
    api.wait_sequence(thread_id, 6)

    snapshot = api.call("thread/resume", {"thread_id": thread_id})
    assert snapshot["active_turn_id"] == turn_id
    active = [turn for turn in snapshot["turns"] if turn["turn_id"] == turn_id][0]
    assert active["status"] in {"running", "interrupted", "completed"}
    api.call(
        "turn/interrupt",
        {"thread_id": thread_id, "turn_id": turn_id},
        idem="mid-interrupt",
    )


def test_steer_appends_input_to_active_turn(api) -> None:
    thread = api.start_thread("追加输入", scenario="interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="steer-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"running"})

    result = api.call(
        "turn/steer",
        {"thread_id": thread_id, "turn_id": turn_id, "text": "补充：优先核对第三段"},
        idem="steer-1",
    )
    assert result["item"]["type"] == "user_input"
    assert result["item"]["payload"]["text"] == "补充：优先核对第三段"
    steer_events = api.find_events(thread_id, "input/provided")
    assert len(steer_events) == 1
    assert steer_events[0].turn_id == turn_id
    inputs = api.state(thread_id).items_for_turn(turn_id)
    assert [item.payload["text"] for item in inputs] == ["长回答", "补充：优先核对第三段"]

    api.call("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}, idem="steer-int")


def test_continue_after_waiting_input(api, service) -> None:
    """``turn/continue``：等待输入 -> 继续运行。

    进入 ``waiting_input`` 由工具层（Agent 2 的输入请求工具）负责：这里直接驱动领域
    接口构造该状态（不启动运行时任务），只验证 API 层的「继续」语义——包括它会把
    挂起的执行重新交给运行时。
    """
    thread = api.start_thread("等待输入", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    turn = service.repo.start_turn(thread_id, inputs=[{"text": "需要澄清"}])
    turn_id = turn.turn_id
    service.repo.wait_for_input(thread_id, turn_id, "请确认采样规模")
    assert api.state(thread_id).turn(turn_id).status is TurnStatus.WAITING_INPUT

    snapshot = api.call("thread/resume", {"thread_id": thread_id})
    waiting = [turn for turn in snapshot["turns"] if turn["turn_id"] == turn_id][0]
    assert waiting["waiting"]["kind"] == "input"
    assert waiting["waiting"]["prompt"] == "请确认采样规模"

    continued = api.call(
        "turn/continue",
        {"thread_id": thread_id, "turn_id": turn_id, "text": "采样规模 32"},
        idem="cont-1",
    )
    assert continued["turn"]["status"] in {"running", "completed"}
    api.wait_idle(thread_id)
    assert api.state(thread_id).turn(turn_id).status is TurnStatus.COMPLETED
    assert api.find_events(thread_id, "input/provided")[-1].turn_id == turn_id


def test_interrupt_produces_no_success_event(api) -> None:
    thread = api.start_thread("中断不成功", scenario="interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="int-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"running"})

    result = api.call(
        "turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id, "reason": "用户中断"},
        idem="int-1",
    )
    assert result["interrupted"] is True
    assert result["turn"]["status"] == "interrupted"
    types = api.event_types(thread_id)
    assert "turn/interrupted" in types
    assert "turn/completed" not in types, "中断后绝不能出现成功事件"
    assert api.state(thread_id).active_turn is None


def test_interrupt_is_idempotent(api) -> None:
    thread = api.start_thread("重复中断", scenario="interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="int2-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"running"})

    payload = {"thread_id": thread_id, "turn_id": turn_id}
    api.call("turn/interrupt", payload, idem="int2-1")
    payload2 = dict(payload)
    payload2["reason"] = "再次中断"

    second = api.call("turn/interrupt", payload, idem="int2-2")
    assert second["turn"]["status"] == "interrupted"
    interrupts = api.find_events(thread_id, "turn/interrupted")
    assert len(interrupts) == 1, "重复中断不得产生第二个中断事件"


def test_recover_interrupted_turn_resumes(api) -> None:
    thread = api.start_thread("恢复中断", scenario="interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="rec-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"running"})
    api.call("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}, idem="rec-int")

    recovered = api.call(
        "turn/recover", {"thread_id": thread_id, "turn_id": turn_id}, idem="rec-1"
    )
    assert recovered["mode"] == "resume"
    assert recovered["turn"]["turn_id"] == turn_id
    api.wait_idle(thread_id)
    assert api.state(thread_id).turn(turn_id).status is TurnStatus.COMPLETED


def test_recover_failed_turn_starts_a_new_turn(api) -> None:
    thread = api.start_thread("失败重试", scenario="error_fatal")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "会失败"}, idem="fail-t1")
    failed_id = started["turn"]["turn_id"]
    api.wait_idle(thread_id)
    assert api.state(thread_id).turn(failed_id).status is TurnStatus.FAILED

    recovered = api.call(
        "turn/recover", {"thread_id": thread_id, "turn_id": failed_id}, idem="fail-rec"
    )
    assert recovered["mode"] == "retry"
    assert recovered["retried_from"] == failed_id
    assert recovered["turn"]["turn_id"] != failed_id, "重试必须开新 Turn（失败态是终态）"
    api.wait_idle(thread_id)


def test_recover_rejects_completed_turn(api) -> None:
    thread = api.start_thread("完成不可恢复", scenario="text_multi_turn")["thread"]
    turn = api.run_turn(thread["thread_id"], "一轮")["turn"]
    api.fails(
        "turn/recover",
        {"thread_id": thread["thread_id"], "turn_id": turn["turn_id"]},
        expect=ErrorCode.ILLEGAL_TURN_TRANSITION.value,
        idem="rec-completed",
    )


def test_plan_is_an_item_without_mode_switch(api) -> None:
    """计划只是事件流里的一个 Item；协议里不存在简单/复杂模式开关。"""
    thread = api.start_thread("计划展示", scenario="plan")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "先给计划")

    plan_items = [item for item in api.state(thread_id).items.values() if item.type.value == "plan"]
    assert plan_items, "计划必须以 PLAN Item 出现在事件流里"
    steps = plan_items[-1].payload["steps"]
    assert [step["title"] for step in steps][:1] == ["梳理研究空白"]
    assert any(step["status"] == "completed" for step in steps)

    events = api.find_events(thread_id, "item/added")
    assert any(event.payload.get("item", {}).get("type") == "plan" for event in events)

    from api.v2.agent_protocol import method_names

    assert not [name for name in method_names() if "mode" in name], (
        "协议不得引入简单/复杂模式开关"
    )


def test_run_command_dispatch_keeps_ordering(api) -> None:
    """事件序号在线程内严格单调递增且不跳号（顺序性回归）。"""
    thread = api.start_thread("顺序性", scenario="tools_parallel")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "跑工具")
    sequences = api.sequences(thread_id)
    assert sequences == list(range(1, len(sequences) + 1))
