# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""Windows 平台能力探测与路径细节（Agent 2 / WP-02）。

**如实报告**：本线实现的是「策略层边界 + 审批 + 进程树终止」，
没有实现 Codex 的 Windows ACL / AppContainer 级沙箱，因此 ``strong_isolation=False``。
不能因为策略层挡住了路径就说"已启用系统级沙箱"。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .models import PlatformCapability


def _run(argv: list[str]) -> tuple[int, str]:
    result = subprocess.run(argv, capture_output=True, check=False)
    return result.returncode, (result.stdout or b"").decode("utf-8", errors="replace")


def has_job_object_support() -> bool:
    """是否可用 Job Object 约束子进程（需要 ctypes 调用 win32 API，本线暂不实现）。"""
    return False


def has_acl_sandbox() -> bool:
    """是否实现了基于 ACL 的写限制沙箱（Codex ``windows-sandbox-rs`` 路线）。"""
    return False


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """是否为重解析点（junction / symlink / mount point）。"""
    try:
        return bool(os.lstat(path).st_file_attributes & os.stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def resolved_case(path: str | os.PathLike[str]) -> str:
    """拿系统记录的真实大小写（Windows 目录项不区分大小写但保留原名）。"""
    raw = str(path)
    if not raw:
        return raw
    parent, name = os.path.split(raw)
    if not parent:
        return raw
    try:
        for entry in os.listdir(parent):
            if entry.casefold() == name.casefold():
                return os.path.join(parent, entry)
    except OSError:
        return raw
    return raw


def probe() -> PlatformCapability:
    """探测 Windows 上的实际隔离能力。"""
    mechanisms = ["policy-layer", "approval-gate", "taskkill-process-tree"]
    notes: list[str] = []
    if not has_acl_sandbox():
        mechanisms.append("acl-sandbox=unavailable")
        notes.append(
            "未实现 ACL/AppContainer 级写限制：策略层拒绝与审批是唯一防线，"
            "被批准的命令在被批准后拥有该用户权限"
        )
    if not has_job_object_support():
        mechanisms.append("job-object=unavailable")
    mechanisms.append("taskkill /F /T" if os.name == "nt" else "taskkill=unavailable")
    return PlatformCapability(
        platform="windows" if os.name == "nt" else f"posix({os.name})",
        strong_isolation=False,
        mechanisms=tuple(mechanisms),
        notes=tuple(notes),
    )


def display_path(path: str | os.PathLike[str]) -> str:
    """审计展示用：保留系统真实大小写（比较仍走 casefold 归一化键）。"""
    return resolved_case(str(Path(path)))


__all__ = [
    "probe",
    "has_acl_sandbox",
    "has_job_object_support",
    "is_reparse_point",
    "resolved_case",
    "display_path",
]
