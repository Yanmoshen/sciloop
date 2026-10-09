"""宿主执行验收（对应验收书 §5）。

- argv 直启，不经 shell 拼接；
- stdout/stderr 可增量记录并正确截断；
- 超时 / Turn 中断都会终止**进程树**（用"孙进程心跳停止"作为可复核证据）；
- 服务重启不会重复执行未确认完成的 Call；
- 相同 ``(thread_id, turn_id, call_id)`` 幂等；
- 非零退出码是结构化结果，不伪装成系统异常。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from tool_helpers import python_bin, run, spawn_tree_script, write_heartbeat_script

from contracts.agent_v2.cancellation import CancelToken
from services.host_execution_v2 import (
    ExecutionStatus,
    HostExecutionManager,
    is_windows,
    pid_alive,
)


# ---------------------------------------------------------------------------- §5.1
def test_argv_is_not_reassembled_through_shell(tmp_path, workspace, sandbox) -> None:
    """含空格与 shell 元字符的参数必须原样传给进程（argv 保真）。"""
    host = HostExecutionManager(tmp_path / "rec", sandbox=sandbox, default_timeout_s=20.0)
    script = "import sys, json; print(json.dumps(sys.argv[1:]))"
    args = ["a b", "c;d", "e&&f", "$HOME", "*"]
    record = run(
        host.execute(
            [python_bin(), "-c", script, *args],
            cwd=workspace,
            thread_id="th_argv",
            turn_id="tu_argv",
            call_id="call_argv",
        )
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    import json

    assert json.loads(record.stdout.strip().splitlines()[-1]) == args
    assert record.metadata["sandbox"]["verdict"]["decision"] in {"allow", "require_approval"}


# ---------------------------------------------------------------------------- §5.2
def test_incremental_output_and_truncation(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(
        tmp_path / "rec2", sandbox=sandbox, max_output_bytes=120, default_timeout_s=20.0
    )
    chunks: list[tuple[str, str]] = []
    record = run(
        host.execute(
            [
                python_bin(),
                "-c",
                "import sys;print('out-1');print('err-1', file=sys.stderr);print('x'*4000)",
            ],
            cwd=workspace,
            on_output=lambda channel, text: chunks.append((channel, text)),
        )
    )
    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.truncated is True, "超过上限的输出必须被截断并标记"
    assert record.stdout_bytes > len(record.stdout)
    assert {channel for channel, _ in chunks} == {"stdout", "stderr"}, "两个通道都要增量回调"
    assert any("out-1" in text for _, text in chunks)


# ---------------------------------------------------------------------------- §5.3
def _tree_command(tmp_path: Path, workspace: Path) -> tuple[list[str], Path, Path]:
    """构造「父进程 → 孙进程心跳」的命令；返回 (argv, 心跳文件, 孙进程 pid 文件)。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    heartbeat = write_heartbeat_script(scripts)
    parent = spawn_tree_script(scripts)
    heart = workspace / "hb.txt"
    argv = [python_bin(), str(parent), str(heart), "0.15", str(heartbeat)]
    return argv, heart, Path(f"{heart}.childpid")


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_timeout_terminates_process_tree(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec3", sandbox=sandbox, default_timeout_s=30.0)
    argv, heart, childpid_file = _tree_command(tmp_path, workspace)
    record = run(
        host.execute(argv, cwd=workspace, timeout_s=2.0, thread_id="th_t", turn_id="tu_t", call_id="call_t")
    )
    assert record.status is ExecutionStatus.TIMEOUT
    assert record.killed_reason == "timeout"
    assert record.metadata["kill_evidence"]["method"] in {"taskkill", "killpg", "kill"}

    assert _wait_until(childpid_file.exists, 3.0), "孙进程没有按时启动，测试无效"
    child_pid = int(childpid_file.read_text(encoding="utf-8"))
    assert pid_alive(child_pid) is False, "超时必须终止整棵进程树（含孙进程）"

    # 心跳必须停止
    assert heart.exists()
    stamp = heart.stat().st_mtime_ns
    time.sleep(1.0)
    assert heart.stat().st_mtime_ns == stamp, "孙进程还在写心跳 → 进程树没被真正终止"


def test_turn_interrupt_terminates_process_tree(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec4", sandbox=sandbox, default_timeout_s=60.0)
    argv, heart, childpid_file = _tree_command(tmp_path, workspace)
    token = CancelToken()

    async def scenario() -> None:
        import asyncio

        task = asyncio.ensure_future(
            host.execute(
                argv, cwd=workspace, thread_id="th_i", turn_id="tu_i", call_id="call_i", cancel=token
            )
        )
        await asyncio.sleep(1.5)
        killed = host.interrupt_turn("th_i", "tu_i", reason="user_interrupted")
        record = await task
        assert killed, "Turn 中断必须找到并终止未完成执行"
        assert record.status in (ExecutionStatus.INTERRUPTED, ExecutionStatus.CANCELLED)

    run(scenario())

    assert _wait_until(childpid_file.exists, 3.0)
    child_pid = int(childpid_file.read_text(encoding="utf-8"))
    assert pid_alive(child_pid) is False
    stamp = heart.stat().st_mtime_ns
    time.sleep(1.0)
    assert heart.stat().st_mtime_ns == stamp


def test_token_cancel_terminates_process_tree(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec5", sandbox=sandbox, default_timeout_s=60.0)
    argv, heart, childpid_file = _tree_command(tmp_path, workspace)
    token = CancelToken()

    async def scenario() -> None:
        import asyncio

        task = asyncio.ensure_future(
            host.execute(argv, cwd=workspace, thread_id="th_c", turn_id="tu_c", call_id="call_c", cancel=token)
        )
        await asyncio.sleep(1.2)
        token.cancel("user_cancelled")
        record = await task
        assert record.status is ExecutionStatus.CANCELLED
        assert record.killed_reason == "user_cancelled"

    run(scenario())
    assert _wait_until(childpid_file.exists, 3.0)
    assert pid_alive(int(childpid_file.read_text(encoding="utf-8"))) is False


# ---------------------------------------------------------------------------- §5.4
def test_same_call_is_idempotent(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec6", sandbox=sandbox, default_timeout_s=20.0)
    marker = workspace / "runs.txt"
    command = [
        python_bin(),
        "-c",
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.write_text((p.read_text() if p.exists() else '') + 'x'); print('ran')",
        str(marker),
    ]
    key = {"thread_id": "th_d", "turn_id": "tu_d", "call_id": "call_d"}
    first = run(host.execute(command, cwd=workspace, **key))
    second = run(host.execute(command, cwd=workspace, **key))

    assert first.execution_id == second.execution_id, "重复请求必须复用同一条执行记录"
    assert marker.read_text(encoding="utf-8") == "x", "重复请求不得真的执行第二次"
    assert second.metadata.get("duplicates") == 1


def test_nonzero_exit_is_structured_not_exception(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec7", sandbox=sandbox, default_timeout_s=20.0)
    record = run(
        host.execute(
            [python_bin(), "-c", "import sys;sys.stderr.write('boom');sys.exit(7)"],
            cwd=workspace,
        )
    )
    assert record.status is ExecutionStatus.FAILED
    assert record.exit_code == 7
    assert record.error and record.error["code"] == "nonzero_exit"
    assert "boom" in record.stderr
    assert record.to_tool_output()["exit_code"] == 7


# ---------------------------------------------------------------------------- §5.5
def test_restart_scan_marks_unknown_and_forbids_replay(tmp_path, workspace, sandbox) -> None:
    records_dir = tmp_path / "rec8"
    host = HostExecutionManager(records_dir, sandbox=sandbox, default_timeout_s=20.0)
    key = {"thread_id": "th_r", "turn_id": "tu_r", "call_id": "call_r"}
    record = run(host.execute([python_bin(), "-c", "print('done')"], cwd=workspace, **key))
    assert record.status is ExecutionStatus.SUCCEEDED

    # 伪造一条"服务重启时仍未确认完成"的记录（直接改盘上文件）
    path = records_dir / f"{record.execution_id}.exec.json"
    payload = path.read_text(encoding="utf-8").replace('"succeeded"', '"running"')
    path.write_text(payload, encoding="utf-8")

    revived = HostExecutionManager(records_dir, sandbox=sandbox)
    unresolved = revived.scan_orphans()
    assert [item.execution_id for item in unresolved] == [record.execution_id]
    scanned = revived.get(record.execution_id)
    assert scanned.status is ExecutionStatus.UNKNOWN
    assert scanned.error["code"] == "unknown_after_restart"

    # 禁止盲目重放：再次提交同 key 不会真的执行
    again = run(
        revived.execute(
            [python_bin(), "-c", "print('SHOULD-NOT-RUN-AGAIN')"], cwd=workspace, **key
        )
    )
    assert again.execution_id == record.execution_id
    assert again.status is ExecutionStatus.UNKNOWN


def test_pending_records_become_interrupted_after_restart(tmp_path, workspace, sandbox) -> None:
    records_dir = tmp_path / "rec9"
    host = HostExecutionManager(records_dir, sandbox=sandbox)
    key = {"thread_id": "th_p", "turn_id": "tu_p", "call_id": "call_p"}
    record = run(host.execute([python_bin(), "-c", "print(1)"], cwd=workspace, **key))

    path = records_dir / f"{record.execution_id}.exec.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"succeeded"', '"pending"'), encoding="utf-8"
    )
    revived = HostExecutionManager(records_dir, sandbox=sandbox)
    unresolved = revived.scan_orphans()
    assert revived.get(record.execution_id).status is ExecutionStatus.INTERRUPTED
    assert revived.get(record.execution_id).error["code"] == "interrupted"
    assert len(unresolved) == 1


def test_sandbox_denied_command_never_spawns(tmp_path, workspace, sandbox) -> None:
    """沙箱**硬拒**（凭据/版本库元数据）时，进程连启动都不该启动。

    ⚠️ 只到"需要授权"（工作区外写入）的命令不在这里拦——那一步由工具层的审批门负责，
    执行管理器只硬拒 `deny`。
    """
    protected = workspace / ".git" / "hooks"
    host = HostExecutionManager(tmp_path / "rec10", sandbox=sandbox, default_timeout_s=10.0)
    record = run(host.execute(["mkdir", str(protected)], cwd=workspace))
    assert record.status is ExecutionStatus.FAILED
    assert record.error["code"] == "sandbox_denied"
    assert not protected.exists()
    assert record.pid is None, "被沙箱拦下的命令不得真的启动进程"

    # 工作区外写入是"需要授权"，不是硬拒 → 交给审批门
    outside = tmp_path / "needs-approval"
    check = sandbox.check_argv(["mkdir", str(outside)], cwd=workspace)
    assert check.verdict.needs_approval and not check.verdict.denied


# ---------------------------------------------------------------------------- Windows-only
@pytest.mark.skipif(not is_windows(), reason="Windows 专属：PowerShell 子进程与孙进程终止")
def test_windows_powershell_tree_is_killed(tmp_path, workspace, sandbox) -> None:
    host = HostExecutionManager(tmp_path / "rec-win", sandbox=sandbox, default_timeout_s=30.0)
    argv, heart, childpid_file = _tree_command(tmp_path, workspace)
    record = run(host.execute(argv, cwd=workspace, timeout_s=2.0))
    assert record.status is ExecutionStatus.TIMEOUT
    assert record.metadata["kill_evidence"]["method"] == "taskkill"
    assert _wait_until(childpid_file.exists, 3.0)
    assert pid_alive(int(childpid_file.read_text(encoding="utf-8"))) is False

    # PowerShell 直启也要能跑通并被终止
    ps = run(
        host.execute(
            ["powershell", "-NoProfile", "-Command", "Write-Output 'ps-ok'"],
            cwd=workspace,
        )
    )
    assert ps.status is ExecutionStatus.SUCCEEDED
    assert "ps-ok" in ps.stdout
