# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""host_execution_v2：宿主机进程生命周期（Agent 2 / WP-05）。

    from services.host_execution_v2 import HostExecutionManager

    manager = HostExecutionManager(records_dir, sandbox=sandbox)
    record = await manager.execute(["python", "x.py"], thread_id=..., turn_id=..., call_id=...)
    manager.scan_orphans()      # 服务重启后：unknown / interrupted，且不重放
    manager.capability()        # 平台能力（如实报告）
"""

from __future__ import annotations

from . import process_tree, recovery, unix, windows
from .manager import RECORD_SUFFIX, HostExecutionManager
from .models import (
    EXECUTION_ID_PREFIX,
    FINAL_STATUSES,
    UNRESOLVED_STATUSES,
    ExecutionRecord,
    ExecutionStatus,
    new_execution_id,
)
from .output import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_OUTPUT_LINES,
    ChannelBuffer,
    OutputCollector,
)
from .process_tree import (
    KILL_GRACE_S,
    SOFT_GRACE_S,
    is_windows,
    pid_alive,
    probe_capability,
    terminate_process_tree,
)
from .recovery import classify, mark_after_restart, needs_replay, recovery_report
from .runner import (
    DEFAULT_TIMEOUT_S,
    ProcessRun,
    ProcessRunner,
    python_executable,
)

__all__ = [
    "HostExecutionManager",
    "RECORD_SUFFIX",
    "ExecutionRecord",
    "ExecutionStatus",
    "FINAL_STATUSES",
    "UNRESOLVED_STATUSES",
    "EXECUTION_ID_PREFIX",
    "new_execution_id",
    "ProcessRunner",
    "ProcessRun",
    "OutputCollector",
    "ChannelBuffer",
    "terminate_process_tree",
    "pid_alive",
    "probe_capability",
    "is_windows",
    "python_executable",
    "classify",
    "needs_replay",
    "mark_after_restart",
    "recovery_report",
    "process_tree",
    "recovery",
    "windows",
    "unix",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_OUTPUT_LINES",
    "DEFAULT_TIMEOUT_S",
    "KILL_GRACE_S",
    "SOFT_GRACE_S",
]
