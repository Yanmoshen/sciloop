# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""宿主执行管理器：幂等、Turn 中断、重启扫描（Agent 2 / WP-05）。

验收书 §5 的四条硬要求在这里落地：

- 同一个 ``(thread_id, turn_id, call_id)`` 重复请求**幂等**（不重跑）；
- Turn 中断 → 终止该 Turn 的进程树；
- 服务重启扫描未完成记录 → 标 ``unknown`` / ``interrupted``，**禁止盲目重放**；
- 非零退出码是结构化结果，不是系统异常。
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from services.sandbox_v2 import SandboxManager

from .models import FINAL_STATUSES, ExecutionRecord, ExecutionStatus, new_execution_id
from .output import DEFAULT_MAX_OUTPUT_LINES
from .recovery import mark_after_restart, recovery_report
from .runner import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_S,
    OutputHook,
    ProcessRun,
    ProcessRunner,
    pid_alive,
    probe_capability,
    terminate_process_tree,
)

#: 记录文件名后缀
RECORD_SUFFIX = ".exec.json"


class HostExecutionManager:
    """宿主机进程生命周期（不依赖 Web 请求生命周期）。"""

    def __init__(
        self,
        records_dir: str | Path,
        *,
        clock: Clock | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_output_lines: int = DEFAULT_MAX_OUTPUT_LINES,
        default_timeout_s: float | None = DEFAULT_TIMEOUT_S,
        sandbox: SandboxManager | None = None,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.records_dir = Path(records_dir)
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.max_output_bytes = int(max_output_bytes)
        self.max_output_lines = int(max_output_lines)
        self.sandbox = sandbox
        self.runner = runner or ProcessRunner(
            clock=self.clock,
            max_output_bytes=self.max_output_bytes,
            max_output_lines=self.max_output_lines,
            default_timeout_s=default_timeout_s,
        )
        self._lock = threading.RLock()
        self._active: dict[str, ProcessRun] = {}
        self._processes: dict[str, Any] = {}
        self._by_key: dict[tuple[str | None, str | None, str | None], str] = {}
        self._load_index()

    # ------------------------------------------------------------------ 索引
    def _load_index(self) -> None:
        for record in self.scan():
            self._by_key[record.idempotency_key] = record.execution_id

    def scan(self) -> list[ExecutionRecord]:
        """读取全部执行记录（按 execution_id 排序，ID 前缀自带时间序）。"""
        records: list[ExecutionRecord] = []
        for path in sorted(self.records_dir.glob(f"*{RECORD_SUFFIX}")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):  # pragma: no cover - 坏文件跳过
                continue
            records.append(ExecutionRecord.from_dict(data))
        return records

    def get(self, execution_id: str) -> ExecutionRecord | None:
        path = self._path_for(execution_id)
        if not path.is_file():
            return None
        try:
            return ExecutionRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):  # pragma: no cover
            return None

    def find(
        self, *, thread_id: str | None, turn_id: str | None, call_id: str | None
    ) -> ExecutionRecord | None:
        execution_id = self._by_key.get((thread_id, turn_id, call_id))
        if execution_id is None:
            return None
        return self.get(execution_id)

    def recent(self, limit: int = 20) -> list[ExecutionRecord]:
        return self.scan()[-limit:]

    def _path_for(self, execution_id: str) -> Path:
        return self.records_dir / f"{execution_id}{RECORD_SUFFIX}"

    def _persist(self, record: ExecutionRecord) -> None:
        path = self._path_for(record.execution_id)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    # ------------------------------------------------------------------ 执行
    async def execute(
        self,
        argv: Sequence[str],
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        call_id: str | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        cancel: CancelToken | None = None,
        on_output: OutputHook | None = None,
        sandbox_check: bool = True,
        policy: str | None = None,
    ) -> ExecutionRecord:
        """执行一条宿主机命令。重复请求直接复用已有结论（幂等，不重跑）。"""
        argv_list = [str(item) for item in argv]
        key = (thread_id, turn_id, call_id)
        # 只有拿到**明确身份**（thread 或 call）时才做重复执行保护：
        # 三个都是 None 的匿名调用彼此无关（例如测试/探针），共用一把键会让第二条命令
        # 直接复用第一条的结果——那是错的。
        keyed = key != (None, None, None)

        with self._lock:
            existing_id = self._by_key.get(key) if keyed else None
            if existing_id is not None:
                existing = self.get(existing_id)
                if existing is not None:
                    if existing.status in FINAL_STATUSES and existing.status is not ExecutionStatus.UNKNOWN:
                        # 已有结论 → 幂等复用，绝不重跑
                        existing.metadata["duplicates"] = int(existing.metadata.get("duplicates", 0)) + 1
                        self._persist(existing)
                        return existing
                    if existing.status in (ExecutionStatus.RUNNING, ExecutionStatus.PENDING):
                        # 还在跑/未确认 → 也复用，不重跑
                        return existing
                    # UNKNOWN：禁止盲目重放
                    return existing

            sandbox = self.sandbox
            work_dir = str(cwd) if cwd is not None else (
                str(sandbox.workspace_root) if sandbox is not None else str(Path.cwd())
            )
            record = ExecutionRecord(
                execution_id=new_execution_id(),
                argv=argv_list,
                cwd=work_dir,
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
                status=ExecutionStatus.PENDING,
                policy=policy or (sandbox.policy.value if sandbox is not None else None),
            )

            if sandbox_check and sandbox is not None:
                check = sandbox.check_argv(argv_list, cwd=work_dir)
                record.metadata["sandbox"] = check.to_dict()
                if check.verdict.denied:
                    record.status = ExecutionStatus.FAILED
                    record.error = {
                        "code": "sandbox_denied",
                        "message": check.verdict.reason,
                        "verdict": check.verdict.to_dict(),
                    }
                    record.finished_at = self.clock.now_iso()
                    if keyed:
                        self._by_key[key] = record.execution_id
                    self._persist(record)
                    return record

            if keyed:
                self._by_key[key] = record.execution_id
            self._persist(record)

        def _on_spawn(pid: int) -> None:
            """进程一起来就把 pid/状态落盘。

            否则盘上的记录一直是 PENDING 且没有 pid：Turn 中断找不到要终止的进程树，
            服务重启扫描也无法识别孤儿进程。
            """
            with self._lock:
                record.pid = pid
                record.status = ExecutionStatus.RUNNING
                self._persist(record)

        run = await self.runner.run(
            argv_list,
            cwd=work_dir,
            env=env,
            timeout_s=timeout_s,
            cancel=cancel,
            on_output=on_output,
            on_spawn=_on_spawn,
            record=record,
        )
        with self._lock:
            self._active.pop(record.execution_id, None)
            # ⚠️ 运行器把结果**就地写回** record，因此必须持久化这份内存对象；
            # 早先这里改成 `self.get()` 导致落盘仍是 PENDING 的那一版。
            # 外部终止（Turn 中断 / 显式 interrupt_execution）优先于原始退出码：
            # 被 SIGKILL/taskkill 掉的进程退出码是负值，只看退出码会误记成 failed。
            disk = self.get(record.execution_id)
            if disk is not None and disk.status in (
                ExecutionStatus.INTERRUPTED,
                ExecutionStatus.CANCELLED,
            ):
                record.status = disk.status
                record.killed_reason = (
                    record.killed_reason or disk.killed_reason or "terminated_externally"
                )
                record.error = {
                    "code": record.status.value,
                    "message": (disk.error or {}).get("message", "执行被外部终止"),
                }
            record.metadata["runner"] = {"output_chunks": run.output_chunks, "pid": run.pid}
            self._persist(record)
        return record

    def register_process(self, execution_id: str, process: Any) -> None:
        """登记活动进程（供中断时终止进程树）。"""
        with self._lock:
            self._processes[execution_id] = process

    # ------------------------------------------------------------------ 中断
    def interrupt_turn(self, thread_id: str, turn_id: str, reason: str = "turn_interrupted") -> list[str]:
        """Turn 中断：终止该 Turn 名下所有**未完成**执行的进程树。

        返回被终止的 execution_id 列表；记录状态置 ``interrupted``。
        """
        killed: list[str] = []
        for record in self.scan():
            if record.thread_id != thread_id or record.turn_id != turn_id:
                continue
            if record.status in FINAL_STATUSES:
                continue
            if record.pid:
                terminate_process_tree(record.pid)
                killed.append(record.execution_id)
            record.status = ExecutionStatus.INTERRUPTED
            record.killed_reason = reason
            record.finished_at = self.clock.now_iso()
            record.error = {"code": "interrupted", "message": reason}
            self._persist(record)
        return killed

    def interrupt_execution(self, execution_id: str, reason: str = "cancelled") -> bool:
        record = self.get(execution_id)
        if record is None or record.status in FINAL_STATUSES:
            return False
        if record.pid:
            terminate_process_tree(record.pid)
        record.status = ExecutionStatus.CANCELLED
        record.killed_reason = reason
        record.finished_at = self.clock.now_iso()
        self._persist(record)
        return True

    # ------------------------------------------------------------------ 重启扫描
    def scan_orphans(self) -> list[ExecutionRecord]:
        """服务重启扫描：把"未确认完成"的记录标成 ``unknown`` / ``interrupted``。

        - ``RUNNING`` → 进程已不在：``unknown``；进程还活着：``unknown`` + 记 ``orphan_pid``
          （需要人工清理，**绝不自动重放**）；
        - ``PENDING`` → ``interrupted``（从未真正启动）。
        """
        unresolved: list[ExecutionRecord] = []
        now_iso = self.clock.now_iso()
        for record in self.scan():
            if record.status not in (ExecutionStatus.RUNNING, ExecutionStatus.PENDING):
                continue
            alive = bool(record.pid) and pid_alive(int(record.pid or 0))
            mark_after_restart(record, now_iso=now_iso, orphan_alive=alive)
            self._persist(record)
            unresolved.append(record)
        return unresolved

    def kill_orphan(self, execution_id: str) -> dict[str, Any]:
        """清理重启后发现的历史孤儿进程（人工确认后调用）。"""
        record = self.get(execution_id)
        if record is None or record.orphan_pid is None:
            return {"execution_id": execution_id, "killed": False, "reason": "no orphan pid"}
        evidence = terminate_process_tree(int(record.orphan_pid))
        record.orphan_pid = None
        record.metadata["orphan_killed"] = evidence
        self._persist(record)
        return {"execution_id": execution_id, "killed": bool(evidence.get("ok")), "evidence": evidence}

    # ------------------------------------------------------------------ 便捷
    async def run_checked(
        self, argv: Sequence[str], *, cwd: str | Path, sandbox: SandboxManager
    ) -> tuple[ExecutionRecord, bool]:
        """带沙箱预检的便捷入口：返回 ``(记录, 是否被沙箱拦下)``。"""
        check = sandbox.check_argv(list(argv), cwd=cwd)
        if check.verdict.denied:
            return (
                ExecutionRecord(
                    execution_id=new_execution_id(),
                    argv=[str(item) for item in argv],
                    cwd=str(cwd),
                    status=ExecutionStatus.FAILED,
                    error={"code": "sandbox_denied", "message": check.verdict.reason},
                ),
                True,
            )
        record = await self.execute(argv, cwd=cwd, sandbox_check=False)
        return record, False

    # ------------------------------------------------------------------ 能力与证据
    def capability(self) -> dict[str, Any]:
        """平台执行能力（如实报告，不伪造隔离）。"""
        return probe_capability()

    def recovery_report(self) -> dict[str, Any]:
        """未完成执行的扫描报告（交付证据）。"""
        unresolved = [
            item
            for item in self.scan()
            if item.status in (ExecutionStatus.UNKNOWN, ExecutionStatus.INTERRUPTED)
        ]
        return recovery_report(unresolved)

    def close(self) -> None:
        """终止所有活动进程（进程退出时调用，避免后台任务泄漏）。"""
        with self._lock, contextlib.suppress(Exception):
            for process in list(self._processes.values()):
                pid = getattr(process, "pid", None)
                if pid:
                    with contextlib.suppress(Exception):
                        terminate_process_tree(int(pid))
            self._processes.clear()
            self._active.clear()


__all__ = ["HostExecutionManager", "RECORD_SUFFIX"]
