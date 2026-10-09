# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""host_execution_v2：宿主机进程生命周期（Agent 2 / WP-05）。

    from services.host_execution_v2 import HostExecutionManager

    manager = HostExecutionManager(records_dir, sandbox=sandbox)
    record = await manager.execute(["python", "x.py"], thread_id=..., turn_id=..., call_id=...)
"""

from __future__ import annotations

from .manager import RECORD_SUFFIX, HostExecutionManager
from .models import (
    EXECUTION_ID_PREFIX,
    FINAL_STATUSES,
    UNRESOLVED_STATUSES,
    ExecutionRecord,
    ExecutionStatus,
    new_execution_id,
)
from .runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    KILL_GRACE_S,
    ProcessRun,
    ProcessRunner,
    is_windows,
    pid_alive,
    python_executable,
    terminate_process_tree,
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
    "terminate_process_tree",
    "pid_alive",
    "is_windows",
    "python_executable",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S",
    "KILL_GRACE_S",
]
