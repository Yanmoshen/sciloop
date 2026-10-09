# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""宿主执行的记录结构与状态（Agent 2 / WP-05）。"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 执行记录 ID 前缀。契约 ``ID_PREFIXES`` 只覆盖 Thread/Turn/Item/Call/Event/审批/记忆，
#: 执行记录是 Agent 2 自己的领域对象，因此在这里用同构格式生成（同为
#: ``<前缀>_<13 位十六进制毫秒><10 位随机十六进制>``，字典序即时间序）。
EXECUTION_ID_PREFIX = "ex"


def new_execution_id(
    *,
    now_ms: int | None = None,
    rand: Callable[[int], str] | None = None,
) -> str:
    """生成执行记录 ID，格式与契约稳定 ID 同构且可按时间排序。"""
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rnd = (rand or secrets.token_hex)(5)
    return f"{EXECUTION_ID_PREFIX}_{ts:013x}{rnd}"


class ExecutionStatus(StrEnum):
    """一次宿主机执行的收敛状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    #: 服务重启后发现"未确认完成"的记录：**禁止盲目重放**
    UNKNOWN = "unknown"
    #: 因 Turn 中断而终止
    INTERRUPTED = "interrupted"


#: 终态：不会再有后续状态变化
FINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.INTERRUPTED,
        ExecutionStatus.UNKNOWN,
    }
)

#: 明确"没有结论"的状态——重启扫描后落到这里，重放必须先人工确认
UNRESOLVED_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {ExecutionStatus.PENDING, ExecutionStatus.RUNNING, ExecutionStatus.UNKNOWN}
)


@dataclass
class ExecutionRecord:
    """一次宿主机执行的完整证据。"""

    execution_id: str
    argv: list[str]
    cwd: str
    thread_id: str | None = None
    turn_id: str | None = None
    call_id: str | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    killed_reason: str | None = None
    error: dict[str, Any] | None = None
    policy: str | None = None
    #: 重启扫描发现的历史孤儿进程 PID（需要人工清理）
    orphan_pid: int | None = None
    attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- 派生 ----
    @property
    def idempotency_key(self) -> tuple[str | None, str | None, str | None]:
        return (self.thread_id, self.turn_id, self.call_id)

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def has_conclusion(self) -> bool:
        return self.is_final and self.status is not ExecutionStatus.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "execution_id": self.execution_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "status": self.status.value,
            "pid": self.pid,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "truncated": self.truncated,
            "killed_reason": self.killed_reason,
            "error": self.error,
            "policy": self.policy,
            "orphan_pid": self.orphan_pid,
            "attempts": self.attempts,
            "metadata": dict(self.metadata),
        }
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionRecord:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        payload = {key: value for key, value in data.items() if key in known}
        payload["status"] = ExecutionStatus(payload.get("status", ExecutionStatus.PENDING))
        payload["argv"] = list(payload.get("argv") or [])
        return cls(**payload)  # type: ignore[arg-type]

    def to_tool_output(self) -> dict[str, Any]:
        """转成模型可见的结构化工具输出（**不含任何审批令牌**）。"""
        out: dict[str, Any] = {
            "execution_id": self.execution_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "pid": self.pid,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
        }
        if self.killed_reason:
            out["killed_reason"] = self.killed_reason
        return out


__all__ = [
    "ExecutionStatus",
    "FINAL_STATUSES",
    "UNRESOLVED_STATUSES",
    "ExecutionRecord",
    "EXECUTION_ID_PREFIX",
    "new_execution_id",
]
