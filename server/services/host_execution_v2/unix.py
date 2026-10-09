# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""POSIX 平台进程细节（Agent 2 / WP-05）。

- 子进程以 ``start_new_session=True`` 起，自己就是进程组组长；
- 终止进程组：先 ``SIGTERM``（软），宽限后 ``SIGKILL``（硬）；
- 无 ``killpg`` 时回退到单进程终止（并如实报告能力缺失）。
"""

from __future__ import annotations

import os
import signal
import time
from typing import Any


def spawn_kwargs() -> dict[str, Any]:
    return {"start_new_session": True}


def is_alive(pid: int) -> bool:
    """是否仍在运行（僵尸进程判为"已死"）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            payload = handle.read()
        state = payload.rsplit(b")", 1)[1].strip().split(b" ", 1)[0]
        if state == b"Z":
            return False
    except OSError:  # pragma: no cover - 无 /proc 时回退
        pass
    return True


def terminate(pid: int, *, force: bool) -> dict[str, Any]:
    """软（SIGTERM）/ 硬（SIGKILL）终止进程组。"""
    sig = signal.SIGKILL if force else signal.SIGTERM
    method = "killpg-" + ("force" if force else "soft")
    try:
        os.killpg(os.getpgid(pid), sig)
        return {"pid": pid, "method": method, "returncode": 0, "stdout": "", "stderr": ""}
    except ProcessLookupError:
        return {"pid": pid, "method": method, "returncode": 0, "stdout": "", "stderr": ""}
    except OSError as exc:  # pragma: no cover - 无权限或非组长
        method = "kill-" + ("force" if force else "soft")
        try:
            os.kill(pid, sig)
            return {"pid": pid, "method": method, "returncode": 0, "stdout": "", "stderr": ""}
        except OSError as inner:
            return {
                "pid": pid,
                "method": method,
                "returncode": 1,
                "stdout": "",
                "stderr": f"{type(exc).__name__}/{type(inner).__name__}: {inner}",
            }


def wait_for_exit(pid: int, *, timeout_s: float) -> bool:
    """轮询等待进程退出（返回是否已退出）。"""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return not is_alive(pid)


def probe() -> dict[str, Any]:
    return {
        "platform": "linux" if os.name != "nt" else f"posix({os.name})",
        "process_tree_kill": hasattr(os, "killpg"),
        "soft_then_hard": True,
        "cgroup": False,
        "landlock": False,
        "notes": ["未实现 cgroup/landlock 级强隔离；策略层与审批是唯一防线"],
    }


__all__ = ["spawn_kwargs", "is_alive", "terminate", "wait_for_exit", "probe"]
