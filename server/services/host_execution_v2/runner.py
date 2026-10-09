# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""宿主进程运行器：argv 直启、增量输出、进程树终止（Agent 2 / WP-05）。

设计要点（逐条对应验收书 §5）：

- **argv 优先**：只用 ``asyncio.create_subprocess_exec``，绝不把结构化命令重新拼成
  shell 字符串（``shell=True`` 在本模块不存在）；
- **进程树终止**：Windows 用 ``taskkill /F /T``，POSIX 用 ``killpg``（子进程以
  ``start_new_session`` 起，自己就是进程组组长）；
- **增量输出**：stdout/stderr 分块读取，逐块回调 + 累积（超上限截断并如实标记）；
- **超时/取消都能终止进程树**，且都返回**结构化记录**而不是抛异常。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock, SystemClock

from .models import ExecutionRecord, ExecutionStatus

OutputHook = Callable[[str, str], None]

#: 默认输出上限（每个通道）：超出即截断并标记
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024
#: 每次读取的块大小
READ_CHUNK = 4096
#: 杀掉进程树后等待回收的宽限时间
KILL_GRACE_S = 5.0
#: 无超时时的兜底上限，防止测试卡死
DEFAULT_TIMEOUT_S = 120.0


def is_windows() -> bool:
    return os.name == "nt"


def pid_alive(pid: int) -> bool:
    """跨平台判断进程是否**真的还在跑**（不引入 psutil）。

    ⚠️ 关键细节：被杀死但尚未回收的进程是**僵尸（Z）**，`os.kill(pid, 0)` 对它仍然成功。
    容器里 PID 1 往往就是测试进程本身，被 reparent 的孙进程僵尸永远不会被回收 ——
    因此这里显式读 ``/proc/<pid>/stat`` 的状态位，把 ``Z`` 判成"已死"，
    否则「进程树是否真的被终止」的断言会假阴性。
    """
    if pid <= 0:
        return False
    if is_windows():
        result = _run_windows_tool(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return str(pid) in result["stdout"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - 存在但无权限
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            payload = handle.read()
        state = payload.rsplit(b")", 1)[1].strip().split(b" ", 1)[0]
        if state == b"Z":
            return False
    except OSError:  # pragma: no cover - 无 /proc 时回退到 kill 判定
        pass
    return True


def _run_windows_tool(argv: list[str]) -> dict[str, Any]:
    """调用 Windows 命令行工具并**容错解码**。

    中文 Windows 上 ``taskkill`` / ``tasklist`` 的输出是 GBK，而 Python 在 UTF-8 模式下
    会按 UTF-8 解码 → `UnicodeDecodeError`（实测在探针里出现过）。因此这里拿字节自己解码，
    `errors="replace"` 兜底：判断"进程是否还在"只看 PID 数字，不依赖输出里的中文。
    """
    result = subprocess.run(argv, capture_output=True, check=False)
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    if "\ufffd" in stdout or "\ufffd" in stderr:  # 试一次本地代码页（cp936）更可读
        stdout = (result.stdout or b"").decode("gbk", errors="replace")
        stderr = (result.stderr or b"").decode("gbk", errors="replace")
    return {"returncode": result.returncode, "stdout": stdout, "stderr": stderr}


def terminate_process_tree(pid: int, *, grace_s: float = KILL_GRACE_S) -> dict[str, Any]:
    """终止整棵进程树（含孙进程）。返回可审计的终止结果。"""
    if pid <= 0:
        return {"pid": pid, "method": "noop", "ok": True}
    if is_windows():
        result = _run_windows_tool(["taskkill", "/F", "/T", "/PID", str(pid)])
        return {
            "pid": pid,
            "method": "taskkill",
            "ok": result["returncode"] == 0 or not pid_alive(pid),
            "stdout": result["stdout"].strip(),
            "stderr": result["stderr"].strip(),
        }
    method = "killpg"
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        ok = True
    except ProcessLookupError:
        ok = True
    except OSError:
        method = "kill"
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
        ok = not pid_alive(pid)
    del grace_s
    return {"pid": pid, "method": method, "ok": ok}


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "status": self.status.value,
            "pid": self.pid,
            "exit_code": self.exit_code,
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
        }


class ProcessRunner:
    """argv 直启的宿主进程运行器。"""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        kill_grace_s: float = KILL_GRACE_S,
        default_timeout_s: float | None = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.clock = clock or SystemClock()
        self.max_output_bytes = int(max_output_bytes)
        self.kill_grace_s = float(kill_grace_s)
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

        environment: dict[str, str] | None
        if env is None:
            environment = None
        else:
            environment = {**os.environ, **{str(k): str(v) for k, v in env.items()}}

        creationflags = 0
        start_new_session = False
        if is_windows():
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            start_new_session = True

        try:
            process = await asyncio.create_subprocess_exec(
                *argv_list,
                cwd=work_dir,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except (OSError, ValueError) as exc:
            run.status = ExecutionStatus.FAILED
            run.error = {"code": "spawn_failed", "message": f"{type(exc).__name__}: {exc}"}
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

        buffers: dict[str, list[str]] = {"stdout": [], "stderr": []}
        counters: dict[str, int] = {"stdout": 0, "stderr": 0}
        truncated = {"flag": False}
        chunks = {"count": 0}

        async def pump(stream: asyncio.StreamReader | None, channel: str) -> None:
            if stream is None:  # pragma: no cover - 防御
                return
            while True:
                data = await stream.read(READ_CHUNK)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                counters[channel] += len(data)
                chunks["count"] += 1
                remaining = self.max_output_bytes - sum(len(item) for item in buffers[channel])
                if remaining <= 0:
                    truncated["flag"] = True
                else:
                    buffers[channel].append(text[:remaining] if len(text) > remaining else text)
                    if len(text) > remaining:
                        truncated["flag"] = True
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
                # 取消可能来自任意线程：必须切回事件循环线程再置位
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(cancelled_event.set)

            unsubscribe = cancel.subscribe(_on_cancel)
            if cancel.cancelled:  # 注册时已取消 → subscribe 已回调一次，这里兜底
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
                    "message": f"command exceeded {effective_timeout}s and its process tree was terminated",
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
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=self.kill_grace_s)

        with contextlib.suppress(Exception):
            await asyncio.gather(*pumps, return_exceptions=True)

        if run.status is ExecutionStatus.RUNNING:
            run.exit_code = run.exit_code if run.exit_code is not None else process.returncode
            if run.exit_code == 0:
                run.status = ExecutionStatus.SUCCEEDED
            else:
                run.status = ExecutionStatus.FAILED
                run.error = {
                    "code": "nonzero_exit",
                    "message": f"command exited with code {run.exit_code}",
                    "exit_code": run.exit_code,
                }

        run.stdout = "".join(buffers["stdout"])
        run.stderr = "".join(buffers["stderr"])
        run.stdout_bytes = counters["stdout"]
        run.stderr_bytes = counters["stderr"]
        run.truncated = bool(truncated["flag"])
        run.output_chunks = chunks["count"]
        run.finished_at = self.clock.now_iso()
        run.duration_ms = int((time.monotonic() - started_monotonic) * 1000)
        if record is not None:
            self._apply(record, run)
        return run

    # ------------------------------------------------------------------ 内部
    async def _kill(
        self, process: asyncio.subprocess.Process, run: ProcessRun, reason: str
    ) -> None:
        if process.returncode is None:
            run.kill_evidence = terminate_process_tree(process.pid)
            run.kill_evidence["reason"] = reason
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(process.wait(), timeout=self.kill_grace_s)
        run.exit_code = process.returncode

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
    "is_windows",
    "python_executable",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_S",
    "KILL_GRACE_S",
]
