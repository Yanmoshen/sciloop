"""Windows 宿主执行探针（**在 Windows 上直接运行**，产出验收证据）。

容器里的 pytest 跑不到 Windows 专属路径，因此这里提供一个可独立运行的探针：

    python server/tests/agent_v2/tools/windows_probe.py

它会做四件事并打印可复核的日志（心跳停止时间、taskkill 输出、退出码）：

1. ``taskkill /F /T`` 终止 PowerShell 起的**孙进程**；
2. PowerShell 直启可正常跑通并捕获输出；
3. 超时终止进程树；
4. Turn 中断终止进程树。

**只用临时目录**，不触碰真实数据；退出码非 0 表示探针失败。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[3]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from services.host_execution_v2 import (  # noqa: E402
    ExecutionStatus,
    HostExecutionManager,
    is_windows,
    pid_alive,
)
from services.sandbox_v2 import SandboxManager, SandboxPolicy  # noqa: E402

HEARTBEAT_SCRIPT = (
    "import sys, time, pathlib\n"
    "target = pathlib.Path(sys.argv[1])\n"
    "while True:\n"
    "    target.write_text(str(time.time()))\n"
    "    time.sleep(0.15)\n"
)

PARENT_SCRIPT = (
    "import subprocess, sys, time, pathlib\n"
    "heart, script = sys.argv[1], sys.argv[2]\n"
    "child = subprocess.Popen([sys.executable, script, heart])\n"
    "pathlib.Path(heart + '.childpid').write_text(str(child.pid))\n"
    "time.sleep(120)\n"
)


def _wait(predicate, timeout_s: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def main() -> int:
    log: dict[str, object] = {"platform": os.name, "windows": is_windows()}
    if not is_windows():
        print(json.dumps({**log, "result": "skipped"}, ensure_ascii=False, indent=2))
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        scripts = root / "scripts"
        scripts.mkdir()
        heartbeat = scripts / "heartbeat.py"
        heartbeat.write_text(HEARTBEAT_SCRIPT, encoding="utf-8")
        parent = scripts / "parent.py"
        parent.write_text(PARENT_SCRIPT, encoding="utf-8")

        sandbox = SandboxManager(workspace, policy=SandboxPolicy.WORKSPACE_WRITE)
        host = HostExecutionManager(root / "records", sandbox=sandbox, default_timeout_s=30.0)

        async def scenario() -> None:
            # 1) PowerShell 直启
            ps = await host.execute(
                ["powershell", "-NoProfile", "-Command", "Write-Output 'probe-ok'"],
                cwd=workspace,
                thread_id="th_probe",
                turn_id="tu_ps",
                call_id="call_ps",
            )
            log["powershell"] = {
                "status": ps.status.value,
                "stdout": ps.stdout.strip(),
                "exit_code": ps.exit_code,
            }

            # 2) 超时终止进程树（孙进程心跳必须停止）
            heart = workspace / "hb.txt"
            timeout_cmd = [sys.executable, str(parent), str(heart), str(heartbeat)]
            killed = await host.execute(
                timeout_cmd,
                cwd=workspace,
                timeout_s=2.5,
                thread_id="th_probe",
                turn_id="tu_timeout",
                call_id="call_timeout",
            )
            child_pid_file = Path(f"{heart}.childpid")
            _wait(child_pid_file.exists)
            child_pid = int(child_pid_file.read_text(encoding="utf-8")) if child_pid_file.exists() else 0
            stamp = heart.stat().st_mtime_ns
            time.sleep(1.2)
            log["timeout_kill"] = {
                "status": killed.status.value,
                "kill_evidence": killed.metadata.get("kill_evidence"),
                "child_pid": child_pid,
                "child_alive_after_kill": pid_alive(child_pid),
                "heartbeat_stopped": heart.stat().st_mtime_ns == stamp,
                "exit_code": killed.exit_code,
            }

            # 3) Turn 中断终止进程树
            heart2 = workspace / "hb2.txt"
            interrupt_cmd = [sys.executable, str(parent), str(heart2), str(heartbeat)]
            task = asyncio.ensure_future(
                host.execute(
                    interrupt_cmd,
                    cwd=workspace,
                    thread_id="th_probe",
                    turn_id="tu_interrupt",
                    call_id="call_interrupt",
                )
            )
            await asyncio.sleep(1.5)
            terminated = host.interrupt_turn("th_probe", "tu_interrupt", reason="probe_interrupt")
            record = await task
            child_pid_file2 = Path(f"{heart2}.childpid")
            _wait(child_pid_file2.exists)
            child_pid2 = (
                int(child_pid_file2.read_text(encoding="utf-8")) if child_pid_file2.exists() else 0
            )
            stamp2 = heart2.stat().st_mtime_ns
            time.sleep(1.2)
            log["interrupt_kill"] = {
                "terminated": terminated,
                "status": record.status.value,
                "child_pid": child_pid2,
                "child_alive_after_kill": pid_alive(child_pid2),
                "heartbeat_stopped": heart2.stat().st_mtime_ns == stamp2,
            }

            # 4) 重启扫描：未确认完成 → unknown，且不重放
            replay_key = {"thread_id": "th_probe", "turn_id": "tu_replay", "call_id": "call_replay"}
            done = await host.execute(
                [sys.executable, "-c", "print('once')"], cwd=workspace, **replay_key
            )
            path = root / "records" / f"{done.execution_id}.exec.json"
            path.write_text(
                path.read_text(encoding="utf-8").replace('"succeeded"', '"running"'),
                encoding="utf-8",
            )
            revived = HostExecutionManager(root / "records", sandbox=sandbox)
            unresolved = revived.scan_orphans()
            again = await revived.execute(
                [sys.executable, "-c", "print('SHOULD-NOT-RUN')"], cwd=workspace, **replay_key
            )
            log["restart_scan"] = {
                "unresolved": [item.execution_id for item in unresolved],
                "scanned_status": revived.get(done.execution_id).status.value,
                "replayed": again.execution_id == done.execution_id,
                "replay_status": again.status.value,
            }

        asyncio.run(scenario())

    ok = (
        log.get("powershell", {}).get("status") == ExecutionStatus.SUCCEEDED.value
        and log.get("timeout_kill", {}).get("heartbeat_stopped") is True
        and log.get("timeout_kill", {}).get("child_alive_after_kill") is False
        and log.get("interrupt_kill", {}).get("heartbeat_stopped") is True
        and log.get("restart_scan", {}).get("scanned_status") == "unknown"
        and log.get("restart_scan", {}).get("replay_status") == "unknown"
    )
    log["result"] = "pass" if ok else "fail"
    print(json.dumps(log, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
