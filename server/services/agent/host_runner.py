# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""后端 → 宿主执行器的客户端（让 SciLoop 能在研究者自己的电脑上干活）。

拓扑
----
后端跑在容器里，执行器跑在宿主机上（`tools/host-runner/host_runner.py`）。
容器访问宿主用 `host.docker.internal`（Docker Desktop 自带；Linux 需要 compose 里
加一条 `extra_hosts` 映射）。

两条纪律
--------
1. **执行器连不上不许打断对话**：返回一句人话 + 启动指引，让模型照实告诉研究者，
   而不是抛异常把整轮对话打断。
2. **这里不做业务判断**：什么算高危、哪些目录不能删，由 `services/agent/policy.py` 决定；
   本模块只负责"把已经批准的事送过去、把结果拿回来"。
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from services.agent import policy

__all__ = [
    "DEFAULT_URL",
    "START_HINT",
    "call_exec",
    "call_fs",
    "default_cwd",
    "ensure_host_roots",
    "health",
    "is_connected",
    "runner_token",
    "runner_url",
]

#: 容器里访问宿主的默认地址（Linux 下由 compose 的 extra_hosts 提供同名映射）
DEFAULT_URL = "http://host.docker.internal:8765"
URL_ENV = "SCILOOP_HOST_RUNNER_URL"
DEFAULT_TIMEOUT_S = 30.0

#: 宿主路径的学习缓存（`/health` 里带回；60 秒内不重复问）
ROOT_TTL_S = 60.0
_ROOTS: dict[str, Any] = {"at": 0.0, "info": None}

#: 面向研究者的启动指引（**人话，不许出现内部术语**）
START_HINT = (
    "我还没连上你这台电脑上的执行器，所以现在只能读数据、动不了你机器上的文件。"
    "在项目目录里打开一个终端，运行这一行就能连上：\n\n"
    "    python tools/host-runner/host_runner.py\n\n"
    "它用的是你项目里那一枚 Owner 密钥，不用另外配什么。"
    "（没装也能用：`docker compose up` 会带一个容器执行环境，只是它只能碰挂进去的目录。）"
)


#: compose 自带的容器执行环境（零手工；没起真机执行器时用它）
COMPOSE_URL = "http://executor:8765"

#: 挑选结果缓存（避免每条命令都试一遍）
_PICKED: dict[str, Any] = {"url": "", "at": 0.0}
PICK_TTL_S = 20.0


def runner_url() -> str:
    """`.env` 明确指定（或默认值）——**显式配置优先**。"""

    return (os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")


async def resolve_url(*, client: httpx.AsyncClient | None = None, force: bool = False) -> str:
    """挑一个**能用的**执行环境（研究者 2026-09-24 定的顺序）：

    1. `.env` 里明确写了地址 → 用它（想接自己那台机器的人照旧）；
    2. **宿主机执行器**（研究者装了开机自启的那个，能力最强：能碰整台机器）→ 用它；
    3. compose 自带的**容器执行环境**（零手工兜底）→ 用它。

    这样"别人拿去 `docker compose up`"开箱可用，而"想用真机"的人装上自启就自动优先真机。
    """

    explicit = (os.environ.get(URL_ENV) or "").strip()
    if explicit:
        return explicit.rstrip("/")

    now = time.monotonic()
    if not force and _PICKED["url"] and now - float(_PICKED["at"] or 0.0) < PICK_TTL_S:
        return str(_PICKED["url"])

    owns = client is None
    probe = client or httpx.AsyncClient(timeout=3.0)
    try:
        for candidate in (DEFAULT_URL, COMPOSE_URL):
            try:
                response = await probe.get(f"{candidate.rstrip('/')}/health")
                if response.status_code == 200:
                    _PICKED.update({"url": candidate.rstrip("/"), "at": now})
                    return str(_PICKED["url"])
            except httpx.HTTPError:
                continue
    finally:
        if owns:
            await probe.aclose()

    _PICKED.update({"url": "", "at": now})
    return DEFAULT_URL.rstrip("/")


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


async def health(*, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """执行器在不在？（**不抛异常**：不在也是一种正常状态，要如实告诉研究者。）"""

    owns = client is None
    client = client or httpx.AsyncClient(timeout=5.0)
    try:
        response = await client.get(f"{await resolve_url(client=client)}/health")
        if response.status_code != 200:
            return {"reachable": False, "status": response.status_code}
        data = response.json()
        return {"reachable": True, "info": data}
    except httpx.HTTPError as exc:
        return {"reachable": False, "error": str(exc)[:200]}
    finally:
        if owns:
            await client.aclose()


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """统一出口：POST 一次，把三种结果分清楚（成功 / 密钥不对 / 连不上）。"""

    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        response = await client.post(f"{await resolve_url(client=client)}{path}", json=payload, headers=_headers())
        if response.status_code == 401:
            return {
                "ok": False,
                "error": "执行器不认识这串密钥，请在设置页重新填写。",
                "auth_failed": True,
            }
        data = response.json()
        return data if isinstance(data, dict) else {"ok": False, "error": "执行器返回了看不懂的内容"}
    except httpx.HTTPError as exc:
        return _unreachable(f"连不上执行器：{str(exc)[:160]}")
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

    result = await health(client=client)
    info = result.get("info") if result.get("reachable") else None
    if isinstance(info, dict):
        policy.set_host_roots(
            host_root=info.get("host_root"),
            project_roots_learned=info.get("project_roots") or (),
        )
        learned: dict[str, Any] = info
    else:
        learned = {}
    _ROOTS["at"] = now
    _ROOTS["info"] = learned
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
    return await _post("/exec", payload, timeout_s=timeout_s + 15, client=client)


async def call_fs(
    *,
    action: str,
    path: str,
    to: str | None = None,
    content: str | None = None,
    recursive: bool = False,
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
    return await _post("/fs", payload, timeout_s=DEFAULT_TIMEOUT_S, client=client)
