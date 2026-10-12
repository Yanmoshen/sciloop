# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""后端 → 宿主执行器的客户端（让 SciLoop 能在研究者自己的电脑上干活）。

拓扑
----
后端与执行器都跑在同一台机器上（执行器是 `tools/host-runner/host_runner.py`，
后端通过 `127.0.0.1:8765` 找它）。2026-10-11 起本项目不再使用 Docker，
原先的 `host.docker.internal` 与 compose `executor` 服务两个候选已移除。

两条纪律
--------
1. **执行器连不上先自动恢复**：连接尚未建立时可恢复后继续同一请求；请求已发送而
   结果未知时不能重放有副作用操作，也不把异常堆栈交给研究者。
2. **这里不做业务判断**：什么算高危、哪些目录不能删，由 `services/agent/policy.py` 决定；
   本模块只负责"把已经批准的事送过去、把结果拿回来"。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from services.agent import policy

__all__ = [
    "LOCAL_URL",
    "START_HINT",
    "call_exec",
    "call_fs",
    "default_cwd",
    "ensure_host_roots",
    "ensure_available",
    "health",
    "is_connected",
    "pick_cancel",
    "pick_create",
    "pick_status",
    "runner_token",
    "runner_url",
]

LOCAL_URL = "http://127.0.0.1:8765"
URL_ENV = "SCILOOP_HOST_RUNNER_URL"
DEFAULT_TIMEOUT_S = 30.0

#: 目录选择：建任务的**受理预算**（只等"任务已受理"，不等研究员选完；§5.2）
PICK_ACCEPT_TIMEOUT_S = 3.0
#: 目录选择：执行器未起时的启动等待（只用于健康检查，不叠加到选择期限上）
PICK_ENSURE_WAIT_S = 1.5

#: 宿主路径的学习缓存（`/health` 里带回；60 秒内不重复问）
ROOT_TTL_S = 60.0
_ROOTS: dict[str, Any] = {"at": 0.0, "info": None}

#: 面向研究者的启动指引（**人话，不许出现内部术语**）
START_HINT = (
    "本地执行环境正在自动启动，准备好后会自动继续。"
    "若仍未恢复，维护人员可检查 tools/host-runner/host_runner.py 的安装状态。"
)


#: 挑选结果缓存（避免每条命令都试一遍）
_PICKED: dict[str, Any] = {"url": "", "at": 0.0}
PICK_TTL_S = 20.0

# 后端在宿主机直接运行；安装脚本可能尚未拉起宿主执行器，后端可以在同一份 checkout
# 中自动启动它。状态只用于进程生命周期管理，不把内部路径或异常送到对话正文。
AUTO_START_ENV = "SCILOOP_AUTO_START_HOST_RUNNER"
AUTO_START_RETRY_S = 15.0
AUTO_START_WAIT_S = 8.0
_AUTO_START_STATE: dict[str, Any] = {"at": 0.0, "process": None, "root": ""}
_AUTO_START_LOCK = threading.Lock()


def _auto_start_enabled() -> bool:
    raw = (os.environ.get(AUTO_START_ENV) or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _runner_script() -> tuple[Path, Path] | None:
    """Return ``(repository_root, host_runner.py)`` when running from a checkout."""

    configured = (os.environ.get("SCILOOP_HOST_RUNNER_SCRIPT") or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    # host_runner.py lives at <repo>/server/services/agent/host_runner.py.
    candidates.append(Path(__file__).resolve().parents[3] / "tools" / "host-runner" / "host_runner.py")
    for script in candidates:
        try:
            if script.is_file():
                return script.parent.parent.parent, script.resolve()
        except OSError:
            continue
    return None


def _local_candidate(url: str) -> bool:
    """Only auto-start for a local URL; remote runners must be managed by the user."""

    try:
        return urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    except ValueError:
        return False


def _start_local_runner() -> bool:
    """Start one detached local runner, with a short duplicate-start guard."""

    if not _auto_start_enabled():
        return False
    script_info = _runner_script()
    if script_info is None:
        return False
    root, script = script_info
    now = time.monotonic()
    with _AUTO_START_LOCK:
        process = _AUTO_START_STATE.get("process")
        if process is not None and process.poll() is None:
            return True
        if now - float(_AUTO_START_STATE.get("at") or 0.0) < AUTO_START_RETRY_S:
            return False
        log = None
        try:
            log_dir = Path.home() / ".sciloop"
            log_dir.mkdir(parents=True, exist_ok=True)
            log = (log_dir / "host-runner.log").open("ab")
            kwargs: dict[str, Any] = {
                "cwd": str(root),
                "stdin": subprocess.DEVNULL,
                "stdout": log,
                "stderr": log,
                "close_fds": True,
            }
            if os.name == "nt":
                flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            # The backend and runner must agree even when configuration came from
            # the process environment instead of the checkout's .env file.
            endpoint = urlsplit(_configured_runner_url())
            port = endpoint.port or 8765
            kwargs["env"] = {**os.environ, "SCILOOP_OWNER_TOKEN": runner_token()}
            process = subprocess.Popen(
                [sys.executable, str(script), "--port", str(port)], **kwargs
            )  # noqa: S603
            # The child owns the duplicated descriptor; close the parent handle.
            log.close()
        except (OSError, ValueError):
            if log is not None:
                with contextlib.suppress(OSError):
                    log.close()
            _AUTO_START_STATE.update({"at": now, "process": None, "root": str(root)})
            return False
        _AUTO_START_STATE.update({"at": now, "process": process, "root": str(root)})
        return True


def runner_url() -> str:
    """`.env` 明确指定（或默认值）——**显式配置优先**。"""

    return (os.environ.get(URL_ENV) or LOCAL_URL).rstrip("/")


def stop_owned_runner() -> None:
    """Release only the executor started by this backend, never an external one."""
    with _AUTO_START_LOCK:
        process = _AUTO_START_STATE.get("process")
        _AUTO_START_STATE.update({"at": 0.0, "process": None, "root": ""})
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=10,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)),
            )
        else:
            import signal

            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)


def _configured_runner_url(*, env_file: Path | None = None) -> str:
    """Read the configured runner endpoint without requiring dotenv in ``os.environ``.

    Uvicorn loads the project settings from ``.env`` through pydantic, but that
    does not populate ``os.environ``.  The runner client must therefore read the
    same value when the app is started directly on the host, while preserving
    ``runner_url()``'s small environment-only contract for callers and tests.
    """

    explicit = (os.environ.get(URL_ENV) or "").strip()
    if explicit:
        return explicit.rstrip("/")
    env_file = env_file or Path(__file__).resolve().parents[3] / ".env"
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == URL_ENV:
                return value.split("#", 1)[0].strip().strip('"').strip("'").rstrip("/")
    except OSError:
        pass
    return LOCAL_URL.rstrip("/")


async def resolve_url(*, client: httpx.AsyncClient | None = None, force: bool = False) -> str:
    """挑一个**能用的**执行环境（研究者 2026-09-24 定的顺序）：

    1. `.env` 里明确写了地址 → 用它（想接自己那台机器的人照旧）；
    2. 本机执行器（`127.0.0.1:8765`，装了开机自启的那个，能力最强：能碰整台机器）。

    2026-10-11：Docker 已下线，`host.docker.internal` 与 compose 的 `executor` 服务两个候选一并移除。
    """

    now = time.monotonic()
    if not force and _PICKED["url"] and now - float(_PICKED["at"] or 0.0) < PICK_TTL_S:
        return str(_PICKED["url"])

    # ⚠️ 显式配置只是**第一个候选**，不是"独占"：
    # 实测踩过——真机执行器进程没了，而 .env 还指着它，于是既不真机也不兜底，一条命令都跑不了。
    explicit = _configured_runner_url()
    candidates = [item for item in (explicit, LOCAL_URL) if item]
    seen: set[str] = set()
    ordered = [item for item in candidates if not (item in seen or seen.add(item))]

    owns = client is None
    probe = client or httpx.AsyncClient(timeout=1.0, trust_env=False)
    try:
        for candidate in ordered:
            try:
                response = await probe.get(f"{candidate.rstrip('/')}/health", timeout=1.0)
                data = response.json() if response.status_code == 200 else None
                if isinstance(data, dict) and data.get("ok") is True:
                    _PICKED.update({"url": candidate.rstrip("/"), "at": now})
                    return str(_PICKED["url"])
            except (httpx.HTTPError, ValueError):
                continue
    finally:
        if owns:
            await probe.aclose()

    _PICKED.update({"url": "", "at": now})
    return (explicit or LOCAL_URL).rstrip("/")


def runner_token() -> str:
    """执行器用**同一枚 Owner 密钥**（研究者 2026-09-22：不要多搞一套密钥）。

    后端校验写操作的那枚令牌，就是调执行器时带的那枚 —— 少一套密钥，
    就少一处"两把钥匙对不上"的故障面。
    """

    from core.config import get_settings

    return (get_settings().owner_token or "").strip()


def _headers() -> dict[str, str]:
    token = runner_token()
    return {"X-SciLoop-Runner-Token": token} if token else {}


def _unreachable(reason: str) -> dict[str, Any]:
    return {"ok": False, "unreachable": True, "error": reason, "hint": START_HINT}


async def health(
    *, client: httpx.AsyncClient | None = None, force: bool = False
) -> dict[str, Any]:
    """执行器在不在？（**不抛异常**：不在也是一种正常状态，要如实告诉研究者。）"""

    owns = client is None
    client = client or httpx.AsyncClient(timeout=1.0, trust_env=False)
    try:
        response = await client.get(f"{await resolve_url(client=client, force=force)}/health")
        if response.status_code != 200:
            return {"reachable": False, "status": response.status_code}
        data = response.json()
        if not isinstance(data, dict) or data.get("ok") is not True:
            return {"reachable": False, "status": response.status_code}
        return {"reachable": True, "info": data}
    except (httpx.HTTPError, ValueError) as exc:
        return {"reachable": False, "error": str(exc)[:200]}
    finally:
        if owns:
            await client.aclose()


async def ensure_available(
    *,
    client: httpx.AsyncClient | None = None,
    wait_s: float = AUTO_START_WAIT_S,
    force: bool = False,
) -> dict[str, Any]:
    """Probe and, for local installs, start the execution service automatically.

    ``client`` is injectable for tests and callers that own a transport. In that case
    no process is spawned. Docker deployments use the compose service candidate and
    simply return once its health endpoint responds.
    """

    owns = client is None
    transport = client or httpx.AsyncClient(timeout=1.0, trust_env=False)
    try:
        result = await health(client=transport, force=force)
        if result.get("reachable"):
            return result
        if owns and not force:
            # A cached endpoint may have died while another endpoint is healthy.
            result = await health(client=transport, force=True)
            if result.get("reachable"):
                return result
        # An injected transport is a test/fake boundary; never launch a real process
        # behind it. Production calls leave client=None and use the real transport.
        if not owns or not _auto_start_enabled():
            return result
        candidate = _configured_runner_url()
        if not _local_candidate(candidate):
            return result
        if not _start_local_runner():
            return result
        deadline = time.monotonic() + max(0.2, float(wait_s))
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            retry = await health(client=transport, force=True)
            if retry.get("reachable"):
                return retry
        return result
    finally:
        if owns:
            await transport.aclose()


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """Execute once; recover before connection, never replay unknown side effects."""

    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s, trust_env=False)
    try:
        if owns:
            available = await ensure_available()
            if not available.get("reachable") and not available.get("status"):
                return _unreachable("本地执行环境暂时不可用")
        for attempt in range(2):
            try:
                response = await client.post(
                    f"{await resolve_url(client=client)}{path}", json=payload, headers=_headers()
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Connection errors occur before the request can execute. Read/
                # write timeouts are deliberately excluded: their side effects
                # are unknown and must never be repeated automatically.
                if attempt or not owns:
                    raise
                _PICKED.update({"url": "", "at": 0.0})
                forget_host_roots()
                recovered = await ensure_available(force=True)
                if not recovered.get("reachable"):
                    raise
        if response.status_code == 401:
            return {
                "ok": False,
                "error": "执行器不认识这串密钥，请在设置页重新填写。",
                "auth_failed": True,
            }
        data = response.json()
        if not isinstance(data, dict):
            return {"ok": False, "error": "执行器返回了看不懂的内容"}
        if "ok" not in data:
            # 执行环境不认识这条接口（老版本）→ 如实说，别让调用方以为"成功但没结果"
            return {"ok": False, "error": f"执行环境不认识这个接口（HTTP {response.status_code}）：{path}", "unknown_route": True}
        return data
    except (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError):
        return {
            "ok": False,
            "outcome_unknown": True,
            "error": "执行请求的结果尚未确认，请先检查实际状态，避免重复操作。",
        }
    except httpx.HTTPError as exc:
        return _unreachable(f"连不上执行器：{str(exc)[:160]}")
    except ValueError:
        return {"ok": False, "error": "执行服务返回内容异常"}
    finally:
        if owns:
            await client.aclose()


async def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_s: float,
    client: httpx.AsyncClient | None,
    ensure_wait_s: float = AUTO_START_WAIT_S,
    allow_ensure: bool = True,
) -> dict[str, Any]:
    """Execute one request（比 `_post` 多支持 GET/DELETE 与受理预算）。

    与 `_post` 同一套纪律：连接前可恢复、结果未知时不重放有副作用操作、
    异常不抛给调用方（转成结构化状态）。
    """

    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s, trust_env=False)
    try:
        if owns and allow_ensure:
            available = await ensure_available(wait_s=ensure_wait_s)
            if not available.get("reachable") and not available.get("status"):
                return _unreachable("本地执行环境暂时不可用")
        for attempt in range(2):
            try:
                response = await client.request(
                    method,
                    f"{await resolve_url(client=client)}{path}",
                    json=payload,
                    headers=_headers(),
                )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt or not owns:
                    raise
                _PICKED.update({"url": "", "at": 0.0})
                forget_host_roots()
                recovered = await ensure_available(force=True, wait_s=ensure_wait_s)
                if not recovered.get("reachable"):
                    raise
        if response.status_code == 401:
            return {
                "ok": False,
                "error": "执行器不认识这串密钥，请在设置页重新填写。",
                "auth_failed": True,
            }
        data = response.json()
        if not isinstance(data, dict):
            return {"ok": False, "error": "执行器返回了看不懂的内容"}
        if "ok" not in data:
            return {
                "ok": False,
                "unknown_route": True,
                "error": f"执行环境不认识这个接口（HTTP {response.status_code}）：{path}",
            }
        return data
    except (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError):
        return {
            "ok": False,
            "outcome_unknown": True,
            "error": "执行请求的结果尚未确认，请先检查实际状态，避免重复操作。",
        }
    except httpx.HTTPError as exc:
        return _unreachable(f"连不上执行器：{str(exc)[:160]}")
    except ValueError:
        return {"ok": False, "error": "执行服务返回内容异常"}
    finally:
        if owns:
            await client.aclose()


async def ensure_host_roots(*, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """确保裁决层知道**宿主视角的路径**（执行器自报；60 秒缓存）。

    为什么必须做：裁决跑在容器里（代码树是 `/app`），而 agent 递过来的是宿主路径
    （`D:/aicoding竞赛/web/...`）。没有宿主根，"删代码=硬拒"会退化成"弹卡"。
    让执行器自报（它知道自己装在哪）比在编排文件里写死盘符更稳：换机器不用改配置。
    拿不到就返回空字典 —— 这不是错误状态，只是"还没连上"。
    """

    now = time.monotonic()
    cached = _ROOTS.get("info")
    if cached is not None and now - float(_ROOTS.get("at") or 0.0) < ROOT_TTL_S:
        return cached

    result = await health(client=client) if client is not None else await ensure_available()
    info = result.get("info") if result.get("reachable") else None
    if isinstance(info, dict):
        policy.set_host_roots(
            host_root=info.get("host_root"),
            project_roots_learned=info.get("project_roots") or (),
        )
        learned: dict[str, Any] = info
    else:
        learned = {}
    # Do not freeze a transient failure into a 60-second false negative.
    _ROOTS["at"] = now if learned else 0.0
    _ROOTS["info"] = learned or None
    return learned


async def is_connected(*, client: httpx.AsyncClient | None = None) -> bool:
    """执行器连得上吗（给设置页与对话用；不抛异常）。"""

    return bool((await ensure_host_roots(client=client)).get("ok"))


async def default_cwd(*, client: httpx.AsyncClient | None = None) -> str | None:
    """模型没指定目录时，默认在哪儿干活（执行器报来的研究项目根目录）。"""

    info = await ensure_host_roots(client=client)
    value = info.get("default_cwd") or info.get("host_root")
    return str(value) if value else None


def forget_host_roots() -> None:
    """忘掉缓存（执行器换机器、或测试隔离时用）。"""

    _ROOTS["at"] = 0.0
    _ROOTS["info"] = None
    policy.clear_host_roots()


async def call_exec(
    *,
    command: str | None = None,
    argv: list[str] | None = None,
    cwd: str | None = None,
    timeout_s: int = 120,
    workspace_root: str | None = None,
    access_mode: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """在研究者自己的电脑上跑一条命令（**已获批之后才调用这里**）。"""

    payload: dict[str, Any] = {"timeout_s": timeout_s}
    if argv:
        payload["argv"] = [str(item) for item in argv]
    else:
        payload["command"] = command or ""
    if cwd:
        payload["cwd"] = cwd
    if workspace_root or cwd:
        payload["workspace_root"] = workspace_root or cwd
    if access_mode:
        payload["access_mode"] = access_mode
    return await _post("/exec", payload, timeout_s=timeout_s + 15, client=client)


async def call_fs(
    *,
    action: str,
    path: str,
    to: str | None = None,
    content: str | None = None,
    recursive: bool = False,
    workspace_root: str | None = None,
    access_mode: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """在研究者自己的电脑上读/写/列/移动/删除文件（**已获批之后才调用这里**）。"""

    payload: dict[str, Any] = {"action": action, "path": path}
    if to:
        payload["to"] = to
    if content is not None:
        payload["content"] = content
    if recursive:
        payload["recursive"] = True
    if workspace_root:
        payload["workspace_root"] = workspace_root
    if access_mode:
        payload["access_mode"] = access_mode
    return await _post("/fs", payload, timeout_s=DEFAULT_TIMEOUT_S, client=client)


async def pick_create(
    *,
    request_id: str,
    title: str = "选择工作目录",
    initial: str = "",
    timeout_s: int = 600,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """请执行环境**建一条目录选择任务**（立刻返回，不等研究员选完）。

    ⚠️ 只等"任务已受理"（3 秒预算）—— 研究员可能在系统窗口里挑几十秒到几分钟，
    那段时间属于任务 `pending`，**不能**占用普通请求的超时（§5.2）。
    """

    return await _request(
        "POST",
        "/pick-folder",
        payload={
            "request_id": request_id,
            "title": title,
            "initial": initial,
            "timeout_s": int(timeout_s),
        },
        timeout_s=PICK_ACCEPT_TIMEOUT_S,
        client=client,
        ensure_wait_s=PICK_ENSURE_WAIT_S,
    )


async def pick_status(*, request_id: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """读一条目录选择任务的状态（终态可重复读同一结果）。"""

    return await _request(
        "GET",
        f"/pick-folder/{quote(str(request_id), safe='')}",
        timeout_s=DEFAULT_TIMEOUT_S,
        client=client,
    )


async def pick_cancel(*, request_id: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """取消一条目录选择任务（让执行器关掉同一个系统窗口；幂等）。"""

    return await _request(
        "POST",
        f"/pick-folder/{quote(str(request_id), safe='')}/cancel",
        payload={},
        timeout_s=DEFAULT_TIMEOUT_S,
        client=client,
    )
