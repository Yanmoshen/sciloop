# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""MCP server 的行为契约测试。

分两层，故意分开：

1. **不走进程的单元层**（`Guard`）：能力 / 路径 / 审批 / 审计四道门逐条验证，
   跑得快、失败定位准。
2. **走真 stdio 子进程的端到端层**：按协议握手 → 列工具 → 调用工具，
   验证「后端走标准协议调自家工具」这条链路真的通。
   **只有这一层能证明协议接对了** —— 直接 import 函数是证明不了的。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_server.client import call_tool, default_repo_server_dir, list_tools, server_params
from mcp_server.guard import Grant, Guard, GuardError, grant_from_env

REPO_SERVER_DIR = default_repo_server_dir()


# --------------------------------------------------------------------------- #
# 第 1 层：Guard 四道门（不走进程）
# --------------------------------------------------------------------------- #
def test_capability_denied_without_write_grant() -> None:
    guard = Guard(grant=Grant(workspace=None))
    with pytest.raises(GuardError) as exc:
        guard.require("write_file", ("read", "write"))
    assert exc.value.code == "tool_denied"
    assert "写盘权限" in exc.value.message


def test_capability_denied_without_exec_grant() -> None:
    guard = Guard(grant=Grant(allow_exec=False))
    with pytest.raises(GuardError) as exc:
        guard.require("run_command", ("read", "exec"))
    assert exc.value.code == "tool_denied"


def test_read_always_allowed() -> None:
    Guard(grant=Grant()).require("query_library", ("read",))  # 不抛即通过


def test_parent_traversal_is_denied(tmp_path: Path) -> None:
    guard = Guard(grant=Grant(workspace=tmp_path))
    with pytest.raises(GuardError) as exc:
        guard.resolve("run_command", "../../etc/passwd", for_write=False)
    assert exc.value.code == "tool_denied"
    assert exc.value.detail["path"]


def test_absolute_path_outside_workspace_is_denied(tmp_path: Path) -> None:
    guard = Guard(grant=Grant(workspace=tmp_path))
    with pytest.raises(GuardError) as exc:
        guard.resolve("run_command", "/etc/hostname", for_write=False)
    assert exc.value.code == "tool_denied"


def test_credentials_are_never_writable(tmp_path: Path) -> None:
    guard = Guard(grant=Grant(workspace=tmp_path))
    for name in (".env", ".git/config", "cert.pem", "id_rsa"):
        with pytest.raises(GuardError) as exc:
            guard.resolve("write_file", name, for_write=True)
        assert exc.value.code == "tool_denied", name


def test_approval_required_when_no_tokens_configured() -> None:
    guard = Guard(grant=Grant(allow_exec=True))
    with pytest.raises(GuardError) as exc:
        guard.require_approval("run_command", None)
    assert exc.value.code == "approval_required"


def test_approval_missing_vs_invalid_are_distinguishable() -> None:
    guard = Guard(grant=Grant(allow_exec=True, approval_tokens=("researcher-ok",)))
    with pytest.raises(GuardError) as missing:
        guard.require_approval("run_command", None)
    assert missing.value.code == "approval_required"

    with pytest.raises(GuardError) as invalid:
        guard.require_approval("run_command", "guess")
    assert invalid.value.code == "approval_invalid"


def test_approval_passes_with_issued_token() -> None:
    Guard(grant=Grant(allow_exec=True, approval_tokens=("researcher-ok",))).require_approval(
        "run_command", "researcher-ok"
    )


def test_audit_records_denial_without_leaking_params(tmp_path: Path) -> None:
    guard = Guard(grant=Grant(workspace=None))
    with pytest.raises(GuardError):
        guard.require("write_file", ("read", "write"))
    # 拒绝本身没有走 audit()，这里显式验证审计的形状
    guard.audit(
        tool="write_file",
        params={"path": "a.md", "content": "秘密内容"},
        ok=False,
        code="tool_denied",
        ms=1.0,
    )
    assert guard.records[0]["ok"] is False
    assert guard.records[0]["code"] == "tool_denied"
    assert "content" in guard.records[0]["param_keys"]
    assert "秘密内容" not in str(guard.records[0])


def test_grant_from_env_is_the_only_authorization_source(tmp_path: Path) -> None:
    grant = grant_from_env(
        {
            "SCILOOP_MCP_ACTOR": "researcher:42",
            "SCILOOP_MCP_WORKSPACE": str(tmp_path),
            "SCILOOP_MCP_ALLOW_EXEC": "1",
            "SCILOOP_MCP_APPROVAL_TOKENS": "t1, t2",
        }
    )
    assert grant.actor == "researcher:42"
    assert grant.workspace == tmp_path.resolve()
    assert grant.allow_exec is True
    assert grant.approval_tokens == ("t1", "t2")


def test_grant_from_env_defaults_are_conservative() -> None:
    grant = grant_from_env({})
    assert grant.workspace is None, "没给工作区就不许写"
    assert grant.allow_exec is False
    assert grant.allow_exec is False and grant.approval_tokens == ()


# --------------------------------------------------------------------------- #
# 第 2 层：走真 stdio 子进程的端到端
# --------------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)


def test_end_to_end_lists_tools_over_stdio() -> None:
    params = server_params(python=sys.executable, repo_server_dir=REPO_SERVER_DIR)
    tools = _run(list_tools(params))
    names = {item["name"] for item in tools}
    assert {"query_library", "run_command"} <= names, names


def test_end_to_end_run_command_requires_approval(tmp_path: Path) -> None:
    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=True,
        approval_tokens=("researcher-ok",),
    )
    result = _run(call_tool("run_command", {"argv": ["python", "-c", "print(1)"]}, params))
    assert result.ok is False
    assert result.error_code == "approval_required"
    assert result.approval_required is True


def test_end_to_end_run_command_with_approval_really_executes(tmp_path: Path) -> None:
    """带上研究者批准令牌后，命令**真的执行了**。

    断言用纯 ASCII 输出：Windows 控制台默认不是 UTF-8，子进程里 `print('中文')`
    会因编码失败而非零退出，让这条断言变成在测平台编码而不是测我们的逻辑。
    """

    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=True,
        approval_tokens=("researcher-ok",),
    )
    result = _run(
        call_tool(
            "run_command",
            {"argv": ["python", "-c", "print('from-mcp')"], "approval_token": "researcher-ok"},
            params,
        )
    )
    assert result.ok is True, result.raw_error
    assert result.data["exit_code"] == 0
    assert "from-mcp" in result.data["stdout"]


def test_end_to_end_wrong_approval_token_is_invalid(tmp_path: Path) -> None:
    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=True,
        approval_tokens=("researcher-ok",),
    )
    result = _run(
        call_tool(
            "run_command",
            {"argv": ["python", "-c", "print(1)"], "approval_token": "伪造的"},
            params,
        )
    )
    assert result.ok is False
    assert result.error_code == "approval_invalid"


def test_end_to_end_exec_denied_when_not_granted(tmp_path: Path) -> None:
    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=False,
        approval_tokens=("researcher-ok",),
    )
    result = _run(
        call_tool(
            "run_command",
            {"argv": ["python", "-c", "print(1)"], "approval_token": "researcher-ok"},
            params,
        )
    )
    assert result.ok is False
    assert result.error_code == "tool_denied"


def test_end_to_end_binary_allowlist(tmp_path: Path) -> None:
    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=True,
        approval_tokens=("researcher-ok",),
    )
    result = _run(
        call_tool(
            "run_command",
            {"argv": ["curl", "http://example.com"], "approval_token": "researcher-ok"},
            params,
        )
    )
    assert result.ok is False
    assert result.error_code == "tool_denied"


def test_end_to_end_schema_violation_is_rejected_before_our_guard(tmp_path: Path) -> None:
    """`argv` 传成字符串：**协议层的 schema 校验先拦下了**，我们的 guard 根本收不到。

    这是实测出来的真实行为（`Tool 'run_command' rejected arguments: ['argv']`）。
    比我们自己校验更早、更硬，所以这里断言"被拒且没执行"，而不是断言我们的错误码 ——
    断言错误码会变成断言一个**不可达**的分支。
    """

    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=True,
        approval_tokens=("researcher-ok",),
    )
    result = _run(
        call_tool(
            "run_command",
            {"argv": "python -c 'print(1)'", "approval_token": "researcher-ok"},
            params,
        )
    )
    assert result.ok is False
    assert "argv" in result.raw_error
    assert result.error_code is None, "这个拒绝来自协议层，不是我们发的错误码"


def test_end_to_end_nonzero_exit_is_not_a_tool_failure(tmp_path: Path) -> None:
    """命令跑成了、只是返回非零 —— 必须和"工具失败"分开，否则上层会误判。"""

    params = server_params(
        python=sys.executable,
        repo_server_dir=REPO_SERVER_DIR,
        workspace=tmp_path,
        allow_exec=True,
        approval_tokens=("researcher-ok",),
    )
    result = _run(
        call_tool(
            "run_command",
            {
                "argv": ["python", "-c", "raise SystemExit(3)"],
                "approval_token": "researcher-ok",
            },
            params,
        )
    )
    assert result.ok is True
    assert result.data["exit_code"] == 3
    assert result.data["ok"] is False


# --------------------------------------------------------------------------- #
# 联网门（需求追加：允许 fetch / github 这类会出网的工具）
# --------------------------------------------------------------------------- #
def test_net_denied_without_net_grant() -> None:
    guard = Guard(grant=Grant(allow_net=False, allowed_hosts=("example.com",)))
    with pytest.raises(GuardError) as exc:
        guard.assert_host("fetch_url", "example.com")
    assert exc.value.code == "tool_denied"


def test_net_denied_when_allowlist_is_empty() -> None:
    """空白名单 = **一个都不许出**（默认拒绝），不是"不限制"。"""

    guard = Guard(grant=Grant(allow_net=True, allowed_hosts=()))
    with pytest.raises(GuardError) as exc:
        guard.assert_host("fetch_url", "example.com")
    assert exc.value.code == "tool_denied"


def test_net_host_must_be_in_allowlist() -> None:
    guard = Guard(grant=Grant(allow_net=True, allowed_hosts=("arxiv.org",)))
    guard.assert_host("fetch_url", "arxiv.org")  # 命中即通过
    with pytest.raises(GuardError) as exc:
        guard.assert_host("fetch_url", "evil.example")
    assert exc.value.code == "tool_denied"
    assert exc.value.detail["host"] == "evil.example"


def test_net_host_matches_with_port() -> None:
    guard = Guard(grant=Grant(allow_net=True, allowed_hosts=("api.example:8443",)))
    guard.assert_host("fetch_url", "api.example", 8443)


def test_grant_from_env_reads_net_settings() -> None:
    grant = grant_from_env(
        {"SCILOOP_MCP_ALLOW_NET": "1", "SCILOOP_MCP_ALLOWED_HOSTS": "A.org, b.org"}
    )
    assert grant.allow_net is True
    assert grant.allowed_hosts == ("a.org", "b.org")
    assert grant_from_env({}).allow_net is False


def test_server_params_propagates_the_net_grant() -> None:
    """**授权链不能断在中间**：`Guard` 只认 `SCILOOP_MCP_*`，而子进程的环境由
    `server_params()` 构造 —— 漏写一个键，"白名单配了"和"子进程里 allow_net=False"
    就会错位：工具摆得出去、一调必被拒，而且两边单看都"对"。

    这条测试盯的正是那个缝：**从构造参数到子进程 Grant，一路走通**。
    """

    params = server_params(allow_net=True, allowed_hosts=("arxiv.org",))
    grant = grant_from_env(dict(params.env))
    assert grant.allow_net is True
    assert grant.allowed_hosts == ("arxiv.org",)

    # 默认值必须是**默认拒绝**，而不是"不限制"
    conservative = grant_from_env(dict(server_params().env))
    assert conservative.allow_net is False
    assert conservative.allowed_hosts == ()


def test_end_to_end_fetch_url_is_registered() -> None:
    params = server_params(python=sys.executable, repo_server_dir=REPO_SERVER_DIR)
    names = {item["name"] for item in _run(list_tools(params))}
    assert "fetch_url" in names, names
