"""请求幂等与重复提交测试（验收书 §2、§4）。

核心断言：**断线重试不重复创建 Turn**、审批结论不重复提交、幂等键复用规则明确。
"""

from __future__ import annotations

from api.v2.agent_protocol import ErrorCode


def test_turn_start_is_idempotent_on_retry(api) -> None:
    thread = api.start_thread("幂等重发", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    key = "retry-turn-1"

    first = api.raw("turn/start", {"thread_id": thread_id, "text": "第一次提交"}, idem=key)
    assert first["ok"] is True and first["replayed"] is False
    turn_id = first["result"]["turn"]["turn_id"]

    # 模拟断线重试：同一条请求原样重发
    second = api.raw("turn/start", {"thread_id": thread_id, "text": "第一次提交"}, idem=key)
    assert second["ok"] is True
    assert second["replayed"] is True, "重复提交必须走幂等缓存"
    assert second["result"]["turn"]["turn_id"] == turn_id

    starts = api.find_events(thread_id, "turn/started")
    assert len(starts) == 1, "重复提交不得产生第二个 Turn"
    assert len(api.state(thread_id).turns) == 1


def test_retry_after_turn_finished_still_returns_same_turn(api) -> None:
    thread = api.start_thread("完成后重发", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    key = "retry-turn-2"
    first = api.raw("turn/start", {"thread_id": thread_id, "text": "跑完"}, idem=key)
    turn_id = first["result"]["turn"]["turn_id"]
    api.wait_idle(thread_id)

    again = api.raw("turn/start", {"thread_id": thread_id, "text": "跑完"}, idem=key)
    assert again["replayed"] is True
    assert again["result"]["turn"]["turn_id"] == turn_id
    assert len(api.state(thread_id).turns) == 1


def test_idempotency_key_reuse_with_other_payload_is_rejected(api) -> None:
    thread = api.start_thread("键复用", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    api.call("turn/start", {"thread_id": thread_id, "text": "原始输入"}, idem="shared-key")
    error = api.fails(
        "turn/start",
        {"thread_id": thread_id, "text": "完全不同的输入"},
        expect=ErrorCode.IDEMPOTENCY_CONFLICT.value,
        idem="shared-key",
    )
    assert error["data"]["idempotency_key"] == "shared-key"


def test_idempotency_scope_is_per_thread(api) -> None:
    first = api.start_thread("作用域 A", scenario="text_multi_turn")["thread"]
    second = api.start_thread("作用域 B", scenario="text_multi_turn")["thread"]
    key = "same-key-different-thread"
    api.call("turn/start", {"thread_id": first["thread_id"], "text": "A"}, idem=key)
    result = api.raw(
        "turn/start", {"thread_id": second["thread_id"], "text": "A"}, idem=key
    )
    assert result["ok"] is True
    assert result["replayed"] is False, "不同线程的同一个幂等键必须互不影响"


def test_approval_resolve_is_idempotent(api) -> None:
    thread = api.start_thread("审批幂等", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="ap-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    approval = next(iter(api.state(thread_id).approvals.values()))

    key = "approve-once-1"
    first = api.raw(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        idem=key,
    )
    assert first["ok"] is True and first["replayed"] is False

    second = api.raw(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        idem=key,
    )
    assert second["replayed"] is True
    assert len(api.find_events(thread_id, "approval/granted")) == 1


def test_approval_resolve_after_decision_without_key_is_still_safe(api) -> None:
    """换一个幂等键重复提交同一审批结论：按既有结论返回，不产生第二个副作用。"""
    thread = api.start_thread("审批二次提交", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="ap-t2")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    approval = next(iter(api.state(thread_id).approvals.values()))
    payload = {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "approval_id": approval.approval_id,
        "decision": "deny",
    }
    api.call("approval/resolve", payload, idem="deny-1")
    second = api.raw("approval/resolve", payload, idem="deny-2")
    assert second["ok"] is True
    assert second["result"]["replayed"] is True
    assert len(api.find_events(thread_id, "approval/denied")) == 1


def test_idempotency_store_stats(api, service) -> None:
    thread = api.start_thread("幂等统计", scenario="text_multi_turn")["thread"]
    api.call("turn/start", {"thread_id": thread["thread_id"], "text": "一次"}, idem="stats-1")
    before = service.idempotency.stats()
    assert before["entries"] >= 1
    api.raw("turn/start", {"thread_id": thread["thread_id"], "text": "一次"}, idem="stats-1")
    after = service.idempotency.stats()
    assert after["hits"] > before["hits"]
