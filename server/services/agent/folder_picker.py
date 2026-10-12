# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""目录选择任务的状态机（后端侧，异步协议）。

为什么要有这一层
----------------
目录选择是**长交互**：研究员可能在系统窗口里挑几十秒、甚至几分钟。把它挂在一条普通
HTTP 请求上，就会撞上前端 30 秒的默认超时 → 页面显示"请求超时（30000ms）"，选择白做 ✗。
所以把它建模成**短请求 + 任务状态**：

    POST   /projects/pick-folder              建任务 → 立刻回 request_id
    GET    /projects/pick-folder/{id}         读状态
    DELETE /projects/pick-folder/{id}         取消

状态集合固定为 ``pending / selected / canceled / unavailable / failed / expired``；
**终态不可回到 pending**，终态结果保留 10 分钟、重复读返回同一结果（计划 §4.2、§4.3）。

本模块负责建任务、弹出本机原生目录窗口（桌面环境可用时）、收敛终态，并把结果翻译成
用户级文案。无桌面环境时仍保留宿主执行器兼容路径。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from services.agent import host_runner

#: 终态结果保留时长（§4.2：至少 10 分钟）
RESULT_TTL_S = 600.0
#: 单条任务最长存活（§5.1：系统保护期限，到点自动取消并关窗）
MAX_LIFE_S = 900.0

TERMINAL_STATUSES = frozenset({"selected", "canceled", "unavailable", "failed", "expired"})

#: 用户级文案（**绝不出现毫秒数、端口、PID、request id、traceback** ✓ §6.3）
MESSAGES: dict[str, str] = {
    "pending": "正在打开系统选择窗口",
    "selected": "",
    "canceled": "已取消选择",
    "unavailable": "当前设备无法打开系统选择窗口",
    "failed": "选择窗口发生故障，请重试",
    "expired": "选择窗口已关闭，请重新选择",
}


#: 执行器状态 → 服务端状态（执行器的 `timed_out` 收敛成服务端的 `expired`）
HELPER_STATUS_MAP: dict[str, str] = {
    "pending": "pending",
    "selected": "selected",
    "canceled": "canceled",
    "timed_out": "expired",
    "expired": "expired",
    "unavailable": "unavailable",
    "failed": "failed",
}


@dataclass
class PickTask:
    """一条目录选择任务（只保留必要字段：id / 状态 / 宿主路径 / 时间戳 / 错误类别）。"""

    request_id: str
    helper_id: str
    status: str = "pending"
    path: str = ""
    message: str = MESSAGES["pending"]
    error_class: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0


class FolderPickerService:
    """任务表 + 单飞（同一时刻只允许一个系统窗口）+ 终态收敛。"""

    def __init__(
        self,
        *,
        local: bool = False,
        clock: Callable[[], float] = time.time,
        ttl_s: float = RESULT_TTL_S,
        max_life_s: float = MAX_LIFE_S,
        create_id: Callable[[], str] | None = None,
    ) -> None:
        self._tasks: dict[str, PickTask] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._ttl_s = ttl_s
        self._max_life_s = max_life_s
        self._new_id = create_id or (lambda: f"pick_{uuid.uuid4().hex[:16]}")
        self._local = local
        self._local_jobs: dict[str, asyncio.Task[Any]] = {}

    # -- 对外 -------------------------------------------------------------- #
    async def create(self, *, title: str = "", initial: str = "") -> dict[str, Any]:
        """建任务；已有 pending 任务时**复用它**（不再弹第二个窗口 ✓ §4.4）。"""

        async with self._lock:
            now = self._clock()
            self._sweep(now)
            pending = self._active(now)
            if pending is not None:
                return self._view(pending, accepted=False, busy=True, created=False)

            request_id = self._new_id()
            task = PickTask(request_id=request_id, helper_id=request_id)
            task.created_at = task.updated_at = now
            self._tasks[request_id] = task
            if self._local:
                job = asyncio.create_task(self._run_local_dialog(task, title=title, initial=initial))
                self._local_jobs[request_id] = job
                return self._view(task, accepted=True, busy=False, created=True)
            reply = await host_runner.pick_create(
                request_id=request_id, title=title, initial=initial
            )
            self._apply(task, reply, created=True)
            return self._view(task, accepted=True, busy=False, created=True)

    async def _run_local_dialog(self, task: PickTask, *, title: str, initial: str) -> None:
        """Open a native folder dialog in the local Windows desktop process."""
        if os.name != "nt":
            self._settle(task, "unavailable", error_class="not_windows")
            return
        escaped_title = title.replace("'", "''")
        escaped_initial = initial.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            # Use a hidden topmost owner so the native dialog is activated in
            # front of the browser instead of appearing only as a taskbar item.
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None; "
            "$owner.ShowInTaskbar = $false; $owner.TopMost = $true; "
            "$owner.Opacity = 0; $owner.Width = 1; $owner.Height = 1; "
            "$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual; "
            "$area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea; "
            "$owner.Left = $area.Left + [int](($area.Width - $owner.Width) / 2); "
            "$owner.Top = $area.Top + [int](($area.Height - $owner.Height) / 2); "
            "$owner.Show(); $owner.Activate(); "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$d.Description = '{escaped_title}'; "
            "$d.ShowNewFolderButton = $true; "
            f"if ('{escaped_initial}') {{ $d.SelectedPath = '{escaped_initial}' }}; "
            "$result = $d.ShowDialog($owner); "
            "if ($result -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [Console]::Write($d.SelectedPath) }; "
            "$owner.Close(); $owner.Dispose()"
        )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-Command",
                script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Suppress the console without applying a hidden show state
                # to the WinForms dialog, which must remain visible.
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._local_jobs[task.request_id] = asyncio.current_task()  # type: ignore[assignment]
            stdout, _ = await process.communicate()
            if task.status in TERMINAL_STATUSES:
                return
            path = stdout.decode(errors="replace").strip()
            self._settle(task, "selected" if path else "canceled", path=path)
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.terminate()
            raise
        except Exception:
            self._settle(task, "failed", error_class="native_dialog")
        finally:
            self._local_jobs.pop(task.request_id, None)

    async def status(self, request_id: str) -> dict[str, Any]:
        """读任务状态；未知 id（已被清理）按 `expired` 回，允许重新发起 ✓。"""

        async with self._lock:
            # 先清过期结果：超过保留期的终态任务会被丢掉 → 读到 `expired`（允许重新发起）✓ §4.2
            self._sweep(self._clock())
            task = self._tasks.get(request_id)
            if task is None:
                return self._expired_view(request_id)
            if task.status not in TERMINAL_STATUSES:
                now = self._clock()
                if now - task.created_at > self._max_life_s:
                    if self._local:
                        job = self._local_jobs.pop(request_id, None)
                        if job is not None:
                            job.cancel()
                    else:
                        await self._cancel_helper(task)
                    self._settle(task, "expired", error_class="max_life")
                elif not self._local:
                    reply = await host_runner.pick_status(request_id=task.helper_id)
                    self._apply(task, reply, created=False)
            return self._view(task)

    async def cancel(self, request_id: str) -> dict[str, Any]:
        """取消任务（关掉同一个系统窗口；幂等 ✓ §4.3）。"""

        async with self._lock:
            self._sweep(self._clock())
            task = self._tasks.get(request_id)
            if task is None:
                return self._expired_view(request_id)
            if task.status not in TERMINAL_STATUSES:
                if self._local:
                    job = self._local_jobs.pop(request_id, None)
                    if job is not None:
                        job.cancel()
                    self._settle(task, "canceled", error_class="user_canceled", force=True)
                    return self._view(task)
                await self._cancel_helper(task)
                self._settle(task, "canceled", error_class="user_canceled", force=True)
            return self._view(task)

    # -- 内部 -------------------------------------------------------------- #
    def _active(self, now: float) -> PickTask | None:
        for task in self._tasks.values():
            if task.status not in TERMINAL_STATUSES and now - task.created_at <= self._max_life_s:
                return task
        return None

    def _sweep(self, now: float) -> None:
        for key, task in list(self._tasks.items()):
            if task.expires_at and now > task.expires_at:
                self._tasks.pop(key, None)

    async def _cancel_helper(self, task: PickTask) -> None:
        if not task.helper_id:
            return
        await host_runner.pick_cancel(request_id=task.helper_id)

    def _settle(
        self,
        task: PickTask,
        status: str,
        *,
        error_class: str = "",
        path: str = "",
        force: bool = False,
    ) -> None:
        """收敛到终态。**终态只写一次**：迟到的执行器结果不能改写已定终态 ✓ §4.2。"""

        if task.status in TERMINAL_STATUSES and not force:
            return
        if status == "selected" and not path:
            status = "canceled"
        task.status = status
        task.path = path if status == "selected" else ""
        task.message = MESSAGES.get(status, MESSAGES["failed"])
        task.error_class = error_class
        task.updated_at = self._clock()
        task.expires_at = task.updated_at + self._ttl_s

    def _apply(self, task: PickTask, reply: dict[str, Any], *, created: bool) -> None:
        """把执行器的回包落到任务上（含"执行器不认识新协议 / 连不上"的降级）。"""

        if not isinstance(reply, dict):
            self._settle(task, "failed", error_class="bad_reply")
            return
        if reply.get("auth_failed") or reply.get("unknown_route") or reply.get("unreachable"):
            self._settle(task, "unavailable", error_class="executor_unavailable")
            return
        if reply.get("outcome_unknown"):
            # 受理结果未知：**绝不自动重放**；如实报"故障可重试"，让用户再点一次 ✓ §5.3
            self._settle(task, "failed", error_class="outcome_unknown")
            return

        helper_id = str(reply.get("request_id") or "")
        if helper_id:
            task.helper_id = helper_id

        mapped = HELPER_STATUS_MAP.get(str(reply.get("status") or ""))
        if mapped is None:
            # 老版本执行器（或容器执行环境）：响应里没有新协议的 status
            if reply.get("supported") is False:
                self._settle(task, "unavailable", error_class="no_desktop")
            elif created and reply.get("ok") is False:
                self._settle(task, "unavailable", error_class="executor_refused")
            elif created:
                self._settle(task, "failed", error_class="protocol_mismatch")
            return

        if mapped == "pending":
            task.updated_at = self._clock()
            return
        self._settle(
            task,
            mapped,
            error_class=str(reply.get("error_class") or ""),
            path=str(reply.get("path") or ""),
        )

    def _view(
        self,
        task: PickTask,
        *,
        accepted: bool = True,
        busy: bool = False,
        created: bool = False,
    ) -> dict[str, Any]:
        return {
            "request_id": task.request_id,
            "status": task.status,
            "path": task.path or None,
            "canceled": task.status == "canceled",
            "message": task.message,
            "accepted": accepted,
            "busy": busy,
            "created": created,
        }

    def _expired_view(self, request_id: str) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "status": "expired",
            "path": None,
            "canceled": False,
            "message": MESSAGES["expired"],
            "accepted": False,
            "busy": False,
            "created": False,
        }


_PICKER: FolderPickerService | None = None
_LOCAL_PICKER: FolderPickerService | None = None


def get_picker(*, local: bool = False) -> FolderPickerService:
    """进程级单例（任务表跟着进程走；重启即清空，前端会读到 `expired` 并允许重试）。"""

    global _PICKER, _LOCAL_PICKER
    if local:
        if _LOCAL_PICKER is None:
            _LOCAL_PICKER = FolderPickerService(local=True)
        return _LOCAL_PICKER
    if _PICKER is None:
        _PICKER = FolderPickerService()
    return _PICKER


def reset_picker() -> None:
    """测试用：丢掉当前单例。"""

    global _PICKER, _LOCAL_PICKER
    _PICKER = None
    _LOCAL_PICKER = None
