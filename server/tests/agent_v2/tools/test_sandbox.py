"""沙箱验收（对应验收书 §4）。

- 普通模式：工作区内可读写执行；工作区外只读可用、写入要求授权；
- **命令参数里的越界路径被拦截**（不能只检查 cwd）；
- 符号链接不能绕过根边界；
- 完全访问可访问任意目录并自动批准命令，但仍保留超时/取消/输出上限与审计。
"""

from __future__ import annotations

from tool_helpers import make_call, run

from services.sandbox_v2 import (
    AccessKind,
    SandboxDecision,
    SandboxPolicy,
    extract_argv_paths,
    is_protected,
    is_within,
    normalize,
)
from services.tool_registry_v2 import ToolRegistry
from services.tool_registry_v2.builtin import default_tool_definitions


# ---------------------------------------------------------------------------- §4.1
def test_inside_workspace_read_write_execute_allowed(sandbox, workspace) -> None:
    assert sandbox.check_path(workspace / "a.txt", AccessKind.READ).allowed
    assert sandbox.check_path(workspace / "a.txt", AccessKind.WRITE).allowed
    assert sandbox.check_path(workspace / "sub" / "b.txt", AccessKind.WRITE).allowed
    assert sandbox.check_path(workspace, AccessKind.EXECUTE).allowed


def test_outside_workspace_read_allowed_write_needs_approval(sandbox, workspace, tmp_path) -> None:
    outside = tmp_path / "outside.txt"
    read = sandbox.check_path(outside, AccessKind.READ)
    write = sandbox.check_path(outside, AccessKind.WRITE)
    assert read.allowed and not read.inside_workspace
    assert write.decision is SandboxDecision.REQUIRE_APPROVAL
    assert write.needs_approval


def test_read_only_policy_denies_writes(sandbox, workspace, tmp_path) -> None:
    sandbox.set_policy(SandboxPolicy.READ_ONLY)
    assert sandbox.check_path(workspace / "a.txt", AccessKind.WRITE).denied
    assert sandbox.check_path(tmp_path / "x.txt", AccessKind.WRITE).denied
    assert sandbox.check_path(workspace / "a.txt", AccessKind.READ).allowed


# ---------------------------------------------------------------------------- §4.2
def test_argv_paths_are_checked_not_only_cwd(sandbox, workspace, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    # 写命令的参数就是写入目标 → 越界必须要求授权
    check = sandbox.check_argv(["mkdir", str(outside / "x")], cwd=workspace)
    assert check.verdict.needs_approval, "命令参数里的越界路径必须被拦截"
    assert any(item.path == str(normalize(outside / "x")) for item in check.paths)

    # 工作区内的写命令 → 放行
    inside = sandbox.check_argv(["mkdir", str(workspace / "ok")], cwd=workspace)
    assert inside.verdict.allowed

    # 只读命令 + 越界参数 → 按读取，放行
    read = sandbox.check_argv(["cat", str(outside / "x")], cwd=workspace)
    assert read.verdict.allowed


def test_redirect_targets_are_treated_as_writes(sandbox, workspace, tmp_path) -> None:
    check = sandbox.check_argv(["echo", "hi", ">", str(tmp_path / "out.txt")], cwd=workspace)
    assert check.verdict.needs_approval


def test_argv_paths_extraction_covers_flags_and_relative() -> None:
    found = extract_argv_paths(["x.py", "--out=/tmp/a.txt", "relative.md", "plain"], cwd="/tmp")
    tokens = [token for token, _ in found]
    assert "x.py" in tokens and "--out=/tmp/a.txt" not in tokens
    assert "/tmp/a.txt" in tokens and "relative.md" in tokens
    assert "plain" not in tokens


def test_sensitive_targets_are_protected(sandbox, workspace) -> None:
    assert is_protected(workspace / ".env")
    assert is_protected(workspace / ".git" / "config")
    assert is_protected(workspace / "keys" / "server.pem")
    assert not is_protected(workspace / "notes.md")

    write_env = sandbox.check_path(workspace / ".env", AccessKind.WRITE)
    read_env = sandbox.check_path(workspace / ".env", AccessKind.READ)
    assert write_env.denied, "凭据文件必须写保护（即使在工作区内）"
    assert read_env.needs_approval
    assert sandbox.check_path(workspace / ".git" / "config", AccessKind.WRITE).denied


# ---------------------------------------------------------------------------- §4.3
def test_symlink_cannot_escape_workspace_root(sandbox, workspace, tmp_path) -> None:
    secret_dir = tmp_path / "outside-dir"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("top secret", encoding="utf-8")
    link = workspace / "escape"
    link.symlink_to(secret_dir, target_is_directory=True)

    verdict = sandbox.check_path(link / "secret.txt", AccessKind.WRITE)
    assert verdict.needs_approval, "软链解析后的真实位置在工作区外 → 必须要求授权"
    assert verdict.inside_workspace is False
    assert verdict.path == str(normalize(secret_dir / "secret.txt"))
    assert is_within(link / "secret.txt", workspace) is False


# ---------------------------------------------------------------------------- §4.4
def test_full_access_allows_any_directory_and_audits(full_access_sandbox, workspace, tmp_path) -> None:
    outside = tmp_path / "anywhere.txt"
    verdict = full_access_sandbox.check_path(outside, AccessKind.WRITE)
    assert verdict.allowed
    assert verdict.audited is True, "完全访问仍要记账（审计不降级）"

    protected = full_access_sandbox.check_path(workspace / ".env", AccessKind.WRITE)
    assert protected.allowed
    assert protected.protected is True and protected.audited is True


def test_full_access_keeps_limits_and_cancellation(tmp_path, workspace, full_access_sandbox) -> None:
    from tool_helpers import python_bin

    from services.host_execution_v2 import HostExecutionManager

    host = HostExecutionManager(
        tmp_path / "rec-full", sandbox=full_access_sandbox, default_timeout_s=30.0
    )
    registry = ToolRegistry(
        sandbox=full_access_sandbox,
        host=host,
        approvals=None,
        default_cwd=str(workspace),
        max_output_bytes=150,
    )
    registry.register_all(default_tool_definitions())

    # 超时上限仍然生效
    timed = run(
        registry.execute(
            make_call(
                "host.exec",
                {
                    "argv": [python_bin(), "-c", "import time; time.sleep(20)"],
                    "cwd": str(workspace),
                    "timeout_s": 1.0,
                },
            )
        )
    )
    assert timed.status.value in {"timeout", "failed"}
    assert timed.output and timed.output.get("exit_code") is not None

    # 输出上限仍然生效
    noisy = run(
        registry.execute(
            make_call(
                "host.exec",
                {"argv": [python_bin(), "-c", "print('y' * 2000)"], "cwd": str(workspace)},
            )
        )
    )
    assert noisy.output and noisy.output.get("truncated") is True

    # 审计信息保留：执行记录里带策略与终止证据
    records = host.recent(limit=5)
    assert records
    assert all(record.policy == "danger-full-access" for record in records)
    assert all(record.execution_id.startswith("ex_") for record in records)
