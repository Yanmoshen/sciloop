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
import os
import secrets
import shlex
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
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

    if argv:
        args = list(argv)
    elif command:
        # Windows 上 posix=False 才不会被反斜杠吃掉；两侧都试一次更稳
        args = shlex.split(command, posix=(os.name != "nt"))
    else:
        return {"ok": False, "error": "既没有 argv 也没有 command"}

    workdir = cwd or str(Path.home())
    if not Path(workdir).is_dir():
        return {"ok": False, "error": f"工作目录不存在：{workdir}"}

    env = dict(os.environ)
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
            "stdout": _clip((exc.stdout or b"").decode("utf-8", "replace")),
            "stderr": _clip((exc.stderr or b"").decode("utf-8", "replace")),
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
        "stdout": _clip(proc.stdout.decode("utf-8", "replace")),
        "stderr": _clip(proc.stderr.decode("utf-8", "replace")),
        "workdir": workdir,
    }


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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
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

    load_or_create_state(args.port)

    Handler.token = token
    Handler.started_at = time.time()

    # 启动时先探一次"能不能起子进程"：不行就当场说清，别等用户点了半天才发现。
    # 超时给 5 秒就够（正常是毫秒级）；探测卡住不该拖着用户看不到启动横幅。
    probe = run_command(argv=[sys.executable, "-c", "print('ok')"], cwd=str(Path.home()), timeout_s=5)

    # 绑定 127.0.0.1：局域网/外网都连不上，只有本机能调
    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print("")
        print(f"启动失败：端口 {args.port} 已经被占用了（{exc}）。")
        print("通常说明已经有一个执行器在跑 —— 先关掉它，或者换一个端口：")
        print(f"    python tools/host-runner/host_runner.py --port {args.port + 1}")
        print("")
        return 2

    _say("")
    _say("SciLoop 宿主执行器已启动（保持这个窗口开着）")
    _say(f"  监听地址：http://127.0.0.1:{args.port}（只有本机能访问）")
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
