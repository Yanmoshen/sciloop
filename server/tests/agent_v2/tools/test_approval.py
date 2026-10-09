"""审批验收（对应验收书 §3）。

- 高危命令生成**完整**审批请求并暂停 Turn；
- 批准一次只释放当前 Call；持续批准保存前缀 + cwd + 会话作用域；
- 参数 / 目录 / 可执行文件变化不会误匹配；
- 拒绝结果回传模型且工具**没有实际副作用**；
- 中断与过期会收敛未完成审批，重复批准无副作用；
- 模型看不到审批令牌；
- 全程不需要 Agent 3 前端。
"""

from __future__ import annotations

from tool_helpers import make_call, python_bin, run

from contracts.agent_v2 import ApprovalStatus, TurnStatus, tool_call_response
from contracts.agent_v2.fake import FakeProvider
from services.agent_runtime_v2 import ToolScheduler, TurnRuntime
from services.agent_threads_v2 import ThreadRepository
from services.approval_v2 import ApprovalManager
from services.tool_registry_v2 import ToolRegistry
from services.tool_registry_v2.builtin import default_tool_definitions


# ---------------------------------------------------------------------------- 夹具
def build_runtime(tmp_path, registry, *, gate, script):
    """用 Agent 1 的运行时驱动我的工具线（不依赖 Agent 3，也不需要真模型）。"""
    repo = ThreadRepository(tmp_path / "conversations")
    thread = repo.create_thread("审批测试")
    runtime = TurnRuntime(
        repo=repo,
        gateway=FakeProvider(script=script),
        tools=ToolScheduler(registry),
        approval_gate=gate,
    )
    return repo, thread, runtime


def risk_command_call(workspace, call_id):
    victim = workspace / "victim.txt"
    victim.write_text("important", encoding="utf-8")
    return (
        victim,
        tool_call_response(
            "host.exec",
            {"argv": ["rm", "-rf", str(victim)], "cwd": str(workspace)},
            call_id=call_id,
        ),
    )


# ---------------------------------------------------------------------------- §3.1
def test_high_risk_command_pauses_turn_with_full_request(tmp_path, workspace, registry, approvals):
    from contracts.agent_v2.ids import new_id

    call_id = new_id("call")
    victim, script = risk_command_call(workspace, call_id)
    gate = registry.approval_gate("placeholder")
    repo, thread, runtime = build_runtime(tmp_path, registry, gate=gate, script=[script])
    runtime.approval_gate = registry.approval_gate(thread.thread_id)

    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "删掉它"}])
    outcome = run(runtime.run(thread.thread_id, turn.turn_id))

    assert outcome.status is TurnStatus.WAITING_APPROVAL, "高危命令必须暂停 Turn"
    assert outcome.suspended is True
    status = repo.state(thread.thread_id)
    pending = [item for item in status.approvals.values() if item.status is ApprovalStatus.PENDING]
    assert len(pending) == 1
    request = pending[0]
    assert request.call_id == call_id
    assert request.thread_id == thread.thread_id
    assert request.turn_id == turn.turn_id
    assert request.action["tool"] == "host.exec"
    assert request.action["arguments"]["argv"][0] == "rm"
    assert request.action["arguments"]["cwd"] == str(workspace)
    assert victim.exists(), "尚未批准，文件不能被删除"

    # 我的判定层给出的是"完整审批请求依据"（命令/参数/cwd/目标/风险类别/来源）
    call = make_call(
        "host.exec",
        {"argv": ["rm", "-rf", str(victim)], "cwd": str(workspace)},
        call_id=call_id,
        thread_id=thread.thread_id,
        turn_id=turn.turn_id,
    )
    verdict = registry.approval_requirement(call, thread_id=thread.thread_id)
    assert verdict["required"] is True
    assert "delete" in verdict["categories"]
    assert verdict["action"]["targets"] is not None


def test_approved_once_releases_current_call(tmp_path, workspace, registry):
    from contracts.agent_v2.ids import new_id

    call_id = new_id("call")
    victim, script = risk_command_call(workspace, call_id)
    repo, thread, runtime = build_runtime(tmp_path, registry, gate=None, script=[script])
    runtime.approval_gate = registry.approval_gate(thread.thread_id)
    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "删掉它"}])

    outcome = run(runtime.run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.WAITING_APPROVAL
    approval_id = next(iter(repo.state(thread.thread_id).approvals))
    repo.resolve_approval(
        thread.thread_id, turn.turn_id, approval_id, granted=True, scope="once"
    )
    # 批准后放行该 Call：先接管 Agent 1 落盘的那条审批，再按「批准一次」处理
    assert registry.approvals is not None
    registry.approvals.adopt(repo.state(thread.thread_id).approvals[approval_id])
    registry.approvals.grant_once(approval_id)
    assert registry.approvals.is_released(call_id) is True
    assert registry.approvals.grants.list(scope_id=thread.thread_id) == [], (
        "「批准一次」不得写入持续批准规则"
    )


def test_denied_command_leaves_no_side_effect(tmp_path, workspace, registry):
    from contracts.agent_v2.ids import new_id

    call_id = new_id("call")
    victim, script = risk_command_call(workspace, call_id)
    repo, thread, runtime = build_runtime(tmp_path, registry, gate=None, script=[script])
    runtime.approval_gate = registry.approval_gate(thread.thread_id)
    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "删掉它"}])
    run(runtime.run(thread.thread_id, turn.turn_id))

    approval_id = next(iter(repo.state(thread.thread_id).approvals))
    repo.resolve_approval(
        thread.thread_id, turn.turn_id, approval_id, granted=False, scope="denied"
    )
    resumed = run(runtime.run(thread.thread_id, turn.turn_id))

    assert victim.exists(), "拒绝之后工具不能执行（无实际副作用）"
    approval = repo.state(thread.thread_id).approvals[approval_id]
    assert approval.status is ApprovalStatus.DENIED
    assert resumed.status in (TurnStatus.WAITING_APPROVAL, TurnStatus.COMPLETED)


# ---------------------------------------------------------------------------- §3.2 持续批准
def test_always_allow_saves_prefix_cwd_and_scope(workspace, approvals):
    thread_id = "th_scope"
    argv = [python_bin(), "-c", "print(1)"]
    assessment = approvals.assess_command(argv, cwd=workspace, thread_id=thread_id)
    request = approvals.request_for_assessment(
        assessment, thread_id=thread_id, turn_id="tu_1", call_id="call_a"
    )
    approval, grant = approvals.always_allow(request.approval_id, scope_id=thread_id)

    assert approval.status is ApprovalStatus.GRANTED
    assert grant is not None
    assert grant.normalized_argv[-1] == "print(1)"
    assert grant.cwd == str(workspace)
    assert grant.scope == "thread" and grant.scope_id == thread_id

    # 同一条命令再次判定 → 命中持续批准，不再打扰
    again = approvals.assess_command(argv, cwd=workspace, thread_id=thread_id)
    assert again.required is False
    assert again.matched_grant is not None


def test_grant_does_not_match_changed_args_cwd_or_executable(workspace, approvals):
    """持续批准只在「同一条高危命令 + 同一目录 + 同一会话」下命中。"""
    thread_id = "th_scope2"
    victim = workspace / "build"
    victim.mkdir(exist_ok=True)
    argv = ["rm", "-rf", str(victim)]
    request = approvals.request_for_assessment(
        approvals.assess_command(argv, cwd=workspace, thread_id=thread_id),
        thread_id=thread_id,
        turn_id="tu_1",
        call_id="call_b",
    )
    approval, grant = approvals.always_allow(request.approval_id, scope_id=thread_id)
    assert grant is not None

    # 同一条命令 → 命中，不再打扰
    assert approvals.assess_command(argv, cwd=workspace, thread_id=thread_id).required is False

    # 参数变化
    other = workspace / "other"
    other.mkdir(exist_ok=True)
    assert approvals.assess_command(
        ["rm", "-rf", str(other)], cwd=workspace, thread_id=thread_id
    ).required is True

    # 目录变化
    elsewhere = workspace.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    assert approvals.assess_command(argv, cwd=elsewhere, thread_id=thread_id).required is True

    # 可执行文件变化
    assert approvals.assess_command(
        ["rmdir", "-rf", str(victim)], cwd=workspace, thread_id=thread_id
    ).required is True

    # 会话作用域变化
    assert approvals.assess_command(argv, cwd=workspace, thread_id="th_other").required is True


def test_repeated_decisions_are_idempotent(workspace, approvals):
    request = approvals.request_for_assessment(
        approvals.assess_command(["rm", "-rf", "build"], cwd=workspace, thread_id="th_x"),
        thread_id="th_x",
        turn_id="tu_x",
        call_id="call_x",
    )
    first = approvals.grant_once(request.approval_id)
    second = approvals.grant_once(request.approval_id)
    assert first.status is ApprovalStatus.GRANTED
    assert second.decided_at == first.decided_at, "重复批改不得产生第二次裁决"
    assert len(approvals.decisions()) == 1


def test_same_call_id_reuses_single_request(workspace, approvals):
    assessment = approvals.assess_command(["rm", "-rf", "x"], cwd=workspace, thread_id="th_y")
    first = approvals.request_for_assessment(
        assessment, thread_id="th_y", turn_id="tu_y", call_id="call_y"
    )
    second = approvals.request_for_assessment(
        assessment, thread_id="th_y", turn_id="tu_y", call_id="call_y"
    )
    assert first.approval_id == second.approval_id
    assert len(approvals.pending()) == 1


# ---------------------------------------------------------------------------- §3.3 取消 / 过期
def test_interrupt_and_expiry_resolve_pending(workspace, approvals, fake_clock):
    from services.approval_v2 import ApprovalManager

    manager = ApprovalManager(clock=fake_clock, ttl_s=10.0)
    assessment = manager.assess_command(["rm", "-rf", "x"], cwd=workspace, thread_id="th_z")
    first = manager.request_for_assessment(
        assessment, thread_id="th_z", turn_id="tu_z", call_id="call_z1"
    )
    second = manager.request_for_assessment(
        assessment, thread_id="th_z", turn_id="tu_z", call_id="call_z2"
    )

    resolved = manager.resolve_on_interrupt("th_z", "tu_z")
    assert len(resolved) == 2, "同一 Turn 下所有未完成审批都要被收敛"
    assert manager.get(first.approval_id).status is ApprovalStatus.DENIED
    assert manager.get(first.approval_id).decision_scope.startswith("cancelled:")
    assert manager.get(second.approval_id).status is ApprovalStatus.DENIED

    # 另开一条用于验证过期
    third = manager.request_for_assessment(
        assessment, thread_id="th_z2", turn_id="tu_z2", call_id="call_z3"
    )
    fake_clock.advance(30)
    expired = manager.expire_stale()
    assert [item.approval_id for item in expired] == [third.approval_id]
    assert manager.get(third.approval_id).status is ApprovalStatus.EXPIRED
    assert manager.pending() == []


# ---------------------------------------------------------------------------- §3.4 令牌
def test_token_never_reaches_model_visible_payload(workspace, approvals, registry):
    request = approvals.request_for_assessment(
        approvals.assess_command(["rm", "-rf", "secret"], cwd=workspace, thread_id="th_t"),
        thread_id="th_t",
        turn_id="tu_t",
        call_id="call_t",
    )
    token = approvals.issue_token(request.approval_id)
    approvals.grant_once(request.approval_id)

    # 审批对象本身不含令牌
    assert token not in str(request.to_dict())
    assert "token" not in request.to_dict()
    # 核销令牌后拿到的是同一条审批
    assert approvals.consume_token(token).approval_id == request.approval_id
    # 令牌不能重复使用
    assert approvals.consume_token(token) is None

    # 工具结果（模型可见面）也不含令牌
    result = run(registry.execute(make_call("host.file.read", {"path": "nope.txt"})))
    assert token not in str(result.output or {})
    assert token not in str(result.error or {})
    assert approvals.token_count() == 0


# ---------------------------------------------------------------------------- §3.5 完全访问
def test_full_access_auto_approves_commands_but_keeps_audit(workspace):
    manager = ApprovalManager(full_access=True)
    assessment = manager.assess_command(
        ["rm", "-rf", str(workspace / "x")], cwd=workspace, thread_id="th_full"
    )
    assert assessment.required is False
    assert assessment.auto_approved is True
    assert "完全访问" in assessment.summary
    # 审计信息仍在（命令/参数/cwd/风险类别）
    assert assessment.action["argv"][0] == "rm"
    assert assessment.categories == ("delete",)
    assert assessment.action["cwd"] == str(workspace)

    manager.set_full_access(False)
    assert manager.assess_command(
        ["rm", "-rf", str(workspace / "x")], cwd=workspace, thread_id="th_full"
    ).required is True


def test_registry_keeps_readonly_auto_when_full_access(full_access_sandbox, workspace, tmp_path):
    from services.host_execution_v2 import HostExecutionManager

    host = HostExecutionManager(tmp_path / "rec2", sandbox=full_access_sandbox)
    manager = ApprovalManager(full_access=True)
    registry = ToolRegistry(
        sandbox=full_access_sandbox, host=host, approvals=manager, default_cwd=str(workspace)
    )
    registry.register_all(default_tool_definitions())

    # 只读工具从不打扰
    assert registry.approval_requirement(
        make_call("host.file.read", {"path": "a.txt"}), thread_id="th_f"
    )["required"] is False
    # 完全访问下命令自动批准
    assert registry.approval_requirement(
        make_call("host.exec", {"argv": ["rm", "-rf", "x"], "cwd": str(workspace)}),
        thread_id="th_f",
    )["required"] is False
    # 但删除类文件工具仍然自动批准——审计信息必须保留
    verdict = registry.approval_requirement(
        make_call("host.file.delete", {"path": str(workspace / "a.txt")}), thread_id="th_f"
    )
    assert verdict["required"] is False
    assert "完全访问" in verdict["reason"] or "自动批准" in verdict["reason"]
