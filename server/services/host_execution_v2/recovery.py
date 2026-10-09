# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""执行恢复：服务重启后的未完成执行扫描（Agent 2 / WP-05）。

文档要求「重启扫描 running/unknown execution，标记 unknown 或 interrupted，
**禁止自动重放未知 Call**」。这里的规则是纯函数，便于测试与复用：

- ``RUNNING`` → ``UNKNOWN``（进程还活着时同时记 ``orphan_pid``，需要人工清理）；
- ``PENDING`` → ``INTERRUPTED``（从未真正启动）；
- ``UNKNOWN`` → 保持未知，且 ``needs_replay=False``（绝不自动重放）。
"""

from __future__ import annotations

from typing import Any

from .models import ExecutionRecord, ExecutionStatus


def classify(record: ExecutionRecord) -> ExecutionStatus:
    """给一条未完成记录判定重启后的状态。"""
    if record.status is ExecutionStatus.RUNNING:
        return ExecutionStatus.UNKNOWN
    if record.status is ExecutionStatus.PENDING:
        return ExecutionStatus.INTERRUPTED
    return record.status


def needs_replay(record: ExecutionRecord) -> bool:
    """**永远为 False**：未确认完成的 Call 不允许自动重放。"""
    return False


def is_replayable(record: ExecutionRecord) -> bool:
    """只有拿到明确结论（非 unknown）的记录才算"有结论"。"""
    return record.has_conclusion


def mark_after_restart(
    record: ExecutionRecord, *, now_iso: str, orphan_alive: bool
) -> ExecutionRecord:
    """就地标记一条未完成记录，返回它（便于链式调用）。"""
    new_status = classify(record)
    if new_status is ExecutionStatus.UNKNOWN:
        record.status = new_status
        record.orphan_pid = int(record.pid) if (orphan_alive and record.pid) else None
        record.finished_at = record.finished_at or now_iso
        record.error = {
            "code": "unknown_after_restart",
            "message": "服务重启时该执行未确认完成，状态记为 unknown；禁止自动重放，需人工确认",
            "orphan_alive": bool(orphan_alive),
        }
    elif new_status is ExecutionStatus.INTERRUPTED:
        record.status = new_status
        record.finished_at = record.finished_at or now_iso
        record.error = {"code": "interrupted", "message": "服务重启时该执行尚未启动"}
    return record


def recovery_report(records: list[ExecutionRecord]) -> dict[str, Any]:
    """重启扫描的报告（交付证据）。"""
    return {
        "scanned": len(records),
        "unknown": [item.execution_id for item in records if item.status is ExecutionStatus.UNKNOWN],
        "interrupted": [
            item.execution_id for item in records if item.status is ExecutionStatus.INTERRUPTED
        ],
        "orphan_pids": [
            {"execution_id": item.execution_id, "pid": item.orphan_pid}
            for item in records
            if item.orphan_pid
        ],
        "replay_allowed": False,
    }


__all__ = [
    "classify",
    "needs_replay",
    "is_replayable",
    "mark_after_restart",
    "recovery_report",
]
