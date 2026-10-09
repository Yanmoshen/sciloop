# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""宿主进程运行器：argv 直启、增量输出、进程树终止（Agent 2 / WP-05）。

设计要点（逐条对应文档 WP-05）：

- **argv 优先**：只用 ``asyncio.create_subprocess_exec``，绝不把结构化命令重新拼成 shell；
- **进程树终止**：软终止 → 宽限 → 硬终止（见 :mod:`.process_tree`）；
- **增量输出**：stdout/stderr 分块回调 + 按**字节与行数**双限截断（见 :mod:`.output`）；
- **超时/取消都能终止进程树**，且都返回**结构化记录**而不是抛异常。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock, SystemClock

from . import unix, windows
from .models import ExecutionRecord, ExecutionStatus
from .output import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_OUTPUT_LINES,
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

OutputHook = Callable[[str, str], None]

#: 每次读取的块大小
READ_CHUNK = 4096
#: 无超时时的兜底上限，防止测试卡死
DEFAULT_TIMEOUT_S = 120.0


def _pid_alive_alias(pid: int) -> bool:  # pragma: no cover - 兼容别名
    return pid_alive(pid)


def _run_windows_tool(argv: list[str]) -> dict[str, Any]:  # pragma: no cover - 兼容别名
    return windows.run_tool(argv)


@dataclass
class ProcessRun:
    """运行器的返回值（尚未落盘）。"""

    argv: list[str]
    cwd: str
    status: ExecutionStatus
    pid: int | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    killed_reason: str | None = None
    error: dict[str, Any] | None = None
    kill_evidence: dict[str, Any] = field(default_factory=dict)
    output_chunks: int = 0
    channels: list[dict[str, Any]] = field(default_factory=list)
    #: 退出方式：exited / signal / killed
    exit_kind: str | None = None

    @property
    def signal_exit(self) -> bool:
        """是否被信号终止（与普通非零退出区分）。"""
        return self.exit_code is not None and self.exit_code < 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "status": self.status.value,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "exit_kind": self.exit_kind,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "truncated": self.truncated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "killed_reason": self.killed_reason,
            "error": self.error,
            "kill_evidence": self.kill_evidence,
            "output_chunks": self.output_chunks,
            "channels": list(self.channels),
        }


class ProcessRunner:
    """argv 直启的宿主进程运行器。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_output_lines: int = DEFAULT_MAX_OUTPUT_LINES,
        kill_grace_s: float = KILL_GRACE_S,
        soft_grace_s: float = SOFT_GRACE_S,
        default_timeout_s: float | None = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.clock = clock or SystemClock()
        self.max_output_bytes = int(max_output_bytes)
        self.max_output_lines = int(max_output_lines)
        self.kill_grace_s = float(kill_grace_s)
        self.soft_grace_s = float(soft_grace_s)
        self.default_timeout_s = default_timeout_s

    # ------------------------------------------------------------------ 入口
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        cancel: CancelToken | None = None,
        on_output: OutputHook | None = None,
        on_spawn: Callable[[int], None] | None = None,
        record: ExecutionRecord | None = None,
    ) -> ProcessRun:
        """执行一条命令并返回结构化结果（**不抛执行异常**）。"""
        argv_list = [str(item) for item in argv]
        work_dir = str(cwd)
        started_iso = self.clock.now_iso()
        started_monotonic = time.monotonic()
        effective_timeout = timeout_s if timeout_s is not None else self.default_timeout_s

        run = ProcessRun(
            argv=argv_list,
            cwd=work_dir,
            status=ExecutionStatus.RUNNING,
            started_at=started_iso,
        )
        if record is not None:
            record.status = ExecutionStatus.RUNNING
            record.started_at = started_iso
            record.argv = argv_list
            record.cwd = work_dir

        environment: dict[str, str] | None = None
        if env is not None:
            environment = {**os.environ, **{str(k): str(v) for k, v in env.items()}}

        platform_kwargs = windows.spawn_kwargs() if is_windows() else unix.spawn_kwargs()

        try:
            process = await asyncio.create_subprocess_exec(
                *argv_list,
                cwd=work_dir,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **platform_kwargs,
            )
        except (OSError, ValueError) as exc:
            run.status = ExecutionStatus.FAILED
            run.error = {"code": "spawn_failed", "message": f"{type(exc).__name__}: {exc}"}
            run.exit_kind = "spawn_failed"
            run.finished_at = self.clock.now_iso()
            run.duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            if record is not None:
                self._apply(record, run)
            return run

        run.pid = process.pid
        if record is not None:
            record.pid = process.pid
            record.status = ExecutionStatus.RUNNING
        if on_spawn is not None:
            # 启动即回调（调用方据此把 pid 落盘）：Turn 中断与重启扫描都要能拿到进程号
            with contextlib.suppress(Exception):
                on_spawn(process.pid)

        collector = OutputCollector(
            max_bytes=self.max_output_bytes, max_lines=self.max_output_lines
        )

        async def pump(stream: asyncio.StreamReader | None, channel: str) -> None:
            if stream is None:  # pragma: no cover - 防御
                return
            while True:
                data = await stream.read(READ_CHUNK)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                collector.append(channel, text)
                if on_output is not None:
                    on_output(channel, text)

        pumps = [
            asyncio.create_task(pump(process.stdout, "stdout")),
            asyncio.create_task(pump(process.stderr, "stderr")),
        ]

        wait_task = asyncio.create_task(process.wait())
        cancelled_event = asyncio.Event()
        unsubscribe: Callable[[], None] | None = None
        if cancel is not None:
            loop = asyncio.get_running_loop()

            def _on_cancel(reason: str) -> None:
                run.killed_reason = reason or "cancelled"
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(cancelled_event.set)

            unsubscribe = cancel.subscribe(_on_cancel)
            if cancel.cancelled:
                cancelled_event.set()

        racers: dict[str, asyncio.Task[Any]] = {"process": wait_task}
        racers["cancel"] = asyncio.create_task(cancelled_event.wait())
        timeout_task: asyncio.Task[Any] | None = None
        if effective_timeout is not None:
            timeout_task = asyncio.create_task(asyncio.sleep(float(effective_timeout)))
            racers["timeout"] = timeout_task

        try:
            done, _ = await asyncio.wait(set(racers.values()), return_when=asyncio.FIRST_COMPLETED)
            if racers["cancel"] in done and not wait_task.done():
                run.status = ExecutionStatus.CANCELLED
                run.killed_reason = run.killed_reason or "cancelled"
                await self._kill(process, run, "cancelled")
            elif timeout_task is not None and timeout_task in done and not wait_task.done():
                run.status = ExecutionStatus.TIMEOUT
                run.killed_reason = "timeout"
                run.error = {
                    "code": "timeout",
                    "message": (
                        f"command exceeded {effective_timeout}s and its process tree was terminated"
                    ),
                }
                await self._kill(process, run, "timeout")
            else:
                run.exit_code = wait_task.result()
        finally:
            if unsubscribe is not None:
                unsubscribe()
            for task in racers.values():
                if task is not wait_task and not task.done():
                    task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=self.kill_grace_s)

        with contextlib.suppress(Exception):
            await asyncio.gather(*pumps, return_exceptions=True)

        if run.status is ExecutionStatus.RUNNING:
            run.exit_code = run.exit_code if run.exit_code is not None else process.returncode
            if run.exit_code == 0:
                run.status = ExecutionStatus.SUCCEEDED
                run.exit_kind = "exited"
            elif run.exit_code is not None and run.exit_code < 0:
                # 被信号终止：与"普通非零退出"区分（文档 §6 测试要求）
                run.status = ExecutionStatus.FAILED
                run.exit_kind = "signal"
                run.error = {
                    "code": "signal_exit",
                    "message": f"command was terminated by signal {-run.exit_code}",
                    "signal": -run.exit_code,
                }
            else:
                run.status = ExecutionStatus.FAILED
                run.exit_kind = "exited"
                run.error = {
                    "code": "nonzero_exit",
                    "message": f"command exited with code {run.exit_code}",
                    "exit_code": run.exit_code,
                }

        run.stdout = collector.stdout
        run.stderr = collector.stderr
        run.stdout_bytes = collector.channels["stdout"].bytes_seen
        run.stderr_bytes = collector.channels["stderr"].bytes_seen
        run.truncated = collector.truncated
        run.output_chunks = collector.chunks
        run.channels = collector.output_events()
        run.finished_at = self.clock.now_iso()
        run.duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        if record is not None:
            record.metadata["channels"] = list(run.channels)
            self._apply(record, run)
        return run

    # ------------------------------------------------------------------ 内部
    async def _kill(self, process: asyncio.subprocess.Process, run: ProcessRun, reason: str) -> None:
        if process.returncode is None:
            run.kill_evidence = terminate_process_tree(
                process.pid, grace_s=self.soft_grace_s, force=True
            )
            run.kill_evidence["reason"] = reason
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=self.kill_grace_s)
        run.exit_code = process.returncode
        run.exit_kind = "killed"

    @staticmethod
    def _apply(record: ExecutionRecord, run: ProcessRun) -> None:
        record.status = run.status
        record.pid = run.pid
        record.exit_code = run.exit_code
        record.stdout = run.stdout
        record.stderr = run.stderr
        record.stdout_bytes = run.stdout_bytes
        record.stderr_bytes = run.stderr_bytes
        record.truncated = run.truncated
        record.started_at = run.started_at
        record.finished_at = run.finished_at
        record.duration_ms = run.duration_ms
        record.killed_reason = run.killed_reason
        record.error = run.error
        if run.exit_kind:
            record.metadata["exit_kind"] = run.exit_kind
        if run.kill_evidence:
            record.metadata["kill_evidence"] = run.kill_evidence


def python_executable() -> str:
    """当前解释器（测试里起子进程用，避免依赖 PATH）。"""
    return sys.executable or "python"


__all__ = [
    "ProcessRunner",
    "ProcessRun",
    "OutputHook",
    "terminate_process_tree",
    "pid_alive",
    "probe_capability",
    "is_windows",
    "python_executable",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_OUTPUT_LINES",
    "DEFAULT_TIMEOUT_S",
    "KILL_GRACE_S",
    "SOFT_GRACE_S",
]
