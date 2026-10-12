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
from pathlib import Path

import httpx
import pytest

from services.agent import host_runner


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def test_url_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(host_runner.URL_ENV, raising=False)
    assert host_runner.runner_url() == host_runner.LOCAL_URL
    monkeypatch.setenv(host_runner.URL_ENV, "http://127.0.0.1:9000/")
    assert host_runner.runner_url() == "http://127.0.0.1:9000"


def test_configured_runner_url_reads_project_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(host_runner.URL_ENV, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SCILOOP_HOST_RUNNER_URL=http://127.0.0.1:8766 # local runner\n",
        encoding="utf-8",
    )
    assert host_runner._configured_runner_url(env_file=env_file) == "http://127.0.0.1:8766"


def test_resolve_url_prefers_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式配置可达时优先用它（Docker 已下线，不再有 host.docker.internal 别名）。"""

    monkeypatch.setenv(host_runner.URL_ENV, "http://127.0.0.1:8766")
    monkeypatch.setitem(host_runner._PICKED, "url", "")
    monkeypatch.setitem(host_runner._PICKED, "at", 0.0)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.copy_with(path="/health")))
        if request.url.host == "127.0.0.1" and request.url.port == 8766:
            return httpx.Response(200, json={"ok": True})
        raise httpx.ConnectError("unreachable", request=request)

    endpoint = asyncio.run(host_runner.resolve_url(client=_client(handler), force=True))

    assert endpoint == "http://127.0.0.1:8766"
    assert seen == ["http://127.0.0.1:8766/health"]


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


def test_runner_uses_the_single_owner_token() -> None:
    """只有一枚 Owner 密钥（研究者 2026-09-22：「不要搞那么多密钥」）。

    顺带钉住"后端这一侧不许自己生成密钥"：执行器那边也不生成，两边都用 Owner 那一枚。
    """

    import inspect

    source = inspect.getsource(host_runner)
    assert "token_urlsafe" not in source, "不许自己生成密钥"
    assert "TOKEN_ENV" not in source, "不再有执行器专用密钥的环境变量"


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


def test_ensure_available_starts_runner_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次探测失败时，生产路径应自动拉起并在短窗口内恢复。"""

    observed: list[bool] = []

    async def fake_health(*, client=None, force=False):
        observed.append(bool(force))
        return {"reachable": True, "info": {"ok": True}} if len(observed) > 2 else {"reachable": False}

    started: list[bool] = []
    monkeypatch.setattr(host_runner, "health", fake_health)
    monkeypatch.setattr(host_runner, "_start_local_runner", lambda: started.append(True) or True)
    monkeypatch.setattr(host_runner, "AUTO_START_WAIT_S", 1.0)
    monkeypatch.setenv(host_runner.AUTO_START_ENV, "1")

    result = asyncio.run(host_runner.ensure_available())
    assert result["reachable"] is True
    assert started == [True]
    assert observed == [False, True, True]


def test_ensure_available_never_spawns_for_injected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试/自定义 HTTP 传输层不能触发宿主进程副作用。"""

    started: list[bool] = []
    monkeypatch.setattr(host_runner, "_start_local_runner", lambda: started.append(True) or True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"ok": False})

    result = asyncio.run(host_runner.ensure_available(client=_client(handler), wait_s=0.2))
    assert result["reachable"] is False
    assert started == []


def test_health_does_not_accept_an_unrelated_http_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(host_runner._PICKED, "url", "http://127.0.0.1:8766")
    monkeypatch.setitem(host_runner._PICKED, "at", host_runner.time.monotonic())
    result = asyncio.run(host_runner.health(client=_client(lambda request: httpx.Response(200, json={"status": "ok"}))))
    assert result["reachable"] is False


def test_failed_host_roots_are_not_cached_for_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    host_runner.forget_host_roots()
    responses = iter([{"reachable": False}, {"reachable": True, "info": {"ok": True}}])

    async def fake_health(**kwargs):
        return next(responses)

    monkeypatch.setattr(host_runner, "ensure_available", fake_health)
    assert asyncio.run(host_runner.ensure_host_roots()) == {}
    assert asyncio.run(host_runner.is_connected()) is True
    host_runner.forget_host_roots()


def test_connection_refusal_recovers_and_continues_the_same_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    recoveries: list[bool] = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            raise httpx.ConnectError("refused before sending", request=request)
        return httpx.Response(200, json={"ok": True, "written": True})

    transport = _client(handler)
    monkeypatch.setattr(host_runner.httpx, "AsyncClient", lambda **kwargs: transport)

    async def fake_available(**kwargs):
        recoveries.append(kwargs.get("force", False))
        return {"reachable": True}

    async def fake_resolve(**kwargs):
        return "http://127.0.0.1:8766"

    monkeypatch.setattr(host_runner, "ensure_available", fake_available)
    monkeypatch.setattr(host_runner, "resolve_url", fake_resolve)
    result = asyncio.run(host_runner.call_fs(action="write", path="/tmp/note.txt", content="hello"))
    assert result == {"ok": True, "written": True}
    assert calls == ["/fs", "/fs"]
    assert recoveries == [False, True]


def test_read_timeout_does_not_replay_possible_file_write(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("response lost after execution", request=request)

    transport = _client(handler)
    monkeypatch.setattr(host_runner.httpx, "AsyncClient", lambda **kwargs: transport)

    async def fake_available(**kwargs):
        return {"reachable": True}

    async def fake_resolve(**kwargs):
        return "http://127.0.0.1:8766"

    monkeypatch.setattr(host_runner, "ensure_available", fake_available)
    monkeypatch.setattr(host_runner, "resolve_url", fake_resolve)
    result = asyncio.run(host_runner.call_fs(action="write", path="/tmp/note.txt", content="hello"))
    assert result["ok"] is False
    assert result["outcome_unknown"] is True
    assert "unreachable" not in result
    assert calls == ["/fs"]


def test_autostart_uses_environment_endpoint_and_shared_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    launched = []
    monkeypatch.setenv(host_runner.URL_ENV, "http://127.0.0.1:18766")
    monkeypatch.setattr(host_runner, "_runner_script", lambda: (tmp_path, tmp_path / "host_runner.py"))
    monkeypatch.setattr(host_runner, "runner_token", lambda: "shared-token")
    monkeypatch.setattr(host_runner, "_AUTO_START_STATE", {"at": 0.0, "process": None})

    class Process:
        def __init__(self, argv, **kwargs):
            launched.append((argv, kwargs))

        def poll(self):
            return None

    monkeypatch.setattr(host_runner.subprocess, "Popen", Process)
    assert host_runner._start_local_runner() is True
    assert launched[0][0][-2:] == ["--port", "18766"]
    assert launched[0][1]["env"]["SCILOOP_OWNER_TOKEN"] == "shared-token"
    assert host_runner._start_local_runner() is True
    assert len(launched) == 1


def test_remote_name_containing_localhost_is_not_local() -> None:
    assert host_runner._local_candidate("http://localhost.attacker.example:8766") is False


@pytest.mark.parametrize("owned", [True, False])
def test_shutdown_only_stops_owned_executor(monkeypatch: pytest.MonkeyPatch, owned: bool) -> None:
    """真实子进程验证：收回自启执行器，但保留独立启动的服务。"""
    import os
    import subprocess
    import sys

    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **options)
    monkeypatch.setattr(host_runner, "_AUTO_START_STATE", {"process": process if owned else None})
    try:
        host_runner.stop_owned_runner()
        assert (process.poll() is not None) is owned
        assert host_runner._AUTO_START_STATE["process"] is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
