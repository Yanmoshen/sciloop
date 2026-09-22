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
from typing import Any

import httpx

__all__ = [
    "DEFAULT_URL",
    "START_HINT",
    "call_exec",
    "call_fs",
    "health",
    "runner_url",
    "runner_token",
]

#: 容器里访问宿主的默认地址（Linux 下由 compose 的 extra_hosts 提供同名映射）
DEFAULT_URL = "http://host.docker.internal:8765"
URL_ENV = "SCILOOP_HOST_RUNNER_URL"
TOKEN_ENV = "SCILOOP_HOST_RUNNER_TOKEN"
DEFAULT_TIMEOUT_S = 30.0

#: 面向研究者的启动指引（**人话，不许出现内部术语**）
START_HINT = (
    "我还没连上你这台电脑上的执行器，所以现在只能读数据、动不了你机器上的文件。"
    "在项目目录里打开一个终端，运行这一行就能连上：\n\n"
    "    python tools/host-runner/host_runner.py\n\n"
    "它启动后会打印一串密钥，把那串密钥填到设置页里就可以了。"
)


def runner_url() -> str:
    return (os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")


def runner_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


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
        response = await client.get(f"{runner_url()}/health")
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
        response = await client.post(f"{runner_url()}{path}", json=payload, headers=_headers())
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
