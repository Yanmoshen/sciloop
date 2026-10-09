# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""Linux/POSIX 平台能力探测（Agent 2 / WP-02）。

**如实报告**：本线没有实现 landlock / seccomp / cgroup 级强隔离，
因此 ``strong_isolation=False``，只提供进程组终止 + 策略层边界。
landlock 是否**可用**也一并探测出来，供后续增强参考。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path

from .models import PlatformCapability

#: landlock 的系统调用号（x86_64 / aarch64 通用约定：444 起）
_LANDLOCK_CREATE_RULESET = 444


def landlock_available() -> bool:
    """内核是否支持 landlock（只探测，不启用）。"""
    if not Path("/sys/kernel/security").exists() and not Path("/proc/self/status").exists():
        return False
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        return False
    try:
        libc = ctypes.CDLL(libc_name, use_errno=True)
    except OSError:  # pragma: no cover
        return False
    # 传 NULL ruleset_attr + size 0：支持则返回 -1/errno=EINVAL/EOPNOTSUPP 之外的值
    result = libc.syscall(
        ctypes.c_long(_LANDLOCK_CREATE_RULESET), ctypes.c_void_p(0), ctypes.c_size_t(0), ctypes.c_uint32(0)
    )
    errno = ctypes.get_errno()
    if result >= 0:
        return True
    # ENOSYS(38) 表示内核不支持；EOPNOTSUPP(95)/EINVAL(22) 说明系统调用存在但参数被拒
    return errno not in (38,)


def has_process_group_kill() -> bool:
    return hasattr(os, "killpg") and hasattr(os, "getpgid")


def probe() -> PlatformCapability:
    """探测 POSIX 上的实际隔离能力。"""
    mechanisms = ["policy-layer", "approval-gate"]
    notes: list[str] = []
    if has_process_group_kill():
        mechanisms.append("killpg-process-group")
    else:  # pragma: no cover
        mechanisms.append("process-group=unavailable")
        notes.append("无法按进程组终止，只能终止单个进程")

    if landlock_available():
        mechanisms.append("landlock=available(unused)")
        notes.append("内核支持 landlock，但本线未启用（不假称已启用系统级沙箱）")
    else:
        mechanisms.append("landlock=unavailable")

    notes.append("未实现 seccomp/cgroup 级写限制：策略层拒绝与审批是唯一防线")
    return PlatformCapability(
        platform="linux",
        strong_isolation=False,
        mechanisms=tuple(mechanisms),
        notes=tuple(notes),
    )


__all__ = ["probe", "landlock_available", "has_process_group_kill"]
