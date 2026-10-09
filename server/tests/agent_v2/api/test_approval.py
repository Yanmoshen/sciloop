"""审批与授权测试（验收书 §4）。

覆盖：审批卡片所需字段、批准一次 / 本对话始终批准 / 完全访问 / 拒绝 / 取消、
拒绝结果回喂模型、拒绝不显示伪成功、结论不重复提交。
"""

from __future__ import annotations

import json

from api.v2.agent.fixtures import parse_lines
from contracts.agent_v2.enums import ToolKind
from contracts.agent_v2.models import ToolCall


def _two_command_scenario():
    """两个回合都调用同一条命令：用于验证「本对话始终批准」是否真的生效。"""

    def tool_call(call_id: str) -> dict:
        return {
            "kind": "tool_call_completed",
            "call_id": call_id,
            "name": "run_command",
            "arguments": {"cmd": "python -m pytest -q", "cwd": "D:/work"},
        }

    lines = [
        json.dumps({"type": "tool_spec", "name": "run_command", "kind": "side_effect", "description": "执行宿主机命令（高危）"}, ensure_ascii=False),
        json.dumps({"type": "approval_required", "name": "run_command"}, ensure_ascii=False),
        json.dumps({"type": "tool_result", "name": "run_command", "status": "succeeded", "output": {"exit_code": 0, "stdout": "12 passed"}}, ensure_ascii=False),
        json.dumps({"type": "model_response", "items": [tool_call("call_" + "1" * 23), {"kind": "completed", "stop_reason": "tool_use"}]}, ensure_ascii=False),
        json.dumps({"type": "model_response", "items": [{"kind": "text_delta", "text": "第一轮命令已通过。"}, {"kind": "completed", "stop_reason": "end_turn"}]}, ensure_ascii=False),
        json.dumps({"type": "model_response", "items": [tool_call("call_" + "2" * 23), {"kind": "completed", "stop_reason": "tool_use"}]}, ensure_ascii=False),
        json.dumps({"type": "model_response", "items": [{"kind": "text_delta", "text": "第二轮命令未再询问。"}, {"kind": "completed", "stop_reason": "end_turn"}]}, ensure_ascii=False),
    ]
    return parse_lines(lines, name="inline_two_commands")


def _wait_approval(api, thread_id: str, turn_id: str):
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    state = api.state(thread_id)
    return next(
        approval for approval in state.approvals.values() if approval.turn_id == turn_id
    )


def test_approval_card_carries_command_arguments_directory_and_risk(api) -> None:
    thread = api.start_thread("审批卡片", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="card-t1")
    turn_id = started["turn"]["turn_id"]
    approval = _wait_approval(api, thread_id, turn_id)

    assert approval.status.value == "pending"
    action = approval.action
    assert action["tool"] == "run_command"
    assert action["arguments"]["cmd"] == "python -m pytest -q"
    assert action["arguments"]["cwd"] == "D:/work"
    assert action["kind"] == "side_effect"
    assert action["risk"], "审批卡片必须带风险描述"
    assert approval.call_id

    event = api.find_events(thread_id, "approval/requested")[-1]
    assert event.payload["approval"]["approval_id"] == approval.approval_id
    assert event.call_id == approval.call_id

    # 挂起时必须是 waiting_approval，且绝不能已经执行成功
    assert api.state(thread_id).turn(turn_id).status.value == "waiting_approval"
    assert not api.find_events(thread_id, "tool/completed")

    api.call("approval/resolve", {"thread_id": thread_id, "turn_id": turn_id, "approval_id": approval.approval_id, "decision": "deny"}, idem="card-deny")
    api.wait_idle(thread_id)


def test_approve_once_runs_the_tool_then_completes(api) -> None:
    thread = api.start_thread("批准一次", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="once-t1")
    turn_id = started["turn"]["turn_id"]
    approval = _wait_approval(api, thread_id, turn_id)

    result = api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        idem="once-1",
    )
    assert result["decision"] == "approve_once"
    assert result["approval"]["decision_scope"] == "once"
    api.wait_idle(thread_id)

    state = api.state(thread_id)
    assert state.turn(turn_id).status.value == "completed"
    calls = list(state.tool_calls.values())
    assert len(calls) == 1
    assert calls[0].status.value == "succeeded"
    assert calls[0].duration_ms is not None and calls[0].duration_ms >= 0
    assert calls[0].call_id == approval.call_id

    tool_results = [
        item for item in state.items.values() if item.type.value == "tool_result"
    ]
    assert tool_results[-1].payload["output"]["exit_code"] == 0
    # 「批准一次」不写会话级规则
    assert not state.thread.settings.get("permission_rules")


def test_approve_conversation_records_prefix_rule_and_skips_next_time(api, service) -> None:
    thread = api.start_thread("本对话始终批准")["thread"]
    thread_id = thread["thread_id"]
    service.host.bind_scenario(thread_id, _two_command_scenario())

    first = api.call("turn/start", {"thread_id": thread_id, "text": "第一次"}, idem="conv-t1")
    approval = _wait_approval(api, thread_id, first["turn"]["turn_id"])
    api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": first["turn"]["turn_id"],
            "approval_id": approval.approval_id,
            "decision": "approve_conversation",
        },
        idem="conv-1",
    )
    api.wait_idle(thread_id)

    rules = api.state(thread_id).thread.settings.get("permission_rules")
    assert rules, "本对话始终批准必须留下规范化规则"
    assert rules[-1]["tool"] == "run_command"
    assert rules[-1]["prefix"] == ["cmd=python -m pytest -q"]
    assert rules[-1]["cwd"] == api.state(thread_id).thread.cwd
    assert rules[-1]["scope"] == "conversation"

    # 第二次同样的命令：gate 直接放行，不再产生审批请求
    approvals_before = len(api.find_events(thread_id, "approval/requested"))
    second = api.call("turn/start", {"thread_id": thread_id, "text": "第二次"}, idem="conv-t2")
    api.wait_idle(thread_id)
    assert api.state(thread_id).turn(second["turn"]["turn_id"]).status.value == "completed"
    assert len(api.find_events(thread_id, "approval/requested")) == approvals_before


def test_full_access_auto_approves_and_records_audit(api, service) -> None:
    thread = api.start_thread("完全访问")["thread"]
    thread_id = thread["thread_id"]
    service.host.bind_scenario(thread_id, _two_command_scenario())

    first = api.call("turn/start", {"thread_id": thread_id, "text": "第一次"}, idem="full-t1")
    approval = _wait_approval(api, thread_id, first["turn"]["turn_id"])
    api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": first["turn"]["turn_id"],
            "approval_id": approval.approval_id,
            "decision": "full_access",
        },
        idem="full-1",
    )
    api.wait_idle(thread_id)

    settings = api.state(thread_id).thread.settings
    assert settings.get("full_access") is True
    assert api.find_events(thread_id, "approval/granted")[-1].payload["approval"]["decision_scope"] == "full_access"

    # 完全访问下任意命令都不再询问
    second = api.call("turn/start", {"thread_id": thread_id, "text": "第二次"}, idem="full-t2")
    api.wait_idle(thread_id)
    assert api.state(thread_id).turn(second["turn"]["turn_id"]).status.value == "completed"
    assert len(api.find_events(thread_id, "approval/requested")) == 1


def test_deny_feeds_real_error_back_to_model(api) -> None:
    thread = api.start_thread("拒绝回喂", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="deny-t1")
    turn_id = started["turn"]["turn_id"]
    approval = _wait_approval(api, thread_id, turn_id)

    result = api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "deny",
        },
        idem="deny-1",
    )
    assert result["approval"]["status"] == "denied"
    assert result["approval"]["decision_scope"] == "denied"
    api.wait_idle(thread_id)

    state = api.state(thread_id)
    assert state.turn(turn_id).status.value == "completed", "拒绝后模型应继续给出替代方案"
    call = next(iter(state.tool_calls.values()))
    assert call.status.value == "failed", "被拒绝的调用绝不能显示为成功"
    assert call.error and call.error["code"] == "approval_denied"
    failed_events = api.find_events(thread_id, "tool/failed")
    assert failed_events and failed_events[-1].payload["tool_call"]["error"]["code"] == "approval_denied"
    assert not api.find_events(thread_id, "tool/completed")
    assert "approval_denied" not in json.dumps(api.state(thread_id).thread.settings)


def test_cancel_denies_and_interrupts_turn(api) -> None:
    thread = api.start_thread("审批取消", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="cancel-t1")
    turn_id = started["turn"]["turn_id"]
    approval = _wait_approval(api, thread_id, turn_id)

    api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "cancel",
        },
        idem="cancel-1",
    )
    state = api.state(thread_id)
    assert state.turn(turn_id).status.value == "interrupted"
    assert "turn/completed" not in api.event_types(thread_id)
    assert "turn/interrupted" in api.event_types(thread_id)
    assert state.active_turn is None


def test_unknown_decision_is_rejected(api) -> None:
    thread = api.start_thread("非法决策", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="bad-t1")
    turn_id = started["turn"]["turn_id"]
    approval = _wait_approval(api, thread_id, turn_id)
    error = api.fails(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_forever",
        },
        expect="invalid_params",
        idem="bad-1",
    )
    assert "approve_once" in error["data"]["allowed"]
    api.call("approval/resolve", {"thread_id": thread_id, "turn_id": turn_id, "approval_id": approval.approval_id, "decision": "deny"}, idem="bad-cleanup")
    api.wait_idle(thread_id)


def test_unknown_approval_id(api) -> None:
    thread = api.start_thread("审批不存在", scenario="text_multi_turn")["thread"]
    turn = api.run_turn(thread["thread_id"], "一轮")["turn"]
    api.fails(
        "approval/resolve",
        {
            "thread_id": thread["thread_id"],
            "turn_id": turn["turn_id"],
            "approval_id": "ap_" + "0" * 23,
            "decision": "deny",
        },
        expect="approval_not_found",
        idem="missing-approval",
    )


def test_normalized_prefix_helpers(service) -> None:
    from api.v2.agent.host import normalize_command_prefix, prefix_matches

    assert normalize_command_prefix({"cmd": "ls -la", "cwd": "D:/x", "timeout_s": 5}) == ["cmd=ls -la"]
    assert normalize_command_prefix({"a": 1, "b": 2, "c": 3, "d": 4}) == ["a=1", "b=2", "c=3"]
    assert prefix_matches(["cmd=ls -la"], {"cmd": "ls -la", "cwd": "/other"})
    assert not prefix_matches(["cmd=ls -la"], {"cmd": "rm -rf /"})
    assert prefix_matches([], {"anything": True})

    # 会话规则真的能放行同类调用
    thread = service.repo.create_thread("规则校验")
    call = ToolCall(
        call_id="call_" + "a" * 23,
        name="run_command",
        kind=ToolKind.SIDE_EFFECT,
        status="requested",
        thread_id=thread.thread_id,
        turn_id="tu_" + "b" * 23,
        arguments={"cmd": "python -m pytest -q"},
    )
    assert service.host.approval_gate(call) == "allow", "未声明审批的工具默认放行"

    service.host.bind_scenario(thread.thread_id, parse_lines(
        [json.dumps({"type": "approval_required", "name": "run_command"})],
        name="gate_only",
    ))
    assert service.host.approval_gate(call) == "require"

    service.repo.emit_event(
        thread.thread_id,
        "thread/updated",
        payload={"patch": {"settings": {"permission_rules": [
            {"tool": "run_command", "prefix": ["cmd=python -m pytest -q"], "scope": "conversation"}
        ]}}},
    )
    assert service.host.approval_gate(call) == "allow"
