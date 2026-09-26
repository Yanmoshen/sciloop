#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SciLoop 宿主执行器 —— 在你自己的电脑上跑命令、读写文件的那只手。

为什么需要它
------------
后端跑在容器里，而容器只能看到"挂进去"的目录。要支持"agent 能访问这台机器上
任何你有权限的目录"（还要能满足别人把自己电脑部署成服务器的情况），就必须有一个
**跑在宿主机上的**执行进程 —— 就是本脚本。它只做两件事：

1. 按请求在你的电脑上执行命令；
2. 按请求读写/列出/移动/删除文件。

安全边界（**故意做得很硬**）
--------------------------
- **只监听本机回环地址**（127.0.0.1），外网/局域网连不上；
- 每次启动生成一枚**一次性握手密钥**，写进 `~/.sciloop/host-runner.json`（仅当前用户可读）；
  后端必须带对密钥才受理；没带或带错一律 401；
- **本脚本自己不做业务判断**（哪些算高危、哪些目录不准动，由后端那层按你定的规则判）。
  这里只保证两件底线：① 拒绝把密钥文件本身删掉；② 拒绝把执行器的监听地址暴露到非回环。

启动（就这一条命令）
-------------------
    python tools/host-runner/host_runner.py

首次启动会把密钥打印出来（也会写进上面那个文件），把密钥填进 SciLoop 的设置页即可。
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "0.2.2"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT_S = 120
MAX_OUTPUT_CHARS = 200_000
STATE_DIR = Path.home() / ".sciloop"
STATE_FILE = STATE_DIR / "host-runner.json"

#: 本脚本所在的 SciLoop 代码树根（`<repo>/tools/host-runner/host_runner.py` → `<repo>`）。
#: 它会随 `/health` 一起报给后端 —— 后端跑在容器里、只知道容器视角的路径，
#: 必须由执行器告诉它"宿主上这个目录是哪儿"，否则"删代码=硬拒"这条边界会失效。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 研究工作项目的默认根目录（新建项目时在这里建目录；用户选本地文件夹时不受它限制）
PROJECT_ROOT = REPO_ROOT / "research-workspaces"

#: **执行器不自己生成密钥**（研究者 2026-09-22 明确要求：「不要做执行器密钥，
#: 不要搞那么多密钥，只要 owner 密钥就够了」）—— 它直接用项目 `.env` 里那枚
#: Owner 令牌：优先环境变量，其次读 `.env`。
OWNER_TOKEN_ENV = "SCILOOP_OWNER_TOKEN"
ENV_FILE = REPO_ROOT / ".env"


def read_owner_token() -> str:
    """取 Owner 令牌：环境变量 → 项目 `.env`（**必须剥掉行内注释**）。

    ⚠️ 踩过的坑：`.env` 写成 `OWNER_TOKEN=abc  # 说明` 时，不剥注释会把中文注释
    一起当进令牌值里 → 比较时抛 `TypeError: comparing strings with non-ASCII characters`，
    表现为接口 500（看着像鉴权坏了，其实是值里带了中文）。
    """

    from_env = (os.environ.get(OWNER_TOKEN_ENV) or "").strip()
    if from_env:
        return from_env
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != "OWNER_TOKEN":
                continue
            value = value.split("#", 1)[0].strip().strip('"').strip("'")
            return value
    except OSError:
        return ""
    return ""


#: 研究者的**科研解释器**：跑命令时把它的目录提到 PATH 最前。
HOST_PYTHON_ENV = "SCILOOP_HOST_PYTHON"


def read_env_value(key: str) -> str:
    """按项目 `.env` 的写法取值（**必须剥行内注释** —— 与 `read_owner_token` 同口径）。"""

    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() != key:
                continue
            return value.split("#", 1)[0].strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def host_python() -> str:
    r"""研究者电脑上「跑命令时该用哪个 Python」：环境变量 → 项目 `.env` → 空（空＝不改 PATH）。

    为什么要有它（2026-09-26 研究者报的「结论二」真根因之一）：
    执行器是**恰好被哪个 Python 拉起就用哪个** —— 实测它被工具链自带的托管解释器拉起
    （`…\workbuddy\binaries\python\…`），那里面**没有 numpy / scipy / torch**，
    于是模型一问「本机有没有这些库」必然得到「没有」，把**明明能做的预试验判成做不了**。
    让研究者指定一个，模型探到的才是他真实的科研环境。

    ⚠️ 只影响**子进程的 PATH**（等价于"先激活这个环境"），不改执行器自己的解释器，
    也不动全局配置 —— 不想要就把 `.env` 里那行删掉，行为立刻回到原样。
    """

    raw = (os.environ.get(HOST_PYTHON_ENV) or "").strip() or read_env_value(HOST_PYTHON_ENV)
    path = raw.strip().strip('"').strip("'")
    return path if path and Path(path).is_file() else ""


def python_path_prefix() -> list[str]:
    """解释器所在目录 + conda 的 `Library\bin` / `Scripts` / `DLLs`（有才加）。"""

    exe = host_python()
    if not exe:
        return []
    home = Path(exe).parent
    dirs = [str(home)]
    for extra in ("Library/bin", "Library/usr/bin", "Scripts", "DLLs"):
        candidate = home / extra
        if candidate.is_dir():
            dirs.append(str(candidate))
    return dirs


def read_configured_port() -> int | None:
    """从 `.env` 的 `SCILOOP_HOST_RUNNER_URL` 里读端口 —— 让「就这一条命令」名副其实。

    ⚠️ 为什么必须读它（2026-09-26 查到的真根因）：`.env` 指向 **8766**
    （9-23 因为 8765 上有个半死旧进程才换过去），而本脚本的 `DEFAULT_PORT` 是 **8765**。
    照文档跑那一句 `python tools/host-runner/host_runner.py`，执行器就起在 8765，
    后端去 8766 找不到人 → 界面永远「还没连上这台电脑的执行器」，
    只有手工加 `--port 8766` 才对得上。这就是这个坑反复出现的原因。
    配置与行为必须同源：令牌已经是从 `.env` 读的（`read_owner_token`），端口也一样读。

    解析口径与 `read_owner_token` 一致（**必须剥行内注释**，否则端口后面挂的说明会污染取值）。
    """

    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != "SCILOOP_HOST_RUNNER_URL":
                continue
            value = value.split("#", 1)[0].strip().strip('"').strip("'")
            tail = value.rsplit(":", 1)[-1].split("/", 1)[0].strip()
            if tail.isdigit():
                candidate = int(tail)
                if 1 <= candidate <= 65535:
                    return candidate
    except OSError:
        return None
    return None


# --------------------------------------------------------------------------- #
# 状态：只有端口与版本（**不再存任何密钥**）
# --------------------------------------------------------------------------- #
def load_or_create_state(port: int) -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    # 老版本在这个文件里存过自生成密钥 —— 就地清掉，避免"到底哪枚密钥才算数"的混乱
    state.pop("token", None)
    state["port"] = port
    state["version"] = VERSION
    state["python_env"] = host_python() or "(未指定，沿用环境默认)"
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass
    return state


# --------------------------------------------------------------------------- #
# 两个能力：跑命令 / 动文件
# --------------------------------------------------------------------------- #
def run_command(
    *,
    argv: list[str] | None = None,
    command: str | None = None,
    cwd: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    env_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """执行一条命令，返回真实退出码与输出（**不美化、不伪造**）。"""

    use_shell = False
    if argv:
        args = list(argv)
    elif command:
        # ⚠️ 2026-09-26 修：原来 Windows 上是 `shlex.split(command, posix=False)` ——
        # **引号会被原样保留**，`python -c "print(1)"` 到程序手里成了**带引号的字符串**
        # （Python 把它当字符串字面量求值：不报错、不打印、退出码 0），
        # 而且 `&&` / 管道 / 重定向这些 shell 语义**全部失效**（不是 shell 在跑）。
        # 后果实测：模型连续几次拿到"退出码 0 + 输出为空"，于是写下
        # 「本机 numpy / scipy / torch / transformers 全部不可用」这种**错误硬结论**，
        # 把能做的事判成做不了。命令字符串本来就该交给**平台 shell** 解析 ——
        # 研究者与模型写的都是 shell 语法，不是 argv 数组。
        # ⚠️ 必须走 `shell=True` 而不是手工拼 ["cmd", "/c", command]：
        # Windows 上 subprocess 会把 list 形式的参数交给 list2cmdline 再转义一次，
        # 内层引号会变成 \"（cmd 不认），于是 `python -c "print(1)"` **依然是空输出** ✗
        # （2026-09-26 实测：拼 cmd 只修好了 `&&`，带引号的仍然丢输出）。
        # 直接把命令**字符串**交给平台 shell，引号与元字符才按原样生效。
        use_shell = True
        args = command
    else:
        return {"ok": False, "error": "既没有 argv 也没有 command"}

    workdir = cwd or str(Path.home())
    if not Path(workdir).is_dir():
        return {"ok": False, "error": f"工作目录不存在：{workdir}"}

    env = dict(os.environ)
    # 让子进程的 `python` 指向研究者的科研环境（等价于"先激活环境"）：
    # 不这么做，模型探到的是"恰好拉起执行器的那个解释器"，结论会离谱（见 host_python 注释）。
    prefix = python_path_prefix()
    if prefix:
        env["PATH"] = os.pathsep.join([*prefix, env.get("PATH", "")])
        env[HOST_PYTHON_ENV] = host_python()
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    # ⚠️ Windows 上必须显式给子进程一个**自己的（隐藏）控制台**。
    # 实测坑：本执行器常被"没有交互式控制台"的进程拉起（计划任务 / 被别的程序 spawn），
    # 这时再 spawn 控制台程序（git、python、whoami、连 `cmd /c ver`）会直接
    # 以 0xC0000142（DLL 初始化失败）退出 —— 退出码看着像崩溃，实际是控制台继承不到。
    # CREATE_NO_WINDOW = 新建一个不可见控制台，正好治这个。
    extra: dict[str, Any] = {}
    if os.name == "nt":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    started = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603 - 这是执行器的本职
            args,
            shell=use_shell,
            cwd=workdir,
            env=env,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
            **extra,
        )
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "stdout": _clip(_decode(exc.stdout or b"")),
            "stderr": _clip(_decode(exc.stderr or b"")),
            "error": f"超过 {timeout_s}s 未结束，已终止",
            "workdir": workdir,
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"无法启动进程：{exc}", "workdir": workdir}

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "stdout": _clip(_decode(proc.stdout or b"")),
        "stderr": _clip(_decode(proc.stderr or b"")),
        "workdir": workdir,
    }


# ---------------------------------------------------------------------------------------
# 常驻弹框助手（2026-09-26）
#
# 为什么要有它：研究者点「选择文件夹」时，走 PowerShell 要等 2.4–3.4 秒（起进程 + 现场
# 编译 C#），走一次性子进程要 ~18 秒（每次重新预热 COM）—— 点一下等三秒以上不合理。
# 常驻助手把预热做在前面，点击时只发一行 JSON，窗口几乎立刻出现；
# **顺带**：助手是无控制台的子进程，所以不会再闪出 PowerShell 黑框。
#
# 失败一律回落：助手起不来 / 中途崩了 / 回话超时 → 返回 None，`pick_folder` 照旧走原路径 ✓
# ---------------------------------------------------------------------------------------
_PICK_ASSISTANT: dict[str, Any] = {"proc": None, "lock": threading.Lock()}


def _start_pick_assistant() -> subprocess.Popen[bytes] | None:
    """启动常驻助手（只在 Windows 且原生模块存在时）；失败返回 None。"""
    if not sys.platform.startswith("win"):
        return None
    script = Path(__file__).with_name("pick_native.py")
    if not script.is_file():
        return None
    try:
        return subprocess.Popen(  # noqa: S603 - 弹系统对话框是本模块的本职
            [sys.executable, str(script), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except OSError:
        return None


def _discard_pick_assistant(proc: subprocess.Popen[bytes] | None) -> None:
    """丢弃一个不正常的助手（杀掉 + 归零；下次请求会重新起一个）。返回值恒为 None。"""
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
    return None


def _readline_within(proc: subprocess.Popen[bytes], timeout_s: float) -> str | None:
    """在独立线程里读一行；超时返回 None（`readline` 本身没法中断）。"""
    box: dict[str, Any] = {"line": None}

    def worker() -> None:
        try:
            raw = proc.stdout.readline() if proc.stdout is not None else b""
        except Exception:  # noqa: BLE001 - 助手没了就是没结果
            raw = b""
        box["line"] = raw.decode("utf-8", errors="replace").strip() if isinstance(raw, bytes) else None

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return None
    return box["line"] or None


def pick_folder_via_assistant(*, title: str, timeout_s: int) -> dict[str, Any] | None:
    """让常驻助手弹框；**任何异常/超时都返回 None**，由调用方回落原路径。"""
    with _PICK_ASSISTANT["lock"]:
        proc = _PICK_ASSISTANT.get("proc")
        if proc is not None and proc.poll() is not None:
            proc = None  # 助手已经退出（崩了 / 被关掉）
        if proc is None:
            proc = _start_pick_assistant()
            _PICK_ASSISTANT["proc"] = proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            return None
        request = json.dumps(
            {"cmd": "pick", "title": title, "timeout_s": int(timeout_s)}, ensure_ascii=False
        )
        try:
            proc.stdin.write((request + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            _PICK_ASSISTANT["proc"] = _discard_pick_assistant(proc)
            return None
        # 助手自己带对话框超时，这里再留 30 秒余量；仍读不到 → 助手不正常，丢弃回落
        line = _readline_within(proc, float(timeout_s) + 30)
        if line is None:
            _PICK_ASSISTANT["proc"] = _discard_pick_assistant(proc)
            return None
        try:
            payload = json.loads(line)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def pick_folder(*, title: str = "选择文件夹", initial: str = "", timeout_s: int = 600) -> dict[str, Any]:
    """弹出**系统自带**的文件夹选择框，返回研究者选中的绝对路径。

    - Windows：`System.Windows.Forms.FolderBrowserDialog`（就是资源管理器那种选择框）；
    - macOS：`osascript` 的 `choose folder`；Linux：`zenity`（没装就如实说"这台机器弹不出来"）。

    2026-09-26 修的三件事（都是真机踩出来的）：
    1. **强制置顶**：没有 owner 的对话框常被浏览器盖住，研究者看不到 → 用不可见置顶窗体当 owner，
       弹完再抢一次前台；否则"点了没反应"（实测有一次 0.3 秒就返回 Cancel ✗）。
    2. **超时自动关**：用 WinForms 计时器到点关闭，**不留孤儿窗口**
       （`subprocess.run(timeout=)` 只杀直接子进程，窗口会残留 ✗）。
    3. **如实区分**：`canceled`（弹出来了、人取消）/ `timed_out`（弹出来了、人没点）/
       `shown=False`（**压根没弹出来**）—— 原来第三种被报成"没有选择文件夹"，是假消息。
    """

    timeout_s = max(5, min(int(timeout_s or 600), 3600))

    # 2026-09-26：Windows 上**先让独立子进程去弹原生对话框**。
    # 为什么不就在本进程里弹：COM 模态框在本线程 Show() 时，别的线程 Close() 解不开它 ✗
    #   → 请求会一直挂着（客户端 22 秒超时）+ 每次留一个卡死的窗口 ✗（实测踩过）。
    # 子进程方案：快约一倍（~1 秒 vs PowerShell 的 2.4–3.4 秒），卡也只卡它自己，
    #   超时由 subprocess.run 杀掉 → 窗口随进程消失（孤儿问题一并解决 ✓）。
# 实测：子进程跑原生 COM 要 ~18 秒 ✗（比旧路径的 2.4–3.4 秒更差），这里不用它；
# 原生模块 pick_native.py 留着，等以后做“常驻助手进程”时再用。
    if sys.platform.startswith("win"):
        # 先试**常驻助手**（快 + 不会闪黑框）；拿不到就回落下面的 PowerShell 路径
        assisted = pick_folder_via_assistant(title=title, timeout_s=timeout_s)
        if assisted is not None:
            return assisted
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            # 抢前台的助手（ShowDialog 会阻塞，只能在消息循环里抢）
            "Add-Type @'\n"
            "using System;\n"
            "using System.Runtime.InteropServices;\n"
            "public class FG {\n"
            "  [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);\n"
            "  [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int c);\n"
            "}\n"
            "'@; "
            # owner：**不可见但已显示**的置顶窗体（最小化的给不了前台 ✗，2026-09-26 实测）
            "$owner = New-Object System.Windows.Forms.Form; "
            "$owner.ShowInTaskbar = $false; $owner.FormBorderStyle = 'None'; "
            "$owner.Opacity = 0; $owner.TopMost = $true; $owner.Size = New-Object System.Drawing.Size(1,1); "
            "$owner.StartPosition = 'CenterScreen'; $owner.Show(); $owner.Activate(); "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$d.Description = {json.dumps(title, ensure_ascii=False)}; "
            "$d.ShowNewFolderButton = $true; "
            "$d.StartPosition = 'CenterScreen'; "
            + (f"$d.SelectedPath = {json.dumps(initial, ensure_ascii=False)}; " if initial else "")
            + "$hits = 0; "
            # 到点自动关（不留孤儿窗口）
            "$timer = New-Object System.Windows.Forms.Timer; "
            f"$timer.Interval = {timeout_s * 1000}; "
            "$timer.Add_Tick({ $script:hits = 1; $d.Dispose(); $owner.Close() }); "
            "$timer.Start(); "
            # 200ms 后主动把对话框抢到前台（抢不到也不算失败，只是可能被盖住）
            "$fg = New-Object System.Windows.Forms.Timer; "
            "$fg.Interval = 200; "
            "$fg.Add_Tick({ try { [void][FG]::ShowWindow($d.Handle, 5); [void][FG]::SetForegroundWindow($d.Handle) } catch {} $fg.Stop() }); "
            "$fg.Start(); "
            "$sw = [System.Diagnostics.Stopwatch]::StartNew(); "
            "$r = $d.ShowDialog($owner); "
            "$sw.Stop(); $timer.Stop(); $fg.Stop(); "
            "$owner.Close(); "
            "Write-Output ('result=' + $r); "
            "Write-Output ('elapsed_ms=' + $sw.ElapsedMilliseconds); "
            "Write-Output ('picked=' + $d.SelectedPath); "
            "if ($hits -eq 1) { Write-Output 'timed_out=1' } else { Write-Output 'timed_out=0' }"
        )
        argv = ["powershell", "-NoProfile", "-STA", "-Command", script]
    elif sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{title}")'
        argv = ["osascript", "-e", script]
    else:
        argv = ["zenity", "--file-selection", "--directory", "--title", title]

    # Windows：**隐藏子进程自带的控制台窗口** —— 否则点「选择文件夹」会闪出一个
    # PowerShell 黑框（对话框是它弹的，那个黑框只是它的控制台副作用，用户不该看到）。
    # CREATE_NO_WINDOW 只掐掉控制台，不影响 WinForms 对话框本身的显示。
    run_extra: dict[str, Any] = {}
    if sys.platform.startswith("win"):
        run_extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout_s, check=False, **run_extra
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "supported": False,
            "shown": False,
            "error": "这台机器上没有可用的系统选择框（Windows 之外需要 osascript / zenity）。",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "supported": True,
            "shown": True,
            "timed_out": True,
            "error": f"等了 {timeout_s} 秒还没选，已关闭选择框。",
        }

    out = _decode(proc.stdout or b"")
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()

    elapsed_ms = int(fields["elapsed_ms"]) if fields.get("elapsed_ms", "").isdigit() else None
    timed_out = fields.get("timed_out") == "1"
    result = fields.get("result", "")

    if elapsed_ms is not None and elapsed_ms < 1000 and not fields.get("picked"):
        # 人不可能在一秒内点完：只能解释为"窗口没弹出来"（抢不到前台/无桌面会话）
        return {
            "ok": False,
            "supported": True,
            "shown": False,
            "elapsed_ms": elapsed_ms,
            "error": "系统选择框没能显示出来（这次连一秒都没到就返回了）—— 请把当前界面告诉我，我从执行器的桌面会话查。",
        }

    if timed_out:
        return {"ok": False, "supported": True, "shown": True, "timed_out": True, "elapsed_ms": elapsed_ms,
                "error": f"等了 {timeout_s} 秒还没选，已关闭选择框。"}

    picked = (fields.get("picked") or "").strip()
    if not picked:
        # 弹出来过（耗时正常）+ 没选 = 研究者取消了
        return {"ok": True, "supported": True, "shown": True, "path": "", "canceled": True, "elapsed_ms": elapsed_ms}
    return {
        "ok": True,
        "supported": True,
        "shown": True,
        "path": picked,
        "canceled": False if result else True,
        "elapsed_ms": elapsed_ms,
    }


def _pick_native_subprocess(*, title: str, timeout_s: int) -> dict[str, Any] | None:
    """让**独立子进程**去弹原生文件夹选择框（成功/取消/超时都给结构化结果）。

    失败（模块不在、解释器不对、输出看不懂）→ 返回 None，调用方退回 PowerShell 路径 ✓。
    """

    script = Path(__file__).with_name("pick_native.py")
    if not script.is_file():
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--title", title, "--timeout", str(timeout_s)],
            capture_output=True,
            timeout=timeout_s + 15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # 超时：杀掉之后窗口随进程消失（这正是子进程方案的好处 ✓）
        return {
            "ok": False,
            "shown": True,
            "timed_out": True,
            "error": f"等了 {timeout_s} 秒还没选，已关闭选择框。",
        }

    out = _decode(proc.stdout or b"").strip()
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                data.setdefault("shown", True)
                return data
    return None


def fs_action(*, action: str, path: str, to: str | None, content: str | None,
              encoding: str | None, recursive: bool) -> dict[str, Any]:
    """文件操作。删除**只在这里具备能力**，是否允许由后端按用户定的三层边界裁决。"""

    target = Path(path).expanduser()
    if action == "list":
        if not target.exists():
            return {"ok": False, "error": f"路径不存在：{path}"}
        if target.is_file():
            st = target.stat()
            return {
                "ok": True,
                "path": str(target),
                "kind": "file",
                "size": st.st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
            }
        entries = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                try:
                    st = child.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "kind": "dir" if child.is_dir() else "file",
                        "size": None if child.is_dir() else st.st_size,
                        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    }
                )
        except OSError as exc:
            return {"ok": False, "error": f"无法列出目录：{exc}"}
        return {"ok": True, "path": str(target), "kind": "dir", "children": entries[:2000]}

    if action == "read":
        if not target.is_file():
            return {"ok": False, "error": f"不是文件：{path}"}
        try:
            return {"ok": True, "path": str(target), "content": _clip(target.read_text(encoding=encoding or "utf-8", errors="replace"))}
        except OSError as exc:
            return {"ok": False, "error": f"读取失败：{exc}"}

    if action == "write":
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            existed = target.exists()
            target.write_text(content or "", encoding=encoding or "utf-8")
            return {"ok": True, "path": str(target), "overwrote": existed, "bytes": len(content or "")}
        except OSError as exc:
            return {"ok": False, "error": f"写入失败：{exc}"}

    if action == "mkdir":
        try:
            target.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "path": str(target), "created": True}
        except OSError as exc:
            return {"ok": False, "error": f"建目录失败：{exc}"}

    if action == "move":
        if not to:
            return {"ok": False, "error": "移动需要给目标路径"}
        try:
            dest = Path(to).expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            target.replace(dest)
            return {"ok": True, "path": str(target), "to": str(dest)}
        except OSError as exc:
            return {"ok": False, "error": f"移动失败：{exc}"}

    if action == "delete":
        # 底线：绝不删密钥文件本身（否则执行器会被自己搞失联）
        try:
            if target.resolve() == STATE_FILE.resolve():
                return {"ok": False, "error": "拒绝删除执行器自己的密钥文件"}
        except OSError:
            pass
        if not target.exists():
            return {"ok": False, "error": f"路径不存在：{path}"}
        try:
            if target.is_dir():
                if not recursive:
                    return {"ok": False, "error": "删除目录需要显式声明 recursive"}
                import shutil

                shutil.rmtree(target)
            else:
                target.unlink()
            return {"ok": True, "path": str(target), "deleted": True}
        except OSError as exc:
            return {"ok": False, "error": f"删除失败：{exc}"}

    return {"ok": False, "error": f"不支持的动作：{action}"}


def _say(text: str = "") -> None:
    """启动横幅专用输出：**行缓冲**。

    为什么单独包一层：Python 的 stdout 在"输出被重定向/非终端"时是**块缓冲**，
    启动横幅会一直不显示 —— 用户以为程序没跑起来（我自己就被骗过一次：
    后台启动的执行器其实活着，但一行输出都看不到）。这里强制 flush。
    """

    print(text, flush=True)


def _decode(raw: bytes) -> str:
    """解子进程输出。

    ⚠️ **不能一律按 UTF-8 解**：Windows 的 cmd/程序输出是本机代码页（中文系统是 GBK），
    一律 UTF-8 会把中文变成一串 `�`（2026-09-25 实测 `cmd /c ver`）。
    顺序：UTF-8 → 本机默认编码 → GBK → 兜底 replace（尽量别丢字）。
    """

    if not raw:
        return ""
    candidates = ["utf-8", locale.getpreferredencoding(False) or "", "gbk"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n…（输出过长，已截断，共 {len(text)} 字符）"


# --------------------------------------------------------------------------- #
# HTTP 层：只监听回环 + 密钥校验
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = f"SciLoopHostRunner/{VERSION}"
    token: str = ""
    started_at: float = 0.0

    # 默认的日志会把每条请求打到 stderr；保留一行简版，方便用户看"后端有没有连上来"
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("[host-runner] %s - %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

    # -- 工具 ------------------------------------------------------------- #
    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        provided = self.headers.get("X-SciLoop-Runner-Token") or ""
        return bool(provided) and secrets.compare_digest(provided.strip(), self.token)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- 路由 ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - http.server 的约定
        if self.path.rstrip("/") == "/health":
            # health 故意不需要密钥：后端用它判断"执行器在不在"，好给用户一句人话提示，
            # 顺便把**宿主视角的路径**告诉后端（它自己看不到宿主文件系统）。
            self._send(
                200,
                {
                    "ok": True,
                    "name": "SciLoop 宿主执行器",
                    "version": VERSION,
                    "platform": sys.platform,
                    "python": sys.version.split()[0],
                    "uptime_s": int(time.time() - self.started_at),
                    "home": str(Path.home()),
                    "host_root": str(REPO_ROOT),
                    "project_roots": [str(PROJECT_ROOT)],
                    "default_cwd": str(PROJECT_ROOT),
                },
            )
            return
        self._send(404, {"ok": False, "error": "没有这个接口"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path not in ("/exec", "/fs", "/pick-folder"):
            self._send(404, {"ok": False, "error": "没有这个接口"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "密钥不对：请在 SciLoop 设置页填宿主执行器的密钥"})
            return

        body = self._body()
        if path == "/pick-folder":
            result = pick_folder(
                title=str(body.get("title") or "选择文件夹"),
                initial=str(body.get("initial") or ""),
                timeout_s=int(body.get("timeout_s") or 600),
            )
        elif path == "/exec":
            argv = body.get("argv")
            result = run_command(
                argv=[str(x) for x in argv] if isinstance(argv, list) else None,
                command=body.get("command") if isinstance(body.get("command"), str) else None,
                cwd=body.get("cwd") if isinstance(body.get("cwd"), str) else None,
                timeout_s=int(body.get("timeout_s") or DEFAULT_TIMEOUT_S),
                env_extra=body.get("env") if isinstance(body.get("env"), dict) else None,
            )
        else:
            result = fs_action(
                action=str(body.get("action") or ""),
                path=str(body.get("path") or ""),
                to=body.get("to") if isinstance(body.get("to"), str) else None,
                content=body.get("content") if isinstance(body.get("content"), str) else None,
                encoding=body.get("encoding") if isinstance(body.get("encoding"), str) else None,
                recursive=bool(body.get("recursive")),
            )
        self._send(200 if result.get("ok") else 400, result)


class Server(ThreadingHTTPServer):
    """不许两个实例抢同一个端口。

    ⚠️ 实测坑：`HTTPServer` 默认 `allow_reuse_address = True`，在 Windows 上于是
    **第二个实例也能绑定成功**（不报错），请求随机落到其中一个上 ——
    用户会看到"我刚改的配置没生效"这种极难查的现象（我自己就踩了：4 个旧实例同时占着端口，
    新实例的 /health 永远被旧实例抢答）。这里显式关掉，撞端口就明确失败。
    """

    allow_reuse_address = False
    daemon_threads = True


def _selftest() -> int:
    """自检：本机能不能正常跑命令 / 读写文件。用户排障与部署验收都用它。"""

    print("SciLoop 宿主执行器 · 自检")
    print(f"  平台：{sys.platform}  Python：{sys.version.split()[0]}")
    print(f"  家目录：{Path.home()}")
    print()

    checks = [
        ("跑一条最简单的命令", lambda: run_command(argv=[sys.executable, "-c", "print('ok')"], cwd=str(Path.home()), timeout_s=20)),
        ("看当前用户是谁", lambda: run_command(command="whoami", cwd=str(Path.home()), timeout_s=20)),
        ("看 git 能不能用", lambda: run_command(argv=["git", "--version"], cwd=str(Path.home()), timeout_s=20)),
        ("列一下家目录", lambda: fs_action(action="list", path=str(Path.home()), to=None, content=None, encoding=None, recursive=False)),
    ]
    failures = 0
    for label, call in checks:
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001 - 自检要把任何异常都摊开给人看
            print(f"  ✗ {label}：抛异常 {type(exc).__name__}: {exc}")
            failures += 1
            continue
        if result.get("ok"):
            extra = (result.get("stdout") or "").strip().splitlines()
            first = extra[0] if extra else ("目录项 %d 个" % len(result.get("children") or []))
            print(f"  ✓ {label}：{first[:80]}")
        else:
            code = result.get("exit_code")
            print(f"  ✗ {label}：{result.get('error') or ('退出码 ' + str(code))}")
            if code == 3221225794:
                print("      （退出码 0xC0000142 = 子进程拿不到控制台。"
                      "把执行器从**普通终端**里启动一次通常就好了；"
                      "被计划任务/服务拉起时请确保允许交互式进程。）")
            failures += 1

    print()
    print("结论：" + ("全部正常，可以把它当执行器用" if failures == 0 else f"有 {failures} 项失败，见上面的原因"))
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SciLoop 宿主执行器：让 SciLoop 能在你这台电脑上跑命令、读写文件")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"监听端口（不传就按 .env 里 SCILOOP_HOST_RUNNER_URL 的端口，都没有才用 {DEFAULT_PORT}）",
    )
    parser.add_argument("--selftest", action="store_true", help="自检本机能不能跑命令、读文件后退出")
    parser.add_argument(
        "--token",
        default="",
        help="Owner 密钥（默认从环境变量或项目 .env 读，一般不用手动给）",
    )
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()

    # 认证就用 Owner 那一枚密钥 —— 不再有"执行器自己的密钥"
    # 端口与令牌**同源**：都从 `.env` 读。否则会出现"配置写 8766、脚本默认 8765"，
    # 研究者怎么照文档做都连不上（见 read_configured_port 的说明）。
    port = args.port or read_configured_port() or DEFAULT_PORT

    token = (args.token or "").strip() or read_owner_token()
    if not token:
        print("")
        print("启动失败：没有找到 Owner 密钥。")
        print(f"请确认项目里的 {ENV_FILE} 有一行 OWNER_TOKEN=...（或用环境变量 {OWNER_TOKEN_ENV} 给）。")
        print("密钥就这一枚，SciLoop 的后端和执行器共用它。")
        print("")
        return 2

    # 研究工作项目的根目录先建好：模型"没有特别指定目录"时就在这儿干活
    try:
        PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 权限异常时只说一声，不让启动失败
        print(f"（提示：没能创建 {PROJECT_ROOT}：{exc}）")

    load_or_create_state(port)

    Handler.token = token
    Handler.started_at = time.time()

    # 启动时先探一次"能不能起子进程"：不行就当场说清，别等用户点了半天才发现。
    # 超时给 5 秒就够（正常是毫秒级）；探测卡住不该拖着用户看不到启动横幅。
    probe = run_command(argv=[sys.executable, "-c", "print('ok')"], cwd=str(Path.home()), timeout_s=5)

    # 绑定 127.0.0.1：局域网/外网都连不上，只有本机能调
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        print("")
        print(f"启动失败：端口 {port} 已经被占用了（{exc}）。")
        print("通常说明已经有一个执行器在跑 —— 先关掉它，或者换一个端口：")
        print(f"    python tools/host-runner/host_runner.py --port {port + 1}")
        print("")
        return 2

    _say("")
    _say("SciLoop 宿主执行器已启动（保持这个窗口开着）")
    _say(f"  监听地址：http://127.0.0.1:{port}（只有本机能访问）")
    _say("  认证：用项目 .env 里的 Owner 密钥（不显示明文）")
    _say("  停止：按 Ctrl+C")
    if not probe.get("ok"):
        _say("")
        _say("⚠️ 注意：现在这台机器上起不了子进程，SciLoop 暂时没法帮你跑命令。")
        _say(f"   原因：{probe.get('error') or ('退出码 ' + str(probe.get('exit_code')))}")
        if probe.get("exit_code") == 3221225794:
            _say("   （退出码 0xC0000142 = 子进程拿不到控制台。请把本程序改从"
                 "普通终端窗口启动；被服务/计划任务拉起时需允许交互式进程。）")
        _say("   输入 `python tools/host-runner/host_runner.py --selftest` 可随时自查。")
    _say("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
