# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""宿主执行器客户端单测（假传输层，不启真执行器）。

要钉住两件事：
1. **连不上不许抛异常**：必须回一句人话 + 启动指引（研究者看到的不能是堆栈）；
2. 401（密钥不对）要和"没启动"分开报 —— 两者的处置完全不同。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from services.agent import host_runner


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_url_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(host_runner.URL_ENV, raising=False)
    assert host_runner.runner_url() == host_runner.DEFAULT_URL
    monkeypatch.setenv(host_runner.URL_ENV, "http://127.0.0.1:9000/")
    assert host_runner.runner_url() == "http://127.0.0.1:9000"


def test_health_reports_reachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"ok": True, "platform": "win32", "version": "0.1.0"})

    result = asyncio.run(host_runner.health(client=_client(handler)))
    assert result["reachable"] is True
    assert result["info"]["version"] == "0.1.0"


def test_health_reports_unreachable_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = asyncio.run(host_runner.health(client=_client(handler)))
    assert result["reachable"] is False
    assert "error" in result


def test_exec_sends_token_and_returns_real_result() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-SciLoop-Runner-Token", "")
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(
            200,
            json={"ok": True, "exit_code": 0, "stdout": "hello\n", "stderr": "", "workdir": "/tmp"},
        )

    result = asyncio.run(
        host_runner.call_exec(command="echo hello", cwd="/tmp", client=_client(handler))
    )
    assert result["ok"] is True and result["stdout"] == "hello\n"
    assert "echo hello" in seen["body"] and "/tmp" in seen["body"]


def test_exec_unreachable_returns_human_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    result = asyncio.run(host_runner.call_exec(command="ls", client=_client(handler)))
    assert result["ok"] is False
    assert result.get("unreachable") is True
    # 给研究者的话必须是"怎么启动"，不是堆栈
    assert "host_runner.py" in result["hint"]
    assert "Traceback" not in json.dumps(result, ensure_ascii=False)


def test_exec_wrong_token_is_reported_separately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "error": "密钥不对"})

    result = asyncio.run(host_runner.call_exec(command="ls", client=_client(handler)))
    assert result["ok"] is False
    assert result["auth_failed"] is True
    assert "设置页" in result["error"]


def test_fs_hits_fs_endpoint_with_its_own_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "kind": "dir", "children": []})

    result = asyncio.run(
        host_runner.call_fs(action="list", path="/home/me/notes", client=_client(handler))
    )
    assert result["ok"] is True
    assert seen["path"] == "/fs"
    assert seen["body"] == {"action": "list", "path": "/home/me/notes"}


def test_fs_delete_carries_recursive_flag() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "deleted": True})

    asyncio.run(
        host_runner.call_fs(
            action="delete", path="/tmp/old-runs", recursive=True, client=_client(handler)
        )
    )
    assert seen["body"]["action"] == "delete"
    assert seen["body"]["recursive"] is True


def test_runner_does_not_read_token_from_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥只来自环境/设置，不许硬编码在仓库里（防止开源时把密钥带出去）。"""

    monkeypatch.delenv(host_runner.TOKEN_ENV, raising=False)
    assert host_runner.runner_token() == ""
    monkeypatch.setenv(host_runner.TOKEN_ENV, " abc ")
    assert host_runner.runner_token() == "abc"


# --------------------------------------------------------------------------- #
# 宿主路径的学习（跨机器可移植的关键）
# --------------------------------------------------------------------------- #
def test_ensure_host_roots_teaches_the_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行器自报的路径要真的落到裁决层，且默认工作目录跟着来。"""

    host_runner.forget_host_roots()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "ok": True,
                "host_root": "D:/somewhere-else/sciloop",
                "project_roots": ["D:/somewhere-else/sciloop/research-workspaces"],
                "default_cwd": "D:/somewhere-else/sciloop/research-workspaces",
            },
        )

    client = _client(handler)
    try:
        info = asyncio.run(host_runner.ensure_host_roots(client=client))
        assert info["host_root"] == "D:/somewhere-else/sciloop"
        # 裁决层立刻认识这个宿主路径
        assert (
            host_runner.policy.classify_layer("D:/somewhere-else/sciloop/web/a.ts")
            == host_runner.policy.LAYER_SCILOOP
        )
        assert asyncio.run(host_runner.default_cwd(client=client)) == info["default_cwd"]

        # 第二次走缓存，不再问执行器（省一次往返）
        asyncio.run(host_runner.ensure_host_roots(client=client))
        assert calls["n"] == 1
    finally:
        host_runner.forget_host_roots()


def test_ensure_host_roots_is_silent_when_runner_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    host_runner.forget_host_roots()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    assert asyncio.run(host_runner.ensure_host_roots(client=client)) == {}
    assert asyncio.run(host_runner.is_connected(client=client)) is False
    assert asyncio.run(host_runner.default_cwd(client=client)) is None
