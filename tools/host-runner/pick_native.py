#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""用 Windows **原生**文件夹对话框（`IFileOpenDialog` + `FOS_PICKFOLDERS`）选目录。

为什么不用 PowerShell（2026-09-26 实测）：
    起一个最简 PowerShell 就要 **2.4 秒**，再加"抢前台"那段 `Add-Type` 编译 C# 要 **3.4 秒** ✗
    —— 研究者点一下按钮要等三秒才看到窗口，不合理。
执行器**本来就是 Python 进程**，直接调 COM 就好：没有新起进程，实测几十毫秒级。

2026-10-11 修的三件事（都是真机踩出来的）：
1. **乱码**：Windows 上 `sys.stdin/stdout` 默认按**本机代码页**（中文系统 cp936）编解码，
   而管道两端约定 UTF-8 → 标题经 stdin 进本模块时已经不是原来的字了 ✗
   （原生窗口标题因此显示成乱码）。现在强制把标准流重挂成 UTF-8。
2. **关闭**：看门狗原来在旁观线程里直接调 `IFileDialog::Close` —— **跨线程操作 COM 不安全** ✗。
   现在先 `QueryInterface(IOleWindow)` 取窗口句柄，再 `PostMessage(hwnd, WM_CLOSE)`，
   由对话框**自己的消息循环**消费：同线程、安全、可复现。
3. **无桌面**：先探一次可交互桌面，没有就**立刻**返回 `unavailable`（不再干等到超时）。

⚠️ 调 COM 最容易错的是 **vtable 序号**（点错了可能**整个进程崩** ✗，不是抛异常 ✗）。
所以：这个模块**单独可测**（`python pick_native.py` 直接试弹）；任何失败都返回结构化结果，
由调用方决定退回哪种实现 ✓。
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Any

CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}"
IID_IFileOpenDialog = "{d57c7288-d4ad-4768-be02-9d969532d960}"
IID_IOleWindow = "{00000114-0000-0000-C000-000000000046}"

#: 只让选文件夹 / 只允许文件系统里的东西
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040

SIGDN_FILESYSPATH = 0x80058000
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

#: IUnknown
VT_QUERY_INTERFACE = 0
VT_RELEASE = 2
#: IFileDialog 的 vtable 序号（IUnknown 占 0/1/2）
VT_SHOW = 3
VT_SET_OPTIONS = 9
VT_SET_TITLE = 17
VT_GET_RESULT = 20
#: IFileDialog::Close（到点自己关，避免孤儿窗口）
VT_CLOSE = 23
#: IOleWindow::GetWindow（拿窗口句柄，用 PostMessage 关才安全）
VT_OLEWINDOW_GET_WINDOW = 3
#: IShellItem 的 GetDisplayName
VT_ITEM_GET_DISPLAY_NAME = 5

WM_CLOSE = 0x0010
#: OpenInputDesktop 需要的权限位（只要"能读对象"就够判断有没有可交互桌面）
DESKTOP_READOBJECTS = 0x0001

#: 状态取值（与执行器的任务状态机对齐）
STATUS_SELECTED = "selected"
STATUS_CANCELED = "canceled"
STATUS_TIMED_OUT = "timed_out"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"


def ensure_utf8_stdio() -> None:
    """把标准流重挂成 UTF-8。

    这是**标题乱码的根因**：Windows 的文本流默认用本机代码页（cp936）解码，
    而父进程按 UTF-8 写字节 → 进到这里已经错了。重挂之后两端才是同一套编码。
    """

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 流被替换过 / 已关闭
            continue


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    cleaned = text.strip("{}")
    parts = cleaned.split("-")
    guid = GUID()
    guid.Data1 = int(parts[0], 16)
    guid.Data2 = int(parts[1], 16)
    guid.Data3 = int(parts[2], 16)
    tail = parts[3] + parts[4]
    for index in range(8):
        guid.Data4[index] = int(tail[index * 2 : index * 2 + 2], 16)
    return guid


def _argtype(value: Any) -> Any:
    """把实参映射成 Win32 函数原型里的参数类型（指针实参要按指针声明）。"""

    if value is None:
        return ctypes.c_void_p
    if isinstance(value, ctypes.c_void_p):
        return ctypes.c_void_p
    return type(value)


def _vtable_call(pointer: Any, index: int, *args: Any) -> int:
    """按序号调 COM 接口方法（`vtbl[index]`）。"""

    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    argtypes = [ctypes.c_void_p] + [_argtype(item) for item in args]
    function = ctypes.WINFUNCTYPE(ctypes.c_long, *argtypes)(vtable[index])
    return function(pointer, *args)


def _release(pointer: Any) -> None:
    if not pointer:
        return
    try:
        _vtable_call(pointer, VT_RELEASE)  # IUnknown::Release
    except (ValueError, OSError):
        pass


def _interactive_desktop_available() -> bool:
    """这台机器 / 这个会话有没有**可交互的桌面**。

    没有（服务会话、SSH 无图形、Linux 容器）就别去建对话框了 ——
    建了也不会显示，只会让调用方干等到超时 ✗。
    """

    if not hasattr(ctypes, "windll"):
        return False
    try:
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not handle:
            return False
        user32.CloseDesktop(handle)
        return True
    except (AttributeError, OSError, ValueError):
        return False


def _post_close_window(hwnd: int) -> bool:
    """请对话框**自己**关闭：往它的窗口消息队列投一条 WM_CLOSE。

    这就是"安全的关闭方式"—— 跨线程直接调 COM 的 `Close()` 是不行的 ✗，
    而 `PostMessage` 之后由对话框自己的消息循环处理，等同于点右上角的 × ✓。
    """

    if not hwnd or not hasattr(ctypes, "windll"):
        return False
    try:
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        return bool(user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0))
    except (AttributeError, OSError, ValueError):
        return False


def _dialog_window_handle(dialog: Any) -> int:
    """取对话框的窗口句柄（先 QueryInterface 到 IOleWindow，再 GetWindow）。"""

    ole_window = ctypes.c_void_p()
    iid = _guid(IID_IOleWindow)
    try:
        if _vtable_call(dialog, VT_QUERY_INTERFACE, ctypes.pointer(iid), ctypes.pointer(ole_window)) != 0:
            return 0
        if not ole_window:
            return 0
        handle = ctypes.c_void_p()
        if _vtable_call(ole_window, VT_OLEWINDOW_GET_WINDOW, ctypes.pointer(handle)) != 0:
            return 0
        return int(handle.value or 0)
    except (ValueError, OSError):
        return 0
    finally:
        _release(ole_window)


def pick_folder_native(
    *,
    title: str = "选择文件夹",
    timeout_s: int = 600,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """弹出原生文件夹对话框（**必须在会被一直持有的专用线程里调用**）。

    返回**结构化结果**（别用 `None`/`""` 混着表示失败与取消 ✗）：
    `{"ok", "status", "path", "canceled", "timed_out", "unsupported", "error"}`。

    关闭有两条路，都走 `PostMessage(WM_CLOSE)`（安全、不跨线程碰 COM）：
    - 到点没人选（`timeout_s`）→ `status="timed_out"`；
    - 外部 `cancel_event` 被置位（用户在网页上取消）→ `status="canceled"`。
    """

    if not hasattr(ctypes, "oledll"):  # 非 Windows
        return {
            "ok": False,
            "status": STATUS_UNAVAILABLE,
            "unsupported": True,
            "shown": False,
            "error": "这个平台没有原生文件夹对话框",
        }
    if not _interactive_desktop_available():
        return {
            "ok": False,
            "status": STATUS_UNAVAILABLE,
            "unsupported": True,
            "shown": False,
            "error": "当前会话没有可交互的桌面，打不开系统选择窗口。",
        }

    try:
        ole32 = ctypes.oledll.ole32
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except OSError:
        # 已经在别的单元里初始化过也会失败，不影响后面的调用
        pass

    dialog = ctypes.c_void_p()
    clsid = _guid(CLSID_FileOpenDialog)
    iid = _guid(IID_IFileOpenDialog)
    try:
        ole32.CoCreateInstance(
            ctypes.pointer(clsid), None, CLSCTX_INPROC_SERVER, ctypes.pointer(iid), ctypes.pointer(dialog)
        )
    except OSError as exc:
        return {
            "ok": False,
            "status": STATUS_UNAVAILABLE,
            "unsupported": True,
            "shown": False,
            "error": f"建对话框失败：{exc}",
        }
    if not dialog:
        return {
            "ok": False,
            "status": STATUS_UNAVAILABLE,
            "unsupported": True,
            "shown": False,
            "error": "建对话框失败",
        }

    watchdog = threading.Thread(target=lambda: None)
    stop_watchdog = threading.Event()
    closed_by_watchdog = threading.Event()
    closed_by_cancel = threading.Event()
    item = ctypes.c_void_p()
    try:
        if _vtable_call(dialog, VT_SET_OPTIONS, ctypes.c_uint32(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)) != 0:
            return {"ok": False, "status": STATUS_FAILED, "error": "设置「只选文件夹」选项失败", "unsupported": True}
        if _vtable_call(dialog, VT_SET_TITLE, ctypes.c_wchar_p(title)) != 0:
            return {"ok": False, "status": STATUS_FAILED, "error": "设置窗口标题失败"}

        handle = _dialog_window_handle(dialog)
        deadline = time.monotonic() + max(1, int(timeout_s))

        def watch() -> None:
            while not stop_watchdog.wait(0.05):
                if cancel_event is not None and cancel_event.is_set():
                    if _post_close_window(handle):
                        closed_by_cancel.set()
                    return
                if time.monotonic() >= deadline:
                    if _post_close_window(handle):
                        closed_by_watchdog.set()
                    return

        watchdog = threading.Thread(target=watch, name="pick-watchdog", daemon=True)
        watchdog.start()

        outcome = _vtable_call(dialog, VT_SHOW, ctypes.c_void_p(None))
        stop_watchdog.set()

        if closed_by_cancel.is_set():
            return {"ok": True, "status": STATUS_CANCELED, "shown": True, "path": "", "canceled": True, "timed_out": False}
        if closed_by_watchdog.is_set():
            return {
                "ok": False,
                "status": STATUS_TIMED_OUT,
                "shown": True,
                "timed_out": True,
                "error": f"等了 {timeout_s} 秒还没选，已关闭选择框。",
            }
        if outcome != 0:  # 用户按了取消 / 关掉窗口（HRESULT = ERROR_CANCELLED）
            return {"ok": True, "status": STATUS_CANCELED, "shown": True, "path": "", "canceled": True, "timed_out": False}
        if _vtable_call(dialog, VT_GET_RESULT, ctypes.pointer(item)) != 0 or not item:
            return {"ok": False, "status": STATUS_FAILED, "shown": True, "error": "拿不到选择结果"}
        buffer = ctypes.c_wchar_p()
        if (
            _vtable_call(item, VT_ITEM_GET_DISPLAY_NAME, ctypes.c_uint32(SIGDN_FILESYSPATH), ctypes.pointer(buffer))
            != 0
        ):
            return {"ok": False, "status": STATUS_FAILED, "shown": True, "error": "拿不到选择结果的路径"}
        path = buffer.value or ""
        try:
            ctypes.oledll.ole32.CoTaskMemFree(buffer)
        except (OSError, ValueError):
            pass
        if not path:
            return {"ok": True, "status": STATUS_CANCELED, "shown": True, "path": "", "canceled": True, "timed_out": False}
        return {"ok": True, "status": STATUS_SELECTED, "shown": True, "path": path, "canceled": False, "timed_out": False}
    except (OSError, ValueError, TypeError) as exc:  # 任何失败都交回去（绝不把失败当"用户取消" ✗）
        return {"ok": False, "status": STATUS_FAILED, "unsupported": True, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        stop_watchdog.set()
        if watchdog.is_alive():
            watchdog.join(timeout=0.5)
        _release(item)
        _release(dialog)


if __name__ == "__main__":
    # 独立入口：直接试弹一次（排障 / 真机验收用）
    import argparse
    import json

    ensure_utf8_stdio()

    parser = argparse.ArgumentParser(description="弹原生文件夹选择框，JSON 回话")
    parser.add_argument("--title", default="选择文件夹")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    outcome = pick_folder_native(title=args.title, timeout_s=args.timeout)
    sys.stdout.write(json.dumps(outcome, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    raise SystemExit(0 if os.name == "nt" else 1)
