# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""Windows 平台进程细节（Agent 2 / WP-05）。

- 子进程用 ``CREATE_NEW_PROCESS_GROUP`` 起，便于整组终止；
- 终止走 ``taskkill``：先不带 ``/F``（软），宽限后带 ``/F /T``（硬，含子孙）；
- 输出容错解码：中文系统上 taskkill/tasklist 的输出是 GBK，UTF-8 解码会抛错。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

#: 新建进程组标志（等价于 subprocess.CREATE_NEW_PROCESS_GROUP）
CREATE_NEW_PROCESS_GROUP = 0x00000200


def spawn_kwargs() -> dict[str, Any]:
    """``asyncio.create_subprocess_exec`` 的平台参数。"""
    return {"creationflags": CREATE_NEW_PROCESS_GROUP}


def run_tool(argv: list[str]) -> dict[str, Any]:
    """调用 Windows 命令行工具并容错解码（见模块说明）。"""
    result = subprocess.run(argv, capture_output=True, check=False)
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    if "\ufffd" in stdout or "\ufffd" in stderr:  # 试一次本地代码页（cp936）更可读
        stdout = (result.stdout or b"").decode("gbk", errors="replace")
        stderr = (result.stderr or b"").decode("gbk", errors="replace")
    return {"returncode": result.returncode, "stdout": stdout, "stderr": stderr}


def list_processes(pid: int) -> str:
    return run_tool(["tasklist", "/FI", f"PID eq {pid}", "/NH"])["stdout"]


def terminate(pid: int, *, force: bool) -> dict[str, Any]:
    """软终止（不带 /F）/ 硬终止（``/F /T``，含整棵进程树）。"""
    argv = ["taskkill", "/T", "/PID", str(pid)]
    if force:
        argv.insert(1, "/F")
    result = run_tool(argv)
    return {"pid": pid, "method": "taskkill-force" if force else "taskkill-soft", **result}


def probe() -> dict[str, Any]:
    """能力探测（如实报告；未实现 ACL/Job Object 级沙箱）。"""
    return {
        "platform": "windows" if os.name == "nt" else f"posix({os.name})",
        "process_tree_kill": True,
        "soft_then_hard": True,
        "job_object": False,
        "acl_sandbox": False,
        "notes": ["未实现 ACL/Job Object 级强隔离；策略层与审批是唯一防线"],
    }


__all__ = ["spawn_kwargs", "run_tool", "list_processes", "terminate", "probe", "CREATE_NEW_PROCESS_GROUP"]
