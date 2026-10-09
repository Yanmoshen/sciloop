"""Agent Tree 工具验收（对应验收书 §6）。

- spawn / send / wait / interrupt / close 的参数符合契约（非法参数在执行前被拒绝）；
- 工具**不直接修改父 Agent 历史**（父线程只多出子关系事件，不产生子 Agent 的 Item）；
- 子 Agent 结果带来源 Thread / Turn / Agent ID。
"""

from __future__ import annotations

from tool_helpers import make_call, run

from contracts.agent_v2 import ToolCallStatus
from services.agent_threads_v2 import AgentTree, ThreadRepository
from services.tool_registry_v2 import ToolRegistry
from services.tool_registry_v2.builtin import default_tool_definitions


def build(tmp_path, workspace):
    repo = ThreadRepository(tmp_path / "conversations")
    tree = AgentTree(repo)
    parent = repo.create_thread("父 Agent", cwd=str(workspace))
    registry = ToolRegistry(agent_tree=tree, default_cwd=str(workspace))
    registry.register_all(default_tool_definitions())
    return repo, tree, parent, registry


def call_for(registry, parent, name, arguments, kind=None):

    definition = registry.definition(name)
    assert definition is not None
    return make_call(
        name,
        arguments,
        kind=kind or definition.kind,
        thread_id=parent.thread_id,
        turn_id="tu_parent",
    )


# ---------------------------------------------------------------------------- §6.1
def test_five_agent_tools_exist_with_declared_params(registry) -> None:
    for name in (
        "spawn_agent",
        "send_message",
        "wait_agent",
        "interrupt_agent",
        "close_agent",
    ):
        definition = registry.definition(name)
        assert definition is not None, f"缺少工具 {name}"
        assert definition.input_schema.get("required"), f"{name} 必须声明必填参数"


def test_spawn_agent_creates_child_and_delivers_task(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    result = run(
        registry.execute(
            call_for(
                registry,
                parent,
                "spawn_agent",
                {"name": "文献调研", "task": "读三篇论文", "cwd": str(workspace)},
            )
        )
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    child_id = result.output["agent_id"]
    assert child_id != parent.thread_id
    assert result.output["parent_thread_id"] == parent.thread_id
    assert result.output["thread_id"] == child_id
    assert result.output["status"] in {"spawned", "scheduled"}

    # 任务投在**子线程**邮箱里（不写父历史）
    mailbox = tree.mailbox(child_id)
    assert [item["content"] for item in mailbox] == ["读三篇论文"]
    assert mailbox[0]["from_thread_id"] == parent.thread_id
    child_state = repo.state(child_id)
    assert not child_state.turns, "工具只登记任务，不越权驱动子 Agent 的 Turn"

    parent_items = repo.state(parent.thread_id).items
    assert not parent_items, "创建子 Agent 不得往父线程塞 Item（父历史不被工具改写）"
    parent_events = [event.type for event in repo.store(parent.thread_id).read_all()]
    assert "agent/child_created" in parent_events


def test_spawn_agent_requires_name_and_task(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    missing_task = run(
        registry.execute(call_for(registry, parent, "spawn_agent", {"name": "只有名字"}))
    )
    assert missing_task.status is ToolCallStatus.INVALID_ARGUMENTS
    assert missing_task.error["code"] == "invalid_arguments"

    blank_name = run(
        registry.execute(call_for(registry, parent, "spawn_agent", {"name": "", "task": "x"}))
    )
    assert blank_name.status is ToolCallStatus.INVALID_ARGUMENTS


def test_send_message_only_to_own_child(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    spawned = run(
        registry.execute(call_for(registry, parent, "spawn_agent", {"name": "子", "task": "任务"}))
    )
    child_id = spawned.output["agent_id"]

    sent = run(
        registry.execute(
            call_for(
                registry,
                parent,
                "send_message",
                {"agent_id": child_id, "content": "补充说明"},
            )
        )
    )
    assert sent.status is ToolCallStatus.SUCCEEDED
    assert sent.output["to_thread_id"] == child_id
    assert sent.output["from_thread_id"] == parent.thread_id
    assert sent.output["sequence"] > 0
    assert [item["content"] for item in tree.mailbox(child_id)][-1] == "补充说明"

    # 别的线程不能给别人的子 Agent 发消息
    stranger = repo.create_thread("陌生线程")
    forged = make_call(
        "send_message",
        {"agent_id": child_id, "content": "越权消息"},
        kind=registry.definition("send_message").kind,
        thread_id=stranger.thread_id,
        turn_id="tu_stranger",
    )
    refused = run(registry.execute(forged))
    assert refused.status is ToolCallStatus.FAILED
    assert refused.error["code"] == "not_my_child"


def test_wait_agent_returns_results_with_source_ids(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    children = []
    for index in range(2):
        spawned = run(
            registry.execute(
                call_for(
                    registry,
                    parent,
                    "spawn_agent",
                    {"name": f"子{index}", "task": f"任务{index}"},
                )
            )
        )
        children.append(spawned.output["agent_id"])

    # 子 Agent 回传结果（Agent 1 的接口；工具不代劳）
    for index, child_id in enumerate(children):
        tree.report_result(child_id, summary=f"结论{index}")

    result = run(
        registry.execute(
            call_for(registry, parent, "wait_agent", {"agent_ids": children, "timeout_s": 5})
        )
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.output["converged"] == sorted(children)
    assert result.output["timed_out"] == []
    for index, child_id in enumerate(children):
        entry = result.output["results"][child_id]
        assert entry["thread_id"] == child_id
        assert entry["agent_id"] == child_id
        assert entry["parent_thread_id"] == parent.thread_id
        assert entry["status"] == "completed"
        assert entry["summary"] == f"结论{index}"
        assert entry["result_item_id"]


def test_wait_agent_unknown_child_is_rejected(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    result = run(
        registry.execute(
            call_for(registry, parent, "wait_agent", {"agent_ids": ["th_0000000000000aaaaaaaaaa"]})
        )
    )
    assert result.status is ToolCallStatus.FAILED
    assert result.error["code"] == "agent_not_found"


# ---------------------------------------------------------------------------- §6.2
def test_interrupt_agent_stops_child_turn_without_touching_parent(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    spawned = run(
        registry.execute(call_for(registry, parent, "spawn_agent", {"name": "子", "task": "任务"}))
    )
    child_id = spawned.output["agent_id"]
    turn = repo.start_turn(child_id, inputs=[{"text": "开始"}])
    assert repo.state(child_id).active_turn is not None

    result = run(
        registry.execute(
            call_for(registry, parent, "interrupt_agent", {"agent_id": child_id, "reason": "父要求停止"})
        )
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.output["agent_id"] == child_id
    assert result.output["parent_thread_id"] == parent.thread_id
    assert result.output["status"] == "interrupted"
    assert repo.state(child_id).turns[turn.turn_id].status.value == "interrupted"

    parent_state = repo.state(parent.thread_id)
    assert parent_state.active_turn is None, "父 Agent 的状态不受影响"
    assert not parent_state.turns, "父线程不产生 Turn/Item"


def test_close_agent_archives_and_interrupts_active_turn(tmp_path, workspace) -> None:
    repo, tree, parent, registry = build(tmp_path, workspace)
    spawned = run(
        registry.execute(call_for(registry, parent, "spawn_agent", {"name": "子", "task": "任务"}))
    )
    child_id = spawned.output["agent_id"]
    repo.start_turn(child_id, inputs=[{"text": "开始"}])

    closed = run(
        registry.execute(call_for(registry, parent, "close_agent", {"agent_id": child_id}))
    )
    assert closed.status is ToolCallStatus.SUCCEEDED
    assert closed.output["closed"] is True
    assert closed.output["interrupted_active_turn"] is True
    assert repo.state(child_id).thread.status == "archived"
    event_types = [event.type for event in repo.store(child_id).read_all()]
    assert "thread/archived" in event_types

    # 关闭后仍处于可查询状态（不删除数据）
    assert repo.exists(child_id)
