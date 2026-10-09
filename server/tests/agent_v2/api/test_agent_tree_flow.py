"""Agent Tree 测试（验收书 §5）。

覆盖：子 Agent 的创建 / 运行 / 等待 / 失败 / 中断 / 完成、结果回传与来源标注。
"""

from __future__ import annotations

from api.v2.agent_protocol import ErrorCode


def _run_parent(api, name: str = "子 Agent 会话"):
    thread = api.start_thread(name, scenario="child_agents")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "拆成三路执行")
    return thread_id


def _children_by_name(api, thread_id) -> dict:
    listed = api.call("agent/list", {"thread_id": thread_id})
    return {child["name"]: child for child in listed["children"]}


def test_agent_tree_records_all_three_outcomes(api) -> None:
    thread_id = _run_parent(api)
    children = _children_by_name(api, thread_id)
    assert set(children) == {"文献调研", "复现实验", "写作"}
    assert children["文献调研"]["status"] == "completed"
    assert children["复现实验"]["status"] == "failed"
    assert children["写作"]["status"] in {"running", "completed"}

    created = api.find_events(thread_id, "agent/child_created")
    assert len(created) == 3
    assert {event.payload["name"] for event in created} == set(children)

    completed = api.find_events(thread_id, "agent/child_completed")
    assert completed, "成功收敛的子 Agent 必须有完成事件"
    assert completed[-1].payload["summary"] == "找到 42 篇高相关文献"

    failed = api.find_events(thread_id, "agent/child_failed")
    assert failed, "失败的子 Agent 必须有失败事件与错误内容"
    assert failed[-1].payload["summary"] == "环境缺少依赖，未能复现"


def test_child_results_are_linked_to_parent_items(api) -> None:
    thread_id = _run_parent(api, "结果来源标注")
    state = api.state(thread_id)
    results = [item for item in state.items.values() if item.type.value == "subagent_result"]
    assert results, "子 Agent 结果必须作为结构化 Item 回传父线程"
    for item in results:
        assert item.subagent_thread_id, "结果 Item 必须标注来源子线程"
        assert item.thread_id == thread_id
        assert item.payload["status"] in {"completed", "failed", "interrupted"}

    summary_source = {
        item.subagent_thread_id: item.payload["summary"] for item in results
    }
    assert summary_source, "结果 Item 必须携带摘要"

    # 子线程自身的事件流里也能看到独立生命周期（不是把子线程内容拼进父线程文本）
    for child_id in summary_source:
        child_types = api.event_types(child_id)
        assert child_types[0] == "thread/created"
        assert "agent/message" in child_types


def test_child_mailbox_keeps_parent_task_and_source(api) -> None:
    thread_id = _run_parent(api, "父子消息")
    children = _children_by_name(api, thread_id)
    child_id = children["文献调研"]["thread_id"]
    mailbox = api.state(child_id).mailbox
    assert mailbox, "父 Agent 的任务必须投递到子线程邮箱"
    message = mailbox[-1]
    assert message["from_thread_id"] == thread_id
    assert message["to_thread_id"] == child_id
    assert message["kind"] == "task"
    assert "检索" in message["content"]


def test_agent_wait_converges_finished_children(api) -> None:
    thread_id = _run_parent(api, "等待子 Agent")
    children = _children_by_name(api, thread_id)
    done_ids = [children["文献调研"]["thread_id"], children["复现实验"]["thread_id"]]
    result = api.call(
        "agent/wait", {"thread_id": thread_id, "child_thread_ids": done_ids, "timeout_s": 2.0}
    )
    outcomes = result["outcomes"]
    assert set(outcomes) == set(done_ids)
    assert outcomes[done_ids[0]]["status"] == "completed"
    assert outcomes[done_ids[0]]["summary"]
    assert outcomes[done_ids[1]]["status"] == "failed"
    assert outcomes[done_ids[1]]["error"]["message"]


def test_agent_wait_times_out_on_running_child(api) -> None:
    thread_id = _run_parent(api, "等待超时")
    children = _children_by_name(api, thread_id)
    running_id = children["写作"]["thread_id"]
    result = api.call(
        "agent/wait",
        {"thread_id": thread_id, "child_thread_ids": [running_id], "timeout_s": 0.2},
    )
    outcome = result["outcomes"][running_id]
    assert outcome["status"] == "timeout"
    assert outcome["error"]["code"] == "wait_timeout"


def test_agent_interrupt_child_notifies_parent(api) -> None:
    thread_id = _run_parent(api, "中断子 Agent")
    children = _children_by_name(api, thread_id)
    child_id = children["写作"]["thread_id"]

    result = api.call(
        "agent/interrupt",
        {"thread_id": thread_id, "child_thread_id": child_id, "reason": "父 Agent 收回任务"},
        idem="child-int-1",
    )
    assert result["child"]["status"] == "interrupted"
    # 子线程自身状态被中断，父监听线程收到通知事件
    assert api.state(child_id).active_turn is None
    interrupted = api.find_events(thread_id, "agent/child_interrupted")
    assert interrupted and interrupted[-1].payload["child_thread_id"] == child_id
    assert interrupted[-1].payload["reason"] == "父 Agent 收回任务"

    # 父 Agent 自身不受影响：仍可继续开新 Turn
    api.run_turn(thread_id, "继续自己的工作")
    state = api.state(thread_id)
    assert state.turns[state.turn_order[-1]].status.value == "completed"


def test_agent_list_validation(api) -> None:
    api.fails("agent/list", {"thread_id": "th_bad"}, expect=ErrorCode.INVALID_ID.value)
    api.fails(
        "agent/wait",
        {"thread_id": "th_" + "0" * 23, "child_thread_ids": []},
        expect=ErrorCode.INVALID_PARAMS.value,
    )


def test_spawned_child_has_own_path(api) -> None:
    thread_id = _run_parent(api, "父子路径")
    children = _children_by_name(api, thread_id)
    child = children["文献调研"]
    assert child["path"][0] == thread_id
    assert child["path"][-1] == child["thread_id"]
    assert len(child["path"]) == 2
    assert child["parent_thread_id"] == thread_id
