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
import base64
import functools
import importlib.util
import json
import locale
import os
import secrets
import subprocess
import sys
import threading
import tempfile
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


def _load_sandbox_module() -> Any:
    """加载仓库内沙箱模块；执行器作为独立脚本启动时不依赖包路径。"""
    path = REPO_ROOT / "server" / "services" / "agent" / "sandbox.py"
    spec = importlib.util.spec_from_file_location("sciloop_host_sandbox", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"找不到宿主沙箱模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

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


def _python_selection() -> tuple[str, str]:
    raw = (os.environ.get(HOST_PYTHON_ENV) or "").strip() or read_env_value(HOST_PYTHON_ENV)
    configured = raw.strip().strip('"').strip("'")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve()), "configured"
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python", "server/.venv/Scripts/python.exe", "server/.venv/bin/python"):
        candidate = REPO_ROOT / relative
        if candidate.is_file():
            return str(candidate.resolve()), "project"
    return sys.executable, "runtime"


@functools.lru_cache(maxsize=8)
def _python_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "-c", "import platform; print(platform.python_version())"],
            capture_output=True, timeout=3, check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
        return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def python_environment() -> dict[str, str]:
    executable, source = _python_selection()
    return {"executable": executable, "source": source, "version": _python_version(executable)}


def execution_capabilities() -> dict[str, Any]:
    try:
        capability = _load_sandbox_module().available()
    except (ImportError, OSError) as exc:
        capability = {"available": False, "reason": str(exc)}
    return {"full_access": True, "approved_execution": True,
            "workspace_execution": bool(capability.get("available")),
            "sandbox": capability}


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

    return _python_selection()[0]


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

    # Environment overrides must match the backend's endpoint resolution.
    configured = (os.environ.get("SCILOOP_HOST_RUNNER_URL") or "").strip()
    if configured:
        from urllib.parse import urlsplit

        try:
            return urlsplit(configured).port
        except ValueError:
            return None
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
def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_write_allowed(path: Path, *, workspace_root: str | None, access_mode: str) -> bool:
    if access_mode in {"full", "approved"}:
        return True
    if not workspace_root:
        return False
    return _inside(path, Path(workspace_root).expanduser())


def run_command(
    *,
    argv: list[str] | None = None,
    command: str | None = None,
    cwd: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    env_extra: dict[str, str] | None = None,
    workspace_root: str | None = None,
    access_mode: str = "workspace",
) -> dict[str, Any]:
    """执行一条命令，返回真实退出码与输出（**不美化、不伪造**）。"""

    if access_mode not in {"workspace", "read-only", "full", "approved"}:
        return {"ok": False, "error": "执行权限无效"}

    use_shell = False
    if argv:
        args = list(argv)
        # Bare Python means the selected scientific environment. Preserve an
        # explicit executable path or version requested by the user/model.
        if args[0].lower() in {"python", "python.exe", "python3", "python3.exe"}:
            args[0] = host_python()
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

    workdir = cwd or workspace_root or str(Path.home())
    if not Path(workdir).is_dir():
        return {"ok": False, "error": f"工作目录不存在：{workdir}"}

    env = dict(os.environ)
    env["SCILOOP_WORKSPACE_ROOT"] = str(workspace_root or workdir)
    env["SCILOOP_ACCESS_MODE"] = access_mode
    # 让子进程的 `python` 指向研究者的科研环境（等价于"先激活环境"）：
    # 不这么做，模型探到的是"恰好拉起执行器的那个解释器"，结论会离谱（见 host_python 注释）。
    prefix = python_path_prefix()
    if prefix:
        env["PATH"] = os.pathsep.join([*prefix, env.get("PATH", "")])
        env[HOST_PYTHON_ENV] = host_python()
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    # 工作区模式必须经过操作系统沙箱；沙箱不可用时返回明确错误，
    # 不能为了“还能跑”而退回到无约束的宿主进程。
    if access_mode not in {"full", "approved"}:
        try:
            sandbox = _load_sandbox_module()
        except (ImportError, OSError) as exc:
            return {"ok": False, "code": "sandbox_unavailable", "error": f"受限执行器不可用：{exc}", "workdir": workdir}
        result = sandbox.run(
            argv=args if argv else None,
            command=command,
            cwd=workdir,
            workspace_root=str(workspace_root or workdir),
            timeout_s=timeout_s,
            env=env,
            access_mode=access_mode,
        )
        if isinstance(result, dict):
            result.setdefault("workdir", workdir)
        return result

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
# 目录选择任务（2026-10-11 重构）
#
# 为什么改成"任务"：研究者可能盯着系统窗口几十秒到几分钟才点 ✓，
# 而普通 HTTP 请求有 30 秒默认超时 → 页面先报"请求超时（30000ms）"，选择白做 ✗。
# 于是拆成三段：
#   POST /pick-folder                  建任务，**立刻**回 {"request_id","status":"pending"}
#   GET  /pick-folder/<request_id>     读状态（pending/selected/canceled/timed_out/unavailable/failed）
#   POST /pick-folder/<id>/cancel      取消（关掉同一个系统窗口）
#
# 弹框跑在**专用线程**里：Windows 的 COM 模态对话框必须归它自己那个 STA 线程所有。
# 取消 / 超时都走 `PostMessage(hwnd, WM_CLOSE)`，由对话框自己的消息循环消费
# —— 跨线程直接调 `IFileDialog::Close` 不安全，老版本就是踩了这个坑 ✗。
# ---------------------------------------------------------------------------------------
_PICK_RETAIN_S = 600.0        # 终态结果保留 10 分钟（允许前端重复读同一结果）
_PICK_MAX_LIFE_S = 900.0      # 单条任务最长存活 15 分钟（系统保护期限）
_PICK_MAX_TASKS = 64
_PICK_NATIVE: dict[str, Any] = {"loaded": False, "module": None}


def _load_native_picker() -> Any:
    """加载同目录的 `pick_native.py`（执行器是独立脚本，不依赖包路径）。"""

    if not _PICK_NATIVE["loaded"]:
        _PICK_NATIVE["loaded"] = True
        script = Path(__file__).with_name("pick_native.py")
        try:
            spec = importlib.util.spec_from_file_location("sciloop_pick_native", script)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                _PICK_NATIVE["module"] = module
        except Exception:  # noqa: BLE001 - 原生模块不可用就如实回落别的实现 ✓
            _PICK_NATIVE["module"] = None
    return _PICK_NATIVE["module"]


class _PickTask:
    """一次目录选择请求的全部状态（**只在 `_PickManager` 的锁内改**）。"""

    __slots__ = (
        "request_id", "title", "initial", "timeout_s", "impl", "status", "path",
        "error", "error_class", "created_at", "finished_at", "cancel_event", "thread", "proc",
    )

    def __init__(self, *, request_id: str, title: str, initial: str, timeout_s: int, impl: str) -> None:
        self.request_id = request_id
        self.title = title
        self.initial = initial
        self.timeout_s = timeout_s
        self.impl = impl
        self.status = "pending"
        self.path = ""
        self.error = ""
        self.error_class = ""
        self.created_at = time.time()
        self.finished_at = 0.0
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen[bytes] | None = None


def _picker_impl() -> str:
    """这台机器**默认**用哪种实现（例外情况在 `_pick_windows_native` 里再回落）。"""

    if sys.platform.startswith("win"):
        return "native" if _load_native_picker() is not None else "fallback"
    if sys.platform == "darwin":
        return "osascript"
    return "zenity"


class _PickManager:
    """同一时刻只允许一个系统窗口；结果保留 10 分钟供重复读取。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, _PickTask] = {}
        self._active_id = ""

    # -- 建任务 ---------------------------------------------------------- #
    def create(self, *, request_id: str, title: str, initial: str, timeout_s: int) -> dict[str, Any]:
        impl = _picker_impl()
        with self._lock:
            self._sweep_locked()
            active = self._tasks.get(self._active_id)
            if active is not None and active.status == "pending":
                # 已经有一个窗口挂着 → 复用，**绝不弹第二个** ✓
                return {
                    "ok": True,
                    "request_id": active.request_id,
                    "status": "pending",
                    "accepted": False,
                    "busy": True,
                    "impl": active.impl,
                    "fallback": active.impl == "fallback",
                }
            task = _PickTask(
                request_id=request_id or f"pick_{secrets.token_hex(8)}",
                title=title,
                initial=initial,
                timeout_s=timeout_s,
                impl=impl,
            )
            self._tasks[task.request_id] = task
            self._active_id = task.request_id
            self._trim_locked()
            task.thread = threading.Thread(
                target=self._run, args=(task,), name=f"pick-{task.request_id}", daemon=True
            )
            task.thread.start()
            return {
                "ok": True,
                "request_id": task.request_id,
                "status": "pending",
                "accepted": True,
                "impl": impl,
                "fallback": impl == "fallback",
            }

    # -- 读状态 ---------------------------------------------------------- #
    def status(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return {"ok": True, "request_id": request_id, "status": "expired", "done": True, "path": ""}
            self._expire_if_overdue_locked(task)
            return {
                "ok": True,
                "request_id": task.request_id,
                "status": task.status,
                "done": task.status != "pending",
                "path": task.path,
                "error": task.error,
                "error_class": task.error_class,
                "impl": task.impl,
                "fallback": task.impl == "fallback",
                "elapsed_ms": int(((task.finished_at or time.time()) - task.created_at) * 1000),
            }

    # -- 取消 ------------------------------------------------------------ #
    def cancel(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(request_id)
            if task is None:
                return {"ok": True, "request_id": request_id, "status": "expired", "done": True}
            if task.status != "pending":
                return {"ok": True, "request_id": request_id, "status": task.status, "done": True, "path": task.path}
            task.cancel_event.set()
        # PowerShell/命令行回落路径没法被消息打断 → 直接结束它（窗口随进程消失，不留孤儿 ✓）
        proc = task.proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except (OSError, ValueError):
                pass
        if task.thread is not None:
            task.thread.join(timeout=10)
        with self._lock:
            if task.status == "pending":
                task.status = "canceled"
                task.finished_at = time.time()
            if self._active_id == request_id:
                self._active_id = ""
            return {"ok": True, "request_id": request_id, "status": task.status, "done": True, "path": task.path}

    # -- 内部 ------------------------------------------------------------ #
    def _run(self, task: _PickTask) -> None:
        try:
            result = _execute_pick(task)
        except Exception as exc:  # noqa: BLE001 - 弹框崩了也只影响这一条任务
            result = {
                "ok": False,
                "status": "failed",
                "error_class": "picker_crashed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        with self._lock:
            task.status = _settle_status(task, result)
            task.path = str(result.get("path") or "")
            task.error = str(result.get("error") or "")
            task.error_class = str(result.get("error_class") or "")
            if task.path and not os.path.isdir(task.path):
                # 选完就被删了 / 不是目录 → 不能返回伪成功 ✗
                task.status = "failed"
                task.error = "选中的目录在系统上已经不存在了。"
                task.error_class = "path_missing"
            task.finished_at = time.time()
            if self._active_id == task.request_id:
                self._active_id = ""

    def _sweep_locked(self) -> None:
        now = time.time()
        for key, task in list(self._tasks.items()):
            if task.status != "pending" and now - task.finished_at > _PICK_RETAIN_S:
                self._tasks.pop(key, None)
            elif task.status == "pending" and now - task.created_at > _PICK_MAX_LIFE_S + 60:
                task.cancel_event.set()
                task.status = "timed_out"
                task.finished_at = now

    def _expire_if_overdue_locked(self, task: _PickTask) -> None:
        """到系统保护期限就收束（任务不能永远 pending，否则前端一直等）✓。"""

        if task.status != "pending":
            return
        if time.time() - task.created_at <= _PICK_MAX_LIFE_S:
            return
        task.cancel_event.set()
        task.status = "timed_out"
        task.finished_at = time.time()
        if self._active_id == task.request_id:
            self._active_id = ""

    def _trim_locked(self) -> None:
        while len(self._tasks) > _PICK_MAX_TASKS:
            key, task = next(iter(self._tasks.items()))
            if task.status == "pending":
                break
            self._tasks.pop(key, None)


_PICK = _PickManager()


def pick_create(
    *, request_id: str = "", title: str = "选择文件夹", initial: str = "", timeout_s: int = 600
) -> dict[str, Any]:
    """建一条选择任务（**立刻返回**，不阻塞在用户选择上）。"""

    clamped = max(5, min(int(timeout_s or 600), int(_PICK_MAX_LIFE_S)))
    return _PICK.create(request_id=request_id, title=title, initial=initial, timeout_s=clamped)


def pick_status(*, request_id: str) -> dict[str, Any]:
    """读任务状态（终态可重复读同一结果 ✓）。"""

    return _PICK.status(request_id)


def pick_cancel(*, request_id: str) -> dict[str, Any]:
    """取消任务（关掉同一个系统窗口；幂等 ✓）。"""

    return _PICK.cancel(request_id)


def _settle_status(task: _PickTask, result: dict[str, Any]) -> str:
    """把各实现的返回值收敛成任务终态（**只能从 pending 落到终态**）。"""

    if task.cancel_event.is_set() and not result.get("path"):
        return "canceled"
    status = str(result.get("status") or "")
    if status in ("selected", "canceled", "timed_out", "unavailable", "failed"):
        if status == "selected" and not str(result.get("path") or ""):
            return "canceled"
        return status
    if result.get("timed_out"):
        return "timed_out"
    if not result.get("ok"):
        return "unavailable" if result.get("unsupported") else "failed"
    return "selected" if result.get("path") else "canceled"


def _execute_pick(task: _PickTask) -> dict[str, Any]:
    if task.impl == "native":
        return _pick_windows_native(task)
    if task.impl == "fallback":
        return _pick_windows_fallback(task)
    if task.impl == "osascript":
        return _pick_macos(task)
    return _pick_zenity(task)


def _pick_windows_native(task: _PickTask) -> dict[str, Any]:
    module = _load_native_picker()
    if module is None:
        return _pick_windows_fallback(task, reason="原生选择器模块不可用")
    outcome = module.pick_folder_native(
        title=task.title, timeout_s=task.timeout_s, cancel_event=task.cancel_event
    )
    if not isinstance(outcome, dict):
        return {
            "ok": False,
            "status": "failed",
            "error_class": "bad_result",
            "error": "选择器返回了看不懂的内容",
        }
    # **只有明确用不了原生**（没有桌面 / 建不出对话框）才回落 —— 正常机器绝不回落 ✓
    if outcome.get("unsupported") and str(outcome.get("status")) == "unavailable":
        return _pick_windows_fallback(task, reason=str(outcome.get("error") or "原生选择器不可用"))
    return outcome


def _powershell_quote(value: str) -> str:
    """单引号字符串 + 去掉控制字符（标题不该带换行/引号/控制字符）✓。"""

    cleaned = "".join(ch for ch in str(value) if ch >= " " or ch == "\t")
    return "'" + cleaned.replace("'", "''") + "'"


def _powershell_script(title: str, initial: str, timeout_s: int) -> str:
    steps = [
        "$OutputEncoding = [System.Text.Encoding]::UTF8",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "Add-Type -AssemblyName System.Windows.Forms",
        "Add-Type -AssemblyName System.Drawing",
        "$owner = New-Object System.Windows.Forms.Form",
        "$owner.ShowInTaskbar = $false; $owner.FormBorderStyle = 'None'",
        "$owner.Opacity = 0; $owner.TopMost = $true",
        "$owner.Size = New-Object System.Drawing.Size(1,1)",
        "$owner.StartPosition = 'CenterScreen'; $owner.Show(); $owner.Activate()",
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
        f"$d.Description = {_powershell_quote(title)}",
        "$d.ShowNewFolderButton = $true",
    ]
    if initial:
        steps.append(f"$d.SelectedPath = {_powershell_quote(initial)}")
    steps += [
        "$hits = 0",
        "$timer = New-Object System.Windows.Forms.Timer",
        f"$timer.Interval = {timeout_s * 1000}",
        "$timer.Add_Tick({ $script:hits = 1; $d.Dispose(); $owner.Close() })",
        "$timer.Start()",
        "$sw = [System.Diagnostics.Stopwatch]::StartNew()",
        "$r = $d.ShowDialog($owner)",
        "$sw.Stop(); $timer.Stop(); $owner.Close()",
        "Write-Output ('elapsed_ms=' + $sw.ElapsedMilliseconds)",
        "Write-Output ('picked=' + $d.SelectedPath)",
        "if ($hits -eq 1) { Write-Output 'timed_out=1' } else { Write-Output 'timed_out=0' }",
    ]
    return "; ".join(steps)


def _pick_windows_fallback(task: _PickTask, *, reason: str = "") -> dict[str, Any]:
    """PowerShell 回落（**只在原生确实不可用时**；结果里标记 `fallback` 供日志与验收）✓。

    标题/路径一律走 `-EncodedCommand`（UTF-16LE base64）：
    **不把中文拼进命令行源码** —— 那正是标题乱码与多层引号踩雷的地方 ✗。
    """

    script = _powershell_script(task.title, task.initial, task.timeout_s)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["powershell", "-NoProfile", "-STA", "-EncodedCommand", encoded]
    try:
        proc = subprocess.Popen(  # noqa: S603 - 弹系统对话框是本模块的本职
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )
    except OSError as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "error_class": "no_powershell",
            "fallback": True,
            "error": f"这台机器上没有可用的系统选择框（{reason or exc}）。",
        }
    task.proc = proc
    try:
        raw, _ = proc.communicate(timeout=task.timeout_s + 20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {
            "ok": False,
            "status": "timed_out",
            "shown": True,
            "fallback": True,
            "error": f"等了 {task.timeout_s} 秒还没选，已关闭选择框。",
        }
    finally:
        task.proc = None

    fields: dict[str, str] = {}
    for line in _decode(raw or b"").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    if fields.get("timed_out") == "1":
        return {
            "ok": False,
            "status": "timed_out",
            "shown": True,
            "fallback": True,
            "error": f"等了 {task.timeout_s} 秒还没选，已关闭选择框。",
        }
    picked = (fields.get("picked") or "").strip()
    if not picked:
        return {
            "ok": True,
            "status": "canceled",
            "shown": True,
            "path": "",
            "canceled": True,
            "timed_out": False,
            "fallback": True,
        }
    return {
        "ok": True,
        "status": "selected",
        "shown": True,
        "path": picked,
        "canceled": False,
        "timed_out": False,
        "fallback": True,
    }


def _osa_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pick_macos(task: _PickTask) -> dict[str, Any]:
    script = f"POSIX path of (choose folder with prompt {_osa_quote(task.title)})"
    return _run_cli_picker(task, ["osascript", "-e", script])


def _pick_zenity(task: _PickTask) -> dict[str, Any]:
    return _run_cli_picker(task, ["zenity", "--file-selection", "--directory", "--title", task.title])


def _run_cli_picker(task: _PickTask, argv: list[str]) -> dict[str, Any]:
    """macOS / Linux：命令行选择器（标题走**参数数组**，绝不拼 shell 命令）✓。"""

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603
    except FileNotFoundError:
        return {
            "ok": False,
            "status": "unavailable",
            "error_class": "no_picker",
            "error": "这台机器上没有可用的系统选择框。",
        }
    except OSError as exc:
        return {
            "ok": False,
            "status": "unavailable",
            "error_class": "no_picker",
            "error": f"弹不出来：{exc}",
        }
    task.proc = proc
    try:
        raw, _ = proc.communicate(timeout=task.timeout_s + 20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {
            "ok": False,
            "status": "timed_out",
            "shown": True,
            "error": f"等了 {task.timeout_s} 秒还没选，已关闭选择框。",
        }
    finally:
        task.proc = None
    if proc.returncode != 0:
        return {"ok": True, "status": "canceled", "shown": True, "path": "", "canceled": True, "timed_out": False}
    path = (raw or b"").decode("utf-8", "replace").strip()
    if not path:
        return {"ok": True, "status": "canceled", "shown": True, "path": "", "canceled": True, "timed_out": False}
    return {"ok": True, "status": "selected", "shown": True, "path": path, "canceled": False, "timed_out": False}


def fs_action(*, action: str, path: str, to: str | None, content: str | None,
              encoding: str | None, recursive: bool, workspace_root: str | None = None,
              access_mode: str = "workspace") -> dict[str, Any]:
    """文件操作。删除**只在这里具备能力**，是否允许由后端按用户定的三层边界裁决。"""

    if access_mode not in {"workspace", "read-only", "full", "approved"}:
        return {"ok": False, "error": "无效的执行授权", "code": "invalid_access_mode"}
    target = Path(path).expanduser()
    write_actions = {"write", "mkdir", "move", "copy", "delete"}
    if action in write_actions and not _path_write_allowed(target, workspace_root=workspace_root, access_mode=access_mode):
        return {"ok": False, "error": "受限模式只允许在选定工作目录内写入、移动或删除；目录外仅允许读取", "code": "path_outside"}
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
            if not _path_write_allowed(dest, workspace_root=workspace_root, access_mode=access_mode):
                return {"ok": False, "error": "移动目标必须位于选定工作目录内", "code": "path_outside"}
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

    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        # Startup is commonly launched without a UTF-8 console on Windows. A
        # banner must never terminate the runner after it has already started.
        stream = getattr(sys, "stdout", None)
        encoding = getattr(stream, "encoding", None) or "utf-8"
        safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)


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
        path = self.path.rstrip("/")
        if path == "/health":
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
                    "python_env": python_environment(),
                    "execution_capabilities": execution_capabilities(),
                    "uptime_s": int(time.time() - self.started_at),
                    "home": str(Path.home()),
                    "host_root": str(REPO_ROOT),
                    "project_roots": [str(PROJECT_ROOT)],
                    "default_cwd": str(PROJECT_ROOT),
                },
            )
            return
        if path.startswith("/pick-folder/"):
            if not self._authorized():
                self._send(401, {"ok": False, "error": "密钥不对：请在 SciLoop 设置页填宿主执行器的密钥"})
                return
            self._send(200, pick_status(request_id=path[len("/pick-folder/") :]))
            return
        self._send(404, {"ok": False, "error": "没有这个接口"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/pick-folder":
            # 建任务：**立刻**回受理结果，绝不在这里等研究者选完 ✓
            if not self._authorized():
                self._send(401, {"ok": False, "error": "密钥不对：请在 SciLoop 设置页填宿主执行器的密钥"})
                return
            body = self._body()
            self._send(
                200,
                pick_create(
                    request_id=str(body.get("request_id") or ""),
                    title=str(body.get("title") or "选择文件夹"),
                    initial=str(body.get("initial") or ""),
                    timeout_s=int(body.get("timeout_s") or 600),
                ),
            )
            return
        if path.startswith("/pick-folder/") and path.endswith("/cancel"):
            if not self._authorized():
                self._send(401, {"ok": False, "error": "密钥不对：请在 SciLoop 设置页填宿主执行器的密钥"})
                return
            self._send(200, pick_cancel(request_id=path[len("/pick-folder/") : -len("/cancel")]))
            return
        if path not in ("/exec", "/fs"):
            self._send(404, {"ok": False, "error": "没有这个接口"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "密钥不对：请在 SciLoop 设置页填宿主执行器的密钥"})
            return

        body = self._body()
        if path == "/exec":
            argv = body.get("argv")
            result = run_command(
                argv=[str(x) for x in argv] if isinstance(argv, list) else None,
                command=body.get("command") if isinstance(body.get("command"), str) else None,
                cwd=body.get("cwd") if isinstance(body.get("cwd"), str) else None,
                timeout_s=int(body.get("timeout_s") or DEFAULT_TIMEOUT_S),
                env_extra=body.get("env") if isinstance(body.get("env"), dict) else None,
                workspace_root=body.get("workspace_root") if isinstance(body.get("workspace_root"), str) else body.get("cwd"),
                access_mode=str(body.get("access_mode") or "workspace"),
            )
        else:
            result = fs_action(
                action=str(body.get("action") or ""),
                path=str(body.get("path") or ""),
                to=body.get("to") if isinstance(body.get("to"), str) else None,
                content=body.get("content") if isinstance(body.get("content"), str) else None,
                encoding=body.get("encoding") if isinstance(body.get("encoding"), str) else None,
                recursive=bool(body.get("recursive")),
                workspace_root=body.get("workspace_root") if isinstance(body.get("workspace_root"), str) else None,
                access_mode=str(body.get("access_mode") or "workspace"),
            )
        self._send(200 if result.get("ok") else 400, result)

    def do_DELETE(self) -> None:  # noqa: N802 - 取消选择任务（等价于 POST .../cancel）
        path = self.path.rstrip("/")
        if not path.startswith("/pick-folder/"):
            self._send(404, {"ok": False, "error": "没有这个接口"})
            return
        if not self._authorized():
            self._send(401, {"ok": False, "error": "密钥不对：请在 SciLoop 设置页填宿主执行器的密钥"})
            return
        self._send(200, pick_cancel(request_id=path[len("/pick-folder/") :]))


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

    _say("SciLoop 宿主执行器 · 自检")
    _say(f"  平台：{sys.platform}  Python：{sys.version.split()[0]}")
    _say(f"  家目录：{Path.home()}")
    _say()

    checks = [
        (
            "跑一条最简单的命令",
            lambda: run_command(
                argv=[sys.executable, "-c", "print('ok')"],
                cwd=str(REPO_ROOT),
                workspace_root=str(REPO_ROOT),
                access_mode="full",
                timeout_s=20,
            ),
        ),
        (
            "看当前用户是谁",
            lambda: run_command(
                command="whoami",
                cwd=str(REPO_ROOT),
                workspace_root=str(REPO_ROOT),
                access_mode="full",
                timeout_s=20,
            ),
        ),
        (
            "看 git 能不能用",
            lambda: run_command(
                argv=["git", "--version"],
                cwd=str(REPO_ROOT),
                workspace_root=str(REPO_ROOT),
                access_mode="full",
                timeout_s=20,
            ),
        ),
        ("列一下项目目录", lambda: fs_action(action="list", path=str(REPO_ROOT), to=None, content=None, encoding=None, recursive=False)),
    ]
    failures = 0
    for label, call in checks:
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001 - 自检要把任何异常都摊开给人看
            _say(f"  ✗ {label}：抛异常 {type(exc).__name__}: {exc}")
            failures += 1
            continue
        if result.get("ok"):
            extra = (result.get("stdout") or "").strip().splitlines()
            first = extra[0] if extra else ("目录项 %d 个" % len(result.get("children") or []))
            _say(f"  ✓ {label}：{first[:80]}")
        else:
            code = result.get("exit_code")
            _say(f"  ✗ {label}：{result.get('error') or ('退出码 ' + str(code))}")
            if code == 3221225794:
                _say("      （退出码 0xC0000142 = 子进程拿不到控制台。"
                     "把执行器从**普通终端**里启动一次通常就好了；"
                     "被计划任务/服务拉起时请确保允许交互式进程。）")
            failures += 1

    _say()
    _say("结论：" + ("全部正常，可以把它当执行器用" if failures == 0 else f"有 {failures} 项失败，见上面的原因"))
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
    # Probe the actual workspace mode on a tiny disposable directory, never
    # walk the user's home or modify permissions over the whole checkout.
    with tempfile.TemporaryDirectory(prefix="sciloop-startup-") as probe_root:
        probe = run_command(
            argv=[host_python(), "-c", "from pathlib import Path; Path('probe.txt').write_text('ok'); print('ok')"],
            cwd=probe_root,
            workspace_root=probe_root,
            access_mode="workspace",
            timeout_s=5,
        )

    # 绑定 127.0.0.1：局域网/外网都连不上，只有本机能调
    try:
        server = Server(("127.0.0.1", port), Handler)
    except OSError as exc:
        print("")
        print(f"启动失败：端口 {port} 已经被占用了（{exc}）。")
        print("请复用已经运行的执行器；若端口被其他程序占用，请先停止原服务。")
        print("不会自动换端口：执行器与后端必须使用同一份配置。")
        print("")
        return 2

    _say("")
    _say("SciLoop 宿主执行器已启动（保持这个窗口开着）")
    _say(f"  监听地址：http://127.0.0.1:{port}（只有本机能访问）")
    _say("  认证：用项目 .env 里的 Owner 密钥（不显示明文）")
    _say("  停止：按 Ctrl+C")
    if not probe.get("ok"):
        _say("")
        _say("受限执行检查未通过；已明确授权的完全访问模式不受此检查影响。")
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
