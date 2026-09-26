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

⚠️ 调 COM 最容易错的是 **vtable 序号**（点错了可能**整个进程崩** ✗，不是抛异常 ✗）。
所以：这个模块**单独可测**（`python pick_native.py` 直接试弹），执行器只在它成功时才用；
任何异常/失败都 `return None`，由调用方退回原路径 ✓。
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Any

CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}"
IID_IFileOpenDialog = "{d57c7288-d4ad-4768-be02-9d969532d960}"

#: 只让选文件夹 / 只允许文件系统里的东西
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040

SIGDN_FILESYSPATH = 0x80058000
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

#: IFileDialog 的 vtable 序号（IUnknown 占 0/1/2）
VT_SHOW = 3
VT_SET_OPTIONS = 9
VT_SET_TITLE = 17
VT_GET_RESULT = 20
#: IFileDialog::Close（到点自己关，避免孤儿窗口）
VT_CLOSE = 23
#: IShellItem 的 GetDisplayName
VT_ITEM_GET_DISPLAY_NAME = 5


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


def _vtable_call(pointer: Any, index: int, *args: Any) -> int:
    """按序号调 COM 接口方法（vtbl[index]）。"""

    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *[type(item) for item in args])(
        vtable[index]
    )
    return function(pointer, *args)


def _release(pointer: Any) -> None:
    try:
        _vtable_call(pointer, 2)  # IUnknown::Release
    except Exception:  # noqa: BLE001
        pass


def pick_folder_native(*, title: str = "选择文件夹", timeout_s: int = 600) -> dict[str, Any]:
    """弹出原生文件夹对话框。

    返回**结构化结果**（别用 `None`/`""` 混着表示失败与取消 ✗）：
    `{"ok": bool, "path": str, "canceled": bool, "timed_out": bool, "error": str}`；
    失败时 `ok=False` 且 `unsupported=True`，调用方据此退回别的实现 ✓。

    到点没人选 → 看门狗线程调 `Close()` 把窗口关掉（不留孤儿 ✓）。
    """

    if not hasattr(ctypes, "oledll"):  # 非 Windows
        return {"ok": False, "unsupported": True, "error": "这个平台没有原生文件夹对话框"}

    try:
        ole32 = ctypes.oledll.ole32
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except Exception:  # noqa: BLE001 - 已经在别的单元里初始化过也会失败，不影响
        pass

    dialog = ctypes.c_void_p()
    clsid = _guid(CLSID_FileOpenDialog)
    iid = _guid(IID_IFileOpenDialog)
    try:
        ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER, ctypes.byref(iid), ctypes.byref(dialog)
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "unsupported": True, "error": f"建对话框失败：{exc}"}
    if not dialog:
        return {"ok": False, "unsupported": True, "error": "建对话框失败"}

    state: dict[str, Any] = {"timed_out": False}
    watchdog: threading.Timer | None = None
    item = ctypes.c_void_p()
    try:
        _vtable_call(dialog, VT_SET_OPTIONS, ctypes.c_uint32(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM))
        _vtable_call(dialog, VT_SET_TITLE, ctypes.c_wchar_p(title))

        def close_it() -> None:
            state["timed_out"] = True
            try:
                _vtable_call(dialog, VT_CLOSE)  # IFileDialog::Close
            except Exception:  # noqa: BLE001
                pass

        watchdog = threading.Timer(timeout_s, close_it)
        watchdog.daemon = True
        watchdog.start()

        outcome = _vtable_call(dialog, VT_SHOW, ctypes.c_void_p(None))
        if watchdog is not None:
            watchdog.cancel()

        if state["timed_out"]:
            return {"ok": False, "shown": True, "timed_out": True, "error": f"等了 {timeout_s} 秒还没选，已关闭选择框。"}
        if outcome != 0:  # 用户取消
            return {"ok": True, "shown": True, "path": "", "canceled": True, "timed_out": False}
        if _vtable_call(dialog, VT_GET_RESULT, ctypes.byref(item)) != 0 or not item:
            return {"ok": False, "unsupported": True, "error": "拿不到选择结果"}
        buffer = ctypes.c_wchar_p()
        if _vtable_call(item, VT_ITEM_GET_DISPLAY_NAME, ctypes.c_uint32(SIGDN_FILESYSPATH), ctypes.byref(buffer)) != 0:
            return {"ok": False, "unsupported": True, "error": "拿不到选择结果的路径"}
        path = buffer.value or ""
        try:
            ctypes.oledll.ole32.CoTaskMemFree(buffer)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "shown": True, "path": path, "canceled": not path, "timed_out": False}
    except Exception as exc:  # noqa: BLE001 - 任何失败都交回去（绝不把失败当"用户取消"✗）
        return {"ok": False, "unsupported": True, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if watchdog is not None:
            watchdog.cancel()
        _release(item)
        _release(dialog)


if __name__ == "__main__":
    # 独立子进程入口：执行器 spawn 它来弹框（卡也只卡它自己，超时直接杀 ✓）
    import argparse
    import json

    parser = argparse.ArgumentParser(description="弹原生文件夹选择框，JSON 回话")
    parser.add_argument("--title", default="选择文件夹")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    outcome = pick_folder_native(title=args.title, timeout_s=args.timeout)
    print(json.dumps(outcome, ensure_ascii=False))
