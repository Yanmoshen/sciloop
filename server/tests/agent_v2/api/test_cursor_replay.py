"""事件游标重放与订阅推送测试（验收书 §2、§6）。

断言的重点是**序号语义**：连续、单调、不重复、可从任意游标补齐。
"""

from __future__ import annotations

from api.v2.agent_protocol import ErrorCode


def test_replay_from_zero_returns_full_contiguous_history(api) -> None:
    thread = api.start_thread("从头重放", scenario="text_multi_turn")["thread"]
    api.run_turn(thread["thread_id"], "一轮")
    replayed = api.call(
        "thread/events/replay", {"thread_id": thread["thread_id"], "after_sequence": 0}
    )
    sequences = [event["sequence"] for event in replayed["events"]]
    assert sequences == list(range(1, len(sequences) + 1))
    assert replayed["last_sequence"] == sequences[-1]
    assert replayed["has_more"] is False
    assert replayed["next_after"] == sequences[-1]


def test_replay_from_cursor_returns_only_newer_events(api) -> None:
    thread = api.start_thread("增量重放", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮")
    cursor = api.last_sequence(thread_id)
    api.run_turn(thread_id, "第二轮")
    replayed = api.call(
        "thread/events/replay", {"thread_id": thread_id, "after_sequence": cursor}
    )
    assert replayed["events"], "第二轮必须产生新事件"
    assert all(event["sequence"] > cursor for event in replayed["events"])
    assert min(event["sequence"] for event in replayed["events"]) == cursor + 1


def test_replay_at_tail_is_empty(api) -> None:
    thread = api.start_thread("尾部重放", scenario="text_multi_turn")["thread"]
    api.run_turn(thread["thread_id"], "一轮")
    tail = api.last_sequence(thread["thread_id"])
    replayed = api.call(
        "thread/events/replay", {"thread_id": thread["thread_id"], "after_sequence": tail}
    )
    assert replayed["events"] == []
    assert replayed["count"] == 0
    assert replayed["next_after"] == tail
    assert replayed["has_more"] is False


def test_replay_pagination_with_limit(api) -> None:
    thread = api.start_thread("分页重放", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "一轮")
    first = api.call("thread/events/replay", {"thread_id": thread_id, "after_sequence": 0, "limit": 3})
    assert first["count"] == 3
    assert first["has_more"] is True
    assert first["next_after"] == 3
    second = api.call(
        "thread/events/replay", {"thread_id": thread_id, "after_sequence": first["next_after"], "limit": 3}
    )
    assert [event["sequence"] for event in second["events"]] == [4, 5, 6]


def test_replay_filter_by_call_id(api) -> None:
    thread = api.start_thread("按调用过滤", scenario="tool_parallel")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "跑工具")
    events = api.events(thread_id)
    call_ids = {event.call_id for event in events if event.call_id}
    assert call_ids, "工具场景必须产生带 call_id 的事件"
    target = sorted(call_ids)[0]
    replayed = api.call(
        "thread/events/replay",
        {"thread_id": thread_id, "after_sequence": 0, "call_id": target},
    )
    assert replayed["events"]
    assert {event["call_id"] for event in replayed["events"]} == {target}


def test_subscribe_then_pump_delivers_pending_events(api) -> None:
    thread = api.start_thread("订阅补拉", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "一轮")
    api.clear_notifications()

    result = api.subscribe(thread_id, after_sequence=0)
    assert result["cursor"] == 0
    assert result["pending"] == api.last_sequence(thread_id)

    notes = api.events_notifications(thread_id)
    sequences = [note["sequence"] for note in notes]
    assert sequences == list(range(1, len(sequences) + 1))
    for note in notes:
        assert note["thread_id"] == thread_id
        assert note["event_id"].startswith("ev_")


def test_pump_is_not_repeating_old_events(api) -> None:
    thread = api.start_thread("不重复推送", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "一轮")
    api.clear_notifications()
    api.subscribe(thread_id, after_sequence=0)
    first_batch = len(api.events_notifications(thread_id))
    api.collect()
    api.collect()
    assert len(api.events_notifications(thread_id)) == first_batch, "游标推进后不得重复投递"


def test_subscribe_with_cursor_only_delivers_new_events(api) -> None:
    thread = api.start_thread("游标订阅", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮")
    cursor = api.last_sequence(thread_id)
    api.clear_notifications()

    result = api.subscribe(thread_id, after_sequence=cursor)
    assert result["pending"] == 0
    assert api.events_notifications(thread_id) == []

    api.run_turn(thread_id, "第二轮")
    sequences = [note["sequence"] for note in api.events_notifications(thread_id)]
    assert sequences, "第二轮必须被推送给订阅方"
    assert min(sequences) == cursor + 1


def test_unsubscribe_stops_delivery_and_notifies(api) -> None:
    thread = api.start_thread("取消订阅", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.subscribe(thread_id, after_sequence=0)
    api.collect()
    api.clear_notifications()

    result = api.call("thread/unsubscribe", {"thread_id": thread_id})
    assert result["subscribed"] is False
    cancelled = api.notifications_of("subscription/cancelled")
    assert cancelled and cancelled[-1]["thread_id"] == thread_id
    assert cancelled[-1]["params"]["reason"] == "unsubscribed"

    api.run_turn(thread_id, "取消后的一轮")
    assert api.events_notifications(thread_id) == [], "取消订阅后不应再收到事件"


def test_subscribe_with_cursor_after_tail_is_rejected(api) -> None:
    thread = api.start_thread("游标越界", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    error = api.fails(
        "thread/subscribe",
        {"thread_id": thread_id, "after_sequence": 9999},
        expect=ErrorCode.CURSOR_EXPIRED.value,
    )
    assert error["data"]["reason"] == "cursor_ahead"


def test_stale_cursor_closes_subscription_with_reason(api, service) -> None:
    """重写事件流抬高重放起点后，订阅方收到明确的 thread/closed 而不是静默丢事件。"""
    thread = api.start_thread("订阅游标失效", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "一轮")
    last = api.last_sequence(thread_id)
    # 订阅到当前末尾：游标 == last
    api.subscribe(thread_id, after_sequence=last)
    api.collect()
    api.clear_notifications()

    # 事件流被修复/重写，可重放起点抬高到 last + 1 -> 客户端游标已过期
    service.facade.set_replay_floor(thread_id, last + 1)
    api.collect()

    closed = api.notifications_of("thread/closed")
    assert closed, "游标失效必须通知客户端"
    assert closed[-1]["params"]["reason"] == "below_replay_floor"
    assert closed[-1]["params"]["cursor"] == last
    assert thread_id not in api.conn.subscriptions


def test_disconnect_clears_subscriptions(api, service) -> None:
    thread = api.start_thread("断线清理", scenario="text_multi_turn")["thread"]
    api.subscribe(thread["thread_id"], after_sequence=0)
    assert len(api.conn.subscriptions) == 1
    cleared = service.disconnect(api.conn.connection_id)
    assert cleared == [thread["thread_id"]]
    assert len(api.conn.subscriptions) == 0


def test_replay_does_not_call_model(api, service) -> None:
    """重放/补拉绝不能触发模型调用（验收书 §6）。"""
    thread = api.start_thread("重放不调模型", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "一轮")
    runtime = service.host.runtime_for(thread_id)
    calls_before = runtime.gateway.call_count
    api.call("thread/events/replay", {"thread_id": thread_id, "after_sequence": 0})
    api.subscribe(thread_id, after_sequence=0)
    api.collect()
    api.call("thread/resume", {"thread_id": thread_id})
    assert runtime.gateway.call_count == calls_before
