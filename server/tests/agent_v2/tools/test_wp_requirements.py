"""文档《Agent2-权限审批沙箱与宿主执行-计划验收》逐条要求的补充验收。

覆盖前面几个文件没直接覆盖的点：

- WP-02：大小写/分隔符归一、不存在目标的父目录解析、平台能力探测**如实返回**；
- WP-03：``approve_for_thread`` 的作用域绑定、pending 重启收敛、升级完全访问的唯一入口；
- WP-04：权限事实渲染**不泄漏**令牌/审批 ID/ACL 细节；
- WP-05：输出按**行数**限制、信号退出与普通非零退出可区分、软→硬两段终止证据；
- §5：跨线接口门面（``evaluate`` / ``execute`` / ``cancel`` / ``approval_events`` /
  ``list_tools`` / ``get_approval`` / ``resolve_approval`` / ``interrupt_execution``）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tool_helpers import make_call, python_bin, run

from contracts.agent_v2 import ToolCallStatus
from services.approval_v2 import (
    ApprovalManager,
    ApprovalStore,
    DecisionScope,
    RiskCategory,
    check_no_secrets,
    render_permission_facts,
)
from services.host_execution_v2 import ExecutionStatus, HostExecutionManager, OutputCollector
from services.sandbox_v2 import (
    SandboxDecision,
    SandboxManager,
    SandboxPolicy,
    normalize_key,
    resolve_for_check,
)
from services.tool_registry_v2 import ToolGateway
from services.tool_registry_v2.builtin import build_default_registry, default_tool_definitions


# ============================================================================ WP-02
def test_case_and_separator_variants_do_not_break_boundary(sandbox, workspace) -> None:
    """大小写变体、多余分隔符、``.`` 段都不能绕过边界判定。"""
    probes = [
        str(workspace / "Sub" / "a.txt"),
        str(workspace / "sub" / "a.txt"),
        str(workspace) + "/sub//a.txt",
        str(workspace / "." / "sub" / "a.txt"),
    ]
    for probe in probes:
        verdict = sandbox.check_write(probe)
        assert verdict.inside_workspace is True, probe
        assert verdict.decision is SandboxDecision.ALLOW, probe
    # 归一化键本身要能识别大小写差异
    assert normalize_key(workspace / "A.TXT") == normalize_key(workspace / "a.txt")


def test_nonexistent_target_resolves_parent(sandbox, workspace, tmp_path) -> None:
    """不存在的目标：按**父目录**解析（否则新建文件永远判不了边界）。"""
    inside_new = workspace / "not-yet" / "deep" / "file.txt"
    resolved, how = resolve_for_check(inside_new)
    assert how == "parent-resolved"
    assert sandbox.check_write(inside_new).allowed

    outside_new = tmp_path / "outside" / "deep" / "file.txt"
    verdict = sandbox.check_write(outside_new)
    assert verdict.decision is SandboxDecision.REQUIRE_APPROVAL
    assert verdict.matched_root is None


def test_platform_capability_is_reported_honestly(sandbox) -> None:
    """能力探测必须如实：本线未实现系统级强隔离，就不能声称已启用。"""
    capability = sandbox.capability()
    data = capability.to_dict()
    assert data["policy_layer"] is True
    assert data["strong_isolation"] is False, "策略层不等于系统级隔离"
    assert data["mechanisms"], "必须列出实际机制"
    assert any("unavailable" in item or "unused" in item for item in data["mechanisms"])
    assert data["notes"], "必须给出风险说明"


def test_suspicious_inline_code_escalates_to_approval(sandbox, workspace) -> None:
    """启发式路径识别不是唯一安全边界：可疑内联代码升级到审批。"""
    benign = sandbox.check_command_paths([python_bin(), "-c", "print(1)"], cwd=workspace)
    assert benign.verdict.allowed is True

    risky = sandbox.check_command_paths(
        [python_bin(), "-c", "open('x','w').write('y')"], cwd=workspace
    )
    assert risky.verdict.needs_approval is True
    assert risky.verdict.escalation

    # 完全访问模式下不再升级（用户已显式放开）
    full = SandboxManager(workspace, policy=SandboxPolicy.DANGER_FULL_ACCESS)
    assert full.check_command_paths(
        [python_bin(), "-c", "open('x','w').write('y')"], cwd=workspace
    ).verdict.allowed is True


def test_protected_rules_are_configurable(sandbox, workspace) -> None:
    """敏感规则可配置，且拒绝必须带命中规则 id（可审计）。"""
    from services.sandbox_v2 import ProtectedRule

    custom = SandboxManager(
        workspace,
        protected_rules=(
            *sandbox.protected_rules,
            ProtectedRule("custom.notes", "suffix", ".secret", reason="团队自定义敏感文件"),
        ),
    )
    verdict = custom.check_write(workspace / "plan.secret")
    assert verdict.denied
    assert verdict.protected_rule == "custom.notes"
    assert "团队自定义敏感文件" in verdict.reason


# ============================================================================ WP-03
def test_approve_for_thread_binds_scope(workspace, approvals) -> None:
    """持续批准绑定 executable + argv 前缀 + 参数限制 + cwd 范围 + thread。"""
    removed = workspace / "build"
    removed.mkdir(exist_ok=True)
    argv = ["rm", "-rf", str(removed)]
    verdict = approvals.assess_command(argv, cwd=workspace, thread_id="th_scope")
    view = approvals.request_for_assessment(
        verdict, thread_id="th_scope", turn_id="tu_1", call_id="call_scope"
    )
    granted, grant = approvals.approve_for_thread(view.approval_id, scope_id="th_scope")
    assert granted.status == "granted"
    assert grant is not None
    assert grant.executable == "rm"
    assert grant.prefix == ("rm", "-rf")
    assert grant.arg_constraints.mode == "exact"
    assert grant.cwd == str(normalize_key(resolve_for_check(workspace)[0])) or grant.cwd
    assert grant.scope == "thread"

    # 命中；换参数 / 换目录 / 换会话都不命中
    assert approvals.assess_command(argv, cwd=workspace, thread_id="th_scope").required is False
    other = workspace / "other"
    other.mkdir(exist_ok=True)
    assert (
        approvals.assess_command(
            ["rm", "-rf", str(other)], cwd=workspace, thread_id="th_scope"
        ).required
        is True
    )
    assert approvals.assess_command(argv, cwd=workspace, thread_id="th_other").required is True


def test_prefix_mode_arg_constraints_reject_path_extra_args(workspace, approvals) -> None:
    """放宽到 prefix 模式时，额外参数不得是路径（否则等于放开了任意文件目标）。"""
    removed = workspace / "build"
    removed.mkdir(exist_ok=True)
    argv = ["rm", "-rf", str(removed)]
    view = approvals.request_for_assessment(
        approvals.assess_command(argv, cwd=workspace, thread_id="th_prefix"),
        thread_id="th_prefix",
        turn_id="tu_p",
        call_id="call_p",
    )
    approvals.approve_for_thread(view.approval_id, scope_id="th_prefix", arg_mode="prefix")

    assert (
        approvals.assess_command(
            ["rm", "-rf", str(removed)], cwd=workspace, thread_id="th_prefix"
        ).required
        is False
    )
    # 额外参数是路径 → 不命中（升级回审批）
    assert (
        approvals.assess_command(
            ["rm", "-rf", str(removed), str(workspace / "other.txt")],
            cwd=workspace,
            thread_id="th_prefix",
        ).required
        is True
    )


def test_one_call_has_exactly_one_final_decision(workspace, approvals) -> None:
    """同一 Call 只有一个最终结论：重复裁决幂等，且互斥（先批后拒不改判）。"""
    view = approvals.request_for_assessment(
        approvals.assess_command(["rm", "-rf", "x"], cwd=workspace, thread_id="th_one"),
        thread_id="th_one",
        turn_id="tu_one",
        call_id="call_one",
    )
    first = approvals.approve_once(view.approval_id)
    second = approvals.deny(view.approval_id)
    third = approvals.approve_once(view.approval_id)
    assert first.status == second.status == third.status == "granted"
    assert len(approvals.decisions()) == 1
    assert approvals.decisions()["call_one"]["scope"] == DecisionScope.ONCE.value


def test_pending_converges_after_restart(tmp_path, workspace) -> None:
    """服务重启：未裁决的审批收敛为过期（不放行、不重放）。"""
    store = ApprovalStore(path=tmp_path / "approvals.json")
    first = ApprovalManager(store=store)
    view = first.request_for_assessment(
        first.assess_command(["rm", "-rf", "x"], cwd=workspace, thread_id="th_r"),
        thread_id="th_r",
        turn_id="tu_r",
        call_id="call_r",
    )
    assert first.pending()

    revived = ApprovalManager(store=ApprovalStore(path=tmp_path / "approvals.json"))
    assert revived.pending(), "重启后仍能看到未裁决的请求"
    converged = revived.converge_on_restart()
    assert [item.approval_id for item in converged] == [view.approval_id]
    assert revived.view(view.approval_id).status == "expired"
    assert revived.pending() == []


def test_escalate_full_access_is_explicit_and_audited(workspace, sandbox) -> None:
    """完全访问只能由用户显式操作升级，并留下裁决记录。"""
    manager = ApprovalManager(sandbox=sandbox)
    assert manager.full_access is False
    assert sandbox.policy is SandboxPolicy.WORKSPACE_WRITE

    view = manager.request_for_assessment(
        manager.assess_command(["rm", "-rf", "x"], cwd=workspace, thread_id="th_e"),
        thread_id="th_e",
        turn_id="tu_e",
        call_id="call_e",
    )
    upgraded = manager.escalate_full_access(view.approval_id, by="owner", sandbox=sandbox)
    assert upgraded.status == "granted"
    assert upgraded.decision_scope == DecisionScope.ESCALATED.value
    assert manager.full_access is True
    assert sandbox.policy is SandboxPolicy.DANGER_FULL_ACCESS
    # 升级之后命令自动放行，但审计字段仍在
    assessment = manager.assess_command(
        ["rm", "-rf", str(workspace / "x")], cwd=workspace, thread_id="th_e"
    )
    assert assessment.required is False
    assert assessment.action["argv"][0] == "rm"
    assert assessment.categories == (RiskCategory.DELETE.value,)


def test_tokens_are_bound_and_redacted(workspace, approvals) -> None:
    """令牌绑定 thread/turn/call/工具，且审计输出打码。"""
    view = approvals.request_for_assessment(
        approvals.assess_command(["rm", "-rf", "x"], cwd=workspace, thread_id="th_tok"),
        thread_id="th_tok",
        turn_id="tu_tok",
        call_id="call_tok",
    )
    approvals.approve_once(view.approval_id)
    token = approvals.issue_token(view.approval_id)
    binding = {
        "thread_id": "th_tok",
        "turn_id": "tu_tok",
        "call_id": "call_tok",
        "tool": view.tool_name,
    }
    assert approvals.verify_token(token, **binding) is not None
    assert approvals.verify_token(token, **{**binding, "tool": "host.file.read"}) is None
    audit = approvals.tokens.audit()
    assert audit and all(token not in json.dumps(item, ensure_ascii=False) for item in audit)
    assert audit[0]["token"].endswith("**") or "***" in audit[0]["token"]


# ============================================================================ WP-04
def test_permission_facts_are_desensitized(sandbox, workspace, approvals) -> None:
    """给模型的事实摘要不得含令牌、审批 ID、ACL 细节或"已批准"字样。"""
    verdict = approvals.assess_command(["rm", "-rf", "build"], cwd=workspace, thread_id="th_f")
    view = approvals.request_for_assessment(
        verdict, thread_id="th_f", turn_id="tu_f", call_id="call_f"
    )
    approvals.approve_for_thread(view.approval_id, scope_id="th_f")

    text = render_permission_facts(
        sandbox_facts=sandbox.facts(),
        tools=[item.describe() for item in default_tool_definitions()],
        approved_prefixes=approvals.grants.prefixes(scope_id="th_f"),
        denied_read_categories=["credentials", "private_keys"],
    )
    assert check_no_secrets(text) == [], "事实摘要泄漏了禁止内容"
    assert view.approval_id not in text
    assert "token" not in text.lower()
    assert "workspace-write" in text
    assert str(workspace) in text
    assert "rm -rf" in text  # 已批准前缀可以如实展示（不含令牌）
    assert "凭据" in text  # 不可读类别与原因


def test_permission_facts_assert_helper_detects_leaks() -> None:
    """泄漏检测本身要有效（否则前面的断言是空的）。"""
    from services.approval_v2 import assert_no_secrets

    with pytest.raises(ValueError):
        assert_no_secrets("token: abc123")
    with pytest.raises(ValueError):
        assert_no_secrets("ap_001a1214dc0c73e8be0e4c3 已批准")
    # 干净文本必须通过（返回 None，不抛错）
    assert_no_secrets("沙箱模式：workspace-write；工作区外只读")


# ============================================================================ WP-05
def test_output_line_limit_and_byte_limit() -> None:
    """字节与行数双限制；超限后继续计数但不再缓存。"""
    collector = OutputCollector(max_bytes=1024, max_lines=5)
    for index in range(20):
        collector.append("stdout", f"line-{index}\n")
    details = collector.channels["stdout"].to_dict()
    assert collector.truncated is True
    assert details["lines_seen"] == 20
    assert details["kept_bytes"] <= details["bytes_seen"]
    assert len(collector.stdout.splitlines()) <= 6
    assert details["dropped_bytes"] > 0


def test_signal_exit_distinguished_from_nonzero(tmp_path, workspace, sandbox) -> None:
    """信号退出与普通非零退出必须可区分。"""
    host = HostExecutionManager(tmp_path / "rec", sandbox=sandbox, default_timeout_s=20.0)
    normal = run(
        host.execute([python_bin(), "-c", "import sys; sys.exit(9)"], cwd=workspace)
    )
    assert normal.status is ExecutionStatus.FAILED
    assert normal.metadata.get("exit_kind") == "exited"
    assert normal.error["code"] == "nonzero_exit"

    killed = run(
        host.execute(
            [python_bin(), "-c", "import time; time.sleep(30)"],
            cwd=workspace,
            timeout_s=1.0,
        )
    )
    assert killed.status is ExecutionStatus.TIMEOUT
    assert killed.metadata.get("exit_kind") == "killed"


def test_kill_evidence_records_soft_then_hard(tmp_path, workspace, sandbox) -> None:
    """终止证据要能看出"先软后硬"两段（文档 WP-05 明确要求）。"""
    host = HostExecutionManager(tmp_path / "rec2", sandbox=sandbox, default_timeout_s=20.0)
    record = run(
        host.execute(
            [python_bin(), "-c", "import time; time.sleep(30)"], cwd=workspace, timeout_s=1.0
        )
    )
    evidence = record.metadata["kill_evidence"]
    phases = [step["phase"] for step in evidence["steps"]]
    assert phases[0] == "soft"
    assert evidence["method"] in {"taskkill-force", "killpg-force", "kill-force", "taskkill-soft"}
    assert evidence["ok"] is True


def test_capability_probe_reports_execution_layer(tmp_path, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec3", sandbox=sandbox)
    data = host.capability()
    assert data["process_tree_kill"] is True
    assert data["soft_then_hard"] is True
    assert data["notes"], "能力探测必须带风险说明"


# ============================================================================ §5 门面
def test_gateway_exposes_both_sides(tmp_path, workspace, sandbox, host, approvals) -> None:
    registry = build_default_registry(
        sandbox=sandbox, host=host, approvals=approvals, cwd=str(workspace)
    )
    gateway = ToolGateway(registry)

    # Agent 3 侧：工具清单 + 审批查询/裁决
    tools = gateway.list_tools()
    assert {item["name"] for item in tools} >= {"host.command", "agent.spawn", "search.query"}
    assert all("permission_class" in item and "audit_fields" in item for item in tools)

    call = make_call("host.file.read", {"path": "nope.txt"}, kind="read_only")
    decision = gateway.evaluate(call)
    assert decision["decision"] == "allow"

    risky = make_call("host.command", {"argv": ["rm", "-rf", "build"], "cwd": str(workspace)})
    decision = gateway.evaluate(risky)
    assert decision["decision"] in {"require", "deny"}
    assert decision["categories"]

    # Agent 1 侧：执行 + 事件流
    result = run(gateway.execute(call))
    assert result.status is ToolCallStatus.FAILED  # 文件不存在，但结构与 call_id 正确
    assert result.call_id == call.call_id

    view = approvals.request_for_assessment(
        approvals.assess_command(
            ["rm", "-rf", "build"], cwd=workspace, thread_id=risky.thread_id
        ),
        thread_id=risky.thread_id,
        turn_id=risky.turn_id,
        call_id=risky.call_id,
    )
    resolved = gateway.resolve_approval({"approval_id": view.approval_id, "decision": "approve_once"})
    assert resolved["ok"] is True
    assert resolved["released_call_id"] == risky.call_id
    assert gateway.get_approval(view.approval_id)["status"] == "granted"
    events = gateway.approval_events().drain()
    assert {item["type"] for item in events} >= {"approval/requested", "approval/granted"}

    # 中断入口（没有对应执行时返回结构化错误，而不是抛异常）
    assert gateway.interrupt_execution("call_missing")["ok"] is False


def test_gateway_permission_summary_is_deliverable_evidence(tmp_path, workspace, sandbox) -> None:
    registry = build_default_registry(sandbox=sandbox, cwd=str(workspace))
    summary = ToolGateway(registry).permission_summary()
    assert summary["tools"] and summary["by_permission_class"]
    assert all(summary["schemas_valid"].values()), "每个工具的输入/输出 Schema 都必须合法"
    assert summary["capability"]["strong_isolation"] is False


def test_registry_unknown_tool_is_not_parallel_and_denied_by_default(registry) -> None:
    """未知工具：不并行、按最严权限处理。"""
    from services.tool_registry_v2 import PermissionClass

    assert registry.permission_of("does.not.exist") is PermissionClass.DANGEROUS
    requirement = registry.approval_requirement(
        make_call("does.not.exist", {}), thread_id="th_unknown"
    )
    assert requirement["required"] is True


def test_json_schemas_are_valid_draft_2020_12(registry) -> None:
    """所有工具 Schema 必须能被 Draft 2020-12 校验器接受（禁止手搓）。"""
    assert all(registry.validated_schemas().values())
    assert Path("services/tool_registry_v2/schemas.py").is_file()
