# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""容器内执行环境：`docker compose up` 之后就能干活，**不需要任何手工步骤**。

为什么需要它
------------
后端跑在容器里，容器看不见"宿主机"的进程 —— 所以"在研究者电脑上执行命令"原本需要
研究者**手动起一个本机小进程**（`tools/host-runner/`）。研究者 2026-09-24 明确要求：
**别人拿去部署时，`docker compose up` 就该配置好一切，不该再有额外动作。**

于是有了这个服务：它和宿主机那个执行器**说同一套协议**（`/health` `/info` `/exec` `/fs`），
所以后端不用改调用方式；区别只在"命令跑在哪" —— 跑在这个容器里，
能看到的目录 = 挂进来的那几个（整个项目根 + 产物目录），看不到这台机器的其它地方。

安全边界（想清楚才写的）
------------------------
- **不发布端口**：只在 compose 内网里可达（compose 服务之间才连得上）；
- 令牌就是项目里那枚 Owner 密钥（`X-SciLoop-Runner-Token`，与宿主机执行器一致）；
- **它自己不做业务判断**：哪些命令高危、哪些目录不准动，由后端那层按研究者定的规则判
  （与宿主机执行器同一套 `services/agent/policy.py`）—— 这里只保证"跑"和"读写"两件事，
  外加超时与输出上限，避免一条命令把容器拖死。

⚠️ 想用**真机执行**（跑在 Windows/macOS 宿主机、能碰到你机器上任何目录）的人：
装上 `tools/host-runner/` 并设为开机自启即可 —— 后端会**优先**用它（见 `host_runner.py` 的候选顺序）。
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

VERSION = "1.0.0"
TOKEN_HEADER = "X-SciLoop-Runner-Token"

#: 单条命令的最长超时（秒）与输出上限（字节）—— 防止一条命令把容器拖死
MAX_TIMEOUT_S = 900
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_S = 120

app = FastAPI(title="SciLoop 容器执行环境", version=VERSION, docs_url=None, redoc_url=None)


def _token() -> str:
    """认哪枚钥匙：优先专用变量，其次项目那枚 Owner 密钥（与宿主机执行器一致）。"""

    return (
        os.environ.get("SCILOOP_EXEC_TOKEN")
        or os.environ.get("OWNER_TOKEN")
        or ""
    ).strip()


def _workspace() -> Path:
    return Path(os.environ.get("EXEC_WORKSPACE") or "/workspace")


def _artifacts() -> Path:
    return Path(os.environ.get("EXEC_ARTIFACTS") or "/artifacts")


def _default_cwd() -> Path:
    raw = os.environ.get("EXEC_DEFAULT_CWD")
    return Path(raw) if raw else _workspace()


def _python() -> str:
    """跑脚本用的解释器：默认就是它自己的（镜像里预装好的那个）。"""

    return os.environ.get("EXEC_PYTHON") or sys.executable or "python"


def _authorised(token: str | None) -> bool:
    expected = _token()
    if not expected:
        # 没配钥匙就只允许只读端点（/health /info）—— 绝不让一个不设防的执行器裸奔
        return False
    return bool(token) and token == expected


def _roots() -> dict[str, Any]:
    """执行环境"自述它能看到什么"——后端据此做路径换算（别在后端写死路径）。"""

    workspace = _workspace()
    return {
        "mode": "container",
        "host_root": str(workspace),
        "project_roots": [str(workspace)],
        "default_cwd": str(_default_cwd()),
        "artifacts_root": str(_artifacts()),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """情况汇报（不鉴权）。

    ⚠️ **必须把 `host_root` / `default_cwd` / `artifacts_root` 一起报** ——
    后端只读这一份当"执行环境的自述"（`host_runner.ensure_host_roots()` 用的是 `/health`，
    不是 `/info`）。第一版只报了 `workspace`，于是后端拿到 `host_root=None`，
    技能脚本的路径换不出来 → 报"找不到这个技能在研究者电脑上的位置"（实测踩到）。
    """

    return {
        "ok": True,
        "name": "SciLoop 容器执行环境",
        "version": VERSION,
        "platform": f"{platform.system()} {platform.release()}",
        "python": _python(),
        "uptime_s": int(time.monotonic() - _STARTED),
        **_roots(),
    }


@app.get("/info")
async def info() -> dict[str, Any]:
    """执行环境能看到什么（后端据此做路径换算，别在后端写死宿主路径）。"""

    workspace = _workspace()
    return {
        "ok": True,
        **_roots(),
        "python": _python(),
        "workspace_exists": workspace.is_dir(),
        "artifacts_exists": _artifacts().is_dir(),
    }


@app.post("/exec")
async def exec_command(request: Request, token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> Any:
    """跑一条命令（argv 优先；command 走 `sh -c`）。**已获批之后才会被调到这里**。"""

    if not _authorised(token):
        raise HTTPException(status_code=401, detail={"error": "执行环境不认识这串密钥"})
    payload = await _json(request)
    argv = payload.get("argv") if isinstance(payload.get("argv"), list) else None
    command = payload.get("command") if isinstance(payload.get("command"), str) else None
    cwd = str(payload.get("cwd") or "") or None
    timeout_s = int(payload.get("timeout_s") or DEFAULT_TIMEOUT_S)
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))

    workdir = Path(cwd) if cwd else _default_cwd()
    if not workdir.is_dir():
        return {"ok": False, "error": f"工作目录不存在：{workdir}", "exit_code": None}

    if argv:
        program, args, shell = str(argv[0]), [str(item) for item in argv[1:]], False
    elif command:
        program, args, shell = command, [], True
    else:
        return {"ok": False, "error": "没给命令", "exit_code": None}

    started = time.monotonic()
    try:
        if shell:
            process = await asyncio.create_subprocess_shell(
                program,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
            )
        else:
            process = await asyncio.create_subprocess_exec(
                program,
                *args,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
        except TimeoutError:
            process.kill()
            return {
                "ok": False,
                "error": f"超过 {timeout_s} 秒还没跑完，已中止。",
                "exit_code": None,
                "duration_s": round(time.monotonic() - started, 2),
            }
    except FileNotFoundError:
        return {"ok": False, "error": f"找不到这个程序：{program}", "exit_code": 127}
    except OSError as exc:
        return {"ok": False, "error": f"起不来：{exc}", "exit_code": None}

    code = process.returncode
    return {
        "ok": code == 0,
        "exit_code": code,
        "stdout": _clip(stdout),
        "stderr": _clip(stderr),
        "cwd": str(workdir),
        "duration_s": round(time.monotonic() - started, 2),
    }


@app.post("/pick-folder")
async def pick_folder(token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> Any:
    """容器执行环境**弹不出系统选择框**（没有屏幕）—— 如实说清，别装能弹。

    前端据此把"选择文件夹"按钮置灰并说明原因（装了本机执行环境才可用）。
    """

    if not _authorised(token):
        raise HTTPException(status_code=401, detail={"error": "执行环境不认识这串密钥"})
    return {
        "ok": False,
        "supported": False,
        "error": "容器执行环境没有屏幕，弹不出系统文件夹选择框；装了本机执行环境（开机自启）才可用。",
    }


@app.post("/fs")
async def fs_action(request: Request, token: str | None = Header(default=None, alias=TOKEN_HEADER)) -> Any:
    """读写文件（read / write / list / mkdir / move / delete / exists）。

    只做动作，不做判断：目录准不准动由后端那层按研究者定的规则判。
    """

    if not _authorised(token):
        raise HTTPException(status_code=401, detail={"error": "执行环境不认识这串密钥"})
    payload = await _json(request)
    action = str(payload.get("action") or "")
    raw_path = payload.get("path")
    if not raw_path:
        return {"ok": False, "error": "没给路径"}
    path = Path(str(raw_path))

    try:
        if action == "read":
            if not path.is_file():
                return {"ok": False, "error": f"不是一个文件：{path}"}
            return {"ok": True, "path": str(path), "content": path.read_text(encoding="utf-8", errors="replace")}
        if action == "write":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(payload.get("content") or ""), encoding="utf-8", newline="\n")
            return {"ok": True, "path": str(path), "bytes": path.stat().st_size}
        if action == "list":
            target = path if path.is_dir() else path.parent
            if not target.is_dir():
                return {"ok": False, "error": f"目录不存在：{target}"}
            entries = []
            for child in sorted(target.iterdir()):
                entries.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_dir": child.is_dir(),
                        "bytes": child.stat().st_size if child.is_file() else None,
                    }
                )
            return {"ok": True, "path": str(target), "entries": entries[:2000]}
        if action == "mkdir":
            path.mkdir(parents=True, exist_ok=True)
            return {"ok": True, "path": str(path)}
        if action == "move":
            to = payload.get("to")
            if not to:
                return {"ok": False, "error": "移动要给 to"}
            target = Path(str(to))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            return {"ok": True, "path": str(target)}
        if action == "delete":
            if not path.exists():
                return {"ok": True, "path": str(path), "note": "本来就不存在"}
            if path.is_dir() and payload.get("recursive"):
                shutil.rmtree(path)
            elif path.is_dir():
                path.rmdir()
            else:
                path.unlink()
            return {"ok": True, "path": str(path)}
        if action == "exists":
            return {"ok": True, "path": str(path), "exists": path.exists(), "is_dir": path.is_dir()}
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": False, "error": f"不认这个动作：{action}"}


@app.exception_handler(HTTPException)
async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"ok": False, **detail})


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _clip(payload: bytes | None) -> str:
    text = (payload or b"").decode("utf-8", "replace")
    if len(text.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
        text = text[:MAX_OUTPUT_BYTES] + "\n…（输出太长，已截断）"
    return text


def _child_env() -> dict[str, str]:
    """给子进程的环境：带上 PATH 与常用变量；**不要把 Owner 密钥递给它**。"""

    env = dict(os.environ)
    env.pop("OWNER_TOKEN", None)
    env.pop("SCILOOP_EXEC_TOKEN", None)
    return env


_STARTED = time.monotonic()


def main() -> None:
    import uvicorn

    port = int(os.environ.get("EXEC_PORT") or 8765)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
