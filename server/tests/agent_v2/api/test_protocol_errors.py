"""结构化错误测试（验收书 §2「非法 ID、非法状态和过期游标返回结构化错误」）。

每条用例都断言 **错误码**（而不是错误文案），因为客户端要按 code 分支。
"""

from __future__ import annotations

import json

from api.v2.agent_protocol import PROTOCOL_VERSION, ErrorCode
from contracts.agent_v2.ids import new_id


def test_unknown_method_is_structured(api) -> None:
    error = api.fails("thread/nope", {}, expect=ErrorCode.UNKNOWN_METHOD.value)
    assert error["data"]["method"] == "thread/nope"


def test_missing_required_param(api) -> None:
    error = api.fails("thread/start", {}, expect=ErrorCode.INVALID_PARAMS.value)
    assert "name" in error["data"]["missing"]


def test_unknown_param_is_rejected(api) -> None:
    error = api.fails(
        "thread/list", {"limit": 1, "not_a_param": True}, expect=ErrorCode.INVALID_PARAMS.value
    )
    assert error["data"]["unknown"] == ["not_a_param"]


def test_mutating_method_requires_idempotency_key(api) -> None:
    error = api.fails(
        "thread/start", {"name": "缺少幂等键"}, expect=ErrorCode.INVALID_PARAMS.value
    )
    assert "idempotency_key" in error["message"]


def test_invalid_thread_id(api) -> None:
    error = api.fails(
        "thread/resume", {"thread_id": "th_not-hex"}, expect=ErrorCode.INVALID_ID.value
    )
    assert error["data"]["kind"] == "thread"
    assert error["data"]["value"] == "th_not-hex"


def test_invalid_turn_id(api) -> None:
    thread = api.start_thread("非法 Turn ID", scenario="text_multi_turn")["thread"]
    error = api.fails(
        "turn/interrupt",
        {"thread_id": thread["thread_id"], "turn_id": "tu_bad"},
        expect=ErrorCode.INVALID_ID.value,
        idem="interrupt-bad",
    )
    assert error["data"]["kind"] == "turn"


def test_invalid_call_id_on_replay(api) -> None:
    thread = api.start_thread("非法 Call ID", scenario="text_multi_turn")["thread"]
    error = api.fails(
        "thread/events/replay",
        {"thread_id": thread["thread_id"], "after_sequence": 0, "call_id": "call_bad"},
        expect=ErrorCode.INVALID_ID.value,
    )
    assert error["data"]["kind"] == "call"


def test_unknown_thread_is_thread_not_found(api) -> None:
    missing = new_id("thread")
    error = api.fails(
        "thread/resume", {"thread_id": missing}, expect=ErrorCode.THREAD_NOT_FOUND.value
    )
    assert missing in error["message"]


def test_unknown_turn_is_turn_not_found(api) -> None:
    thread = api.start_thread("Turn 不存在", scenario="text_multi_turn")["thread"]
    error = api.fails(
        "turn/interrupt",
        {"thread_id": thread["thread_id"], "turn_id": new_id("turn")},
        expect=ErrorCode.TURN_NOT_FOUND.value,
        idem="interrupt-missing-turn",
    )
    assert error["code"] == ErrorCode.TURN_NOT_FOUND.value


def test_illegal_state_transition_is_structured(api) -> None:
    thread = api.start_thread("非法状态", scenario="text_multi_turn")["thread"]
    turn = api.run_turn(thread["thread_id"], "先跑完一轮")["turn"]
    # 已完成的 Turn 不能再中断（终态无出边）
    error = api.fails(
        "turn/interrupt",
        {"thread_id": thread["thread_id"], "turn_id": turn["turn_id"]},
        expect=ErrorCode.INVALID_STATE.value,
        idem="interrupt-completed",
    )
    assert error["data"]["turn_id"] == turn["turn_id"]
    assert error["data"]["from"] == "completed"


def test_continue_requires_waiting_input(api) -> None:
    thread = api.start_thread("继续条件", scenario="text_multi_turn")["thread"]
    turn = api.run_turn(thread["thread_id"], "一轮")["turn"]
    error = api.fails(
        "turn/continue",
        {"thread_id": thread["thread_id"], "turn_id": turn["turn_id"], "text": "继续"},
        expect=ErrorCode.INVALID_STATE.value,
        idem="continue-not-waiting",
    )
    # 已完成的回合：给出可行动的细分（直接发新消息），而不是笼统的非法迁移
    assert error["data"]["kind"] == "turn_completed"
    assert error["data"]["status"] == "completed"


def test_steer_requires_active_turn(api) -> None:
    thread = api.start_thread("追加条件", scenario="text_multi_turn")["thread"]
    turn = api.run_turn(thread["thread_id"], "一轮")["turn"]
    error = api.fails(
        "turn/steer",
        {"thread_id": thread["thread_id"], "turn_id": turn["turn_id"], "text": "补充"},
        expect=ErrorCode.INVALID_STATE.value,
        idem="steer-completed",
    )
    assert error["data"]["kind"] == "turn_completed"
    assert error["data"]["turn_id"] == turn["turn_id"]


def test_concurrent_turn_is_rejected(api) -> None:
    thread = api.start_thread("并发 Turn", scenario="turn_interrupt")["thread"]
    result = api.call("turn/start", {"thread_id": thread["thread_id"], "text": "第一轮"}, idem="t1")
    turn_id = result["turn"]["turn_id"]
    api.wait_status(thread["thread_id"], turn_id, {"running"})
    error = api.fails(
        "turn/start",
        {"thread_id": thread["thread_id"], "text": "第二轮"},
        expect=ErrorCode.CONCURRENT_TURN.value,
        idem="t2",
    )
    assert "active turn" in error["message"]


def test_cursor_ahead_is_stale_cursor(api) -> None:
    thread = api.start_thread("游标越过末尾", scenario="text_multi_turn")["thread"]
    api.subscribe(thread["thread_id"])
    last = api.last_sequence(thread["thread_id"])
    error = api.fails(
        "thread/events/replay",
        {"thread_id": thread["thread_id"], "after_sequence": last + 50},
        expect=ErrorCode.CURSOR_EXPIRED.value,
    )
    assert error["data"]["reason"] == "cursor_ahead"
    assert error["data"]["last_sequence"] == last


def test_cursor_below_replay_floor_is_stale_cursor(api, service) -> None:
    thread = api.start_thread("游标过期", scenario="text_multi_turn")["thread"]
    api.run_turn(thread["thread_id"], "产生若干事件")
    last = api.last_sequence(thread["thread_id"])
    service.facade.set_replay_floor(thread["thread_id"], last - 1)
    error = api.fails(
        "thread/events/replay",
        {"thread_id": thread["thread_id"], "after_sequence": 1},
        expect=ErrorCode.CURSOR_EXPIRED.value,
    )
    assert error["data"]["reason"] == "below_replay_floor"
    assert error["data"]["floor"] == last - 1


def test_cursor_gap_in_history_is_reported_as_corruption(api, service) -> None:
    """事件流被外部改写出现序号缺口时，必须结构化报错，绝不静默跳过。

    缺口由事件存储（Agent 1 的 ``EventStore``）判定为 ``corrupted_event``；
    「游标过期」则由 ``stale_cursor`` 覆盖（见上面两条用例）。
    """
    thread = api.start_thread("历史缺口", scenario="text_multi_turn")["thread"]
    api.run_turn(thread["thread_id"], "第一轮")
    api.run_turn(thread["thread_id"], "第二轮")
    store = service.repo.store(thread["thread_id"])
    lines = store.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 5
    # 删掉第 2 行 -> 序号 1,3,4… 出现缺口（模拟外部工具改写过事件流）
    store.events_path.write_text("\n".join([lines[0], *lines[2:]]) + "\n", encoding="utf-8")
    error = api.fails(
        "thread/events/replay",
        {"thread_id": thread["thread_id"], "after_sequence": 1},
        expect=ErrorCode.CORRUPTED_EVENT.value,
    )
    assert error["data"]["sequence"] == 3
    assert "gap" in error["data"]["reason"]


def test_not_subscribed_error(api) -> None:
    thread = api.start_thread("未订阅", scenario="text_multi_turn")["thread"]
    error = api.fails(
        "thread/unsubscribe", {"thread_id": thread["thread_id"]}, expect=ErrorCode.NOT_SUBSCRIBED.value
    )
    assert error["data"]["thread_id"] == thread["thread_id"]


def test_duplicate_request_id_with_different_payload(api) -> None:
    thread = api.start_thread("重复请求 id", scenario="text_multi_turn")["thread"]
    first = api.raw("thread/resume", {"thread_id": thread["thread_id"]}, request_id="dup-1")
    assert first["ok"] is True
    error = api.fails(
        "thread/resume",
        {"thread_id": new_id("thread")},
        expect=ErrorCode.IDEMPOTENCY_CONFLICT.value,
        request_id="dup-1",
    )
    assert error["data"]["request_id"] == "dup-1"
    # 同 id 同载荷是合法重发：不报错
    again = api.raw("thread/resume", {"thread_id": thread["thread_id"]}, request_id="dup-1")
    assert again["ok"] is True


def test_shutting_down_rejects_new_requests(api, service) -> None:
    thread = api.start_thread("服务关闭", scenario="text_multi_turn")["thread"]
    api.run(service.shutdown())
    error = api.fails(
        "thread/resume", {"thread_id": thread["thread_id"]}, expect=ErrorCode.SERVER_SHUTTING_DOWN.value
    )
    assert error["data"]["method"] == "thread/resume"
    api.run(service.host.shutdown())


def test_protocol_describe_reports_capabilities(api) -> None:
    described = api.call("protocol/describe", {})
    assert described["protocol_version"] == PROTOCOL_VERSION
    assert described["contract_version"] == "agent.v2.contract.v1"
    assert described["contract_freeze_tag"] == "agent-v2-contract-v1"
    assert "thread/start" in {row["name"] for row in described["methods"]}
    assert "text_multi_turn" in described["scenarios"]


def test_events_are_schema_valid(api) -> None:
    """写进事件流的每一条事件都必须满足冻结的 events.schema.json。"""
    from contracts.agent_v2.validate import validator_for

    thread = api.start_thread("事件合规", scenario="tool_parallel")["thread"]
    api.run_turn(thread["thread_id"], "跑一轮带工具的执行")
    validator = validator_for("events")
    for event in api.events(thread["thread_id"]):
        problems = validator.errors(event.to_dict())
        assert not problems, f"{event.type} 不合规：{problems}"


def test_thread_directory_layout_matches_contract(api) -> None:
    """落盘布局必须是 knowledge-base/conversations/<名称-id>/events.jsonl。"""
    result = api.start_thread("布局核对", scenario="text_multi_turn")
    thread_id = result["thread"]["thread_id"]
    store = api.service.repo.store(thread_id)
    assert store.folder.startswith("布局核对-")
    assert store.events_path.name == "events.jsonl"
    assert store.snapshots_dir.name == "snapshots"
    raw = json.loads(store.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert raw["type"] == "thread/created"
    assert raw["contract"] == "agent.v2.contract.v1"
