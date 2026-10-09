# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""进程树终止与存活判定（Agent 2 / WP-05）。

文档要求「超时先发送终止信号，等待宽限时间后强制终止进程树」。因此：

1. **软终止**：POSIX ``SIGTERM`` 进程组 / Windows ``taskkill /T``（不带 ``/F``）；
2. **宽限等待**：轮询进程是否退出（默认 3s）；
3. **硬终止**：仍存活则 ``SIGKILL`` 进程组 / ``taskkill /F /T``（含子孙）。

存活判定会把**僵尸进程判为已死**（容器里 PID 1 不回收 reparent 的子进程，
``os.kill(pid, 0)`` 对僵尸仍然成功，会造成"进程树没被终止"的假阴性）。
"""

from __future__ import annotations

import os
from typing import Any

from . import unix, windows

#: 杀掉进程树后等待回收的宽限时间
KILL_GRACE_S = 5.0
#: 软终止后等待退出的宽限时间
SOFT_GRACE_S = 3.0


def is_windows() -> bool:
    return os.name == "nt"


def pid_alive(pid: int) -> bool:
    """跨平台存活判定（僵尸算死）。"""
    if is_windows():
        return str(pid) in windows.list_processes(pid)
    return unix.is_alive(pid)


def terminate_process_tree(
    pid: int, *, grace_s: float = SOFT_GRACE_S, force: bool = True
) -> dict[str, Any]:
    """软终止 → 宽限 → 硬终止（含整棵进程树）。返回可审计的证据。"""
    if pid <= 0:
        return {"pid": pid, "method": "noop", "ok": True, "steps": []}
    platform = windows if is_windows() else unix
    steps: list[dict[str, Any]] = []

    soft = platform.terminate(pid, force=False)
    steps.append({**soft, "phase": "soft"})
    exited = _wait_exit(pid, timeout_s=grace_s)
    if exited or not force:
        return {
            "pid": pid,
            "method": soft.get("method"),
            "ok": exited or not pid_alive(pid),
            "steps": steps,
            "exited_after": "soft" if exited else "unknown",
        }

    hard = platform.terminate(pid, force=True)
    steps.append({**hard, "phase": "hard"})
    exited = _wait_exit(pid, timeout_s=KILL_GRACE_S)
    return {
        "pid": pid,
        "method": hard.get("method"),
        "ok": exited or not pid_alive(pid),
        "steps": steps,
        "exited_after": "hard" if exited else "unconfirmed",
    }


def _wait_exit(pid: int, *, timeout_s: float) -> bool:
    if is_windows():
        import time

        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.05)
        return not pid_alive(pid)
    return unix.wait_for_exit(pid, timeout_s=timeout_s)


def probe_capability() -> dict[str, Any]:
    """宿主执行的平台能力（如实报告）。"""
    platform = windows if is_windows() else unix
    data = dict(platform.probe())
    data["pid_alive_zombie_aware"] = not is_windows()
    return data


__all__ = [
    "KILL_GRACE_S",
    "SOFT_GRACE_S",
    "is_windows",
    "pid_alive",
    "terminate_process_tree",
    "probe_capability",
]
