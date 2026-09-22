# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""宿主工具接进工具通道：逐次裁决 + 走执行器（不启真执行器）。

要钉住三条边界：
1. **不是按工具名一刀切**：同一个"动文件"工具，读=放行、删项目文件=待批准、删代码=硬拒；
2. **被硬拒的事不进批准队列**，也不该发生任何执行；
3. **执行器连不上时不许装死**：模型要能拿到一句人话（引导研究者启动），而不是空结果。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from services.agent import mcp_tools


def _call(name: str, **arguments) -> dict:
    return {
        "id": "call_1",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


# --------------------------------------------------------------------------- #
# 裁决：逐次看"要干什么"
# --------------------------------------------------------------------------- #
def test_read_file_is_allowed_without_bothering_the_researcher(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fs(**_kwargs):
        return {"ok": True, "kind": "file", "content": "hi"}

    monkeypatch.setattr(mcp_tools.host_runner, "call_fs", fake_fs)
    verdict = asyncio.run(mcp_tools.judge(_call("files_on_computer", action="read", path="/tmp/a.txt")))
    assert verdict.allowed


def test_deleting_project_file_needs_approval(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILOOP_PROJECT_ROOTS", str(tmp_path))

    async def fake_fs(**_kwargs):  # pragma: no cover - 删除不需要先查存在
        return {"ok": False}

    monkeypatch.setattr(mcp_tools.host_runner, "call_fs", fake_fs)
    verdict = asyncio.run(
        mcp_tools.judge(_call("files_on_computer", action="delete", path=str(tmp_path / "junk.bin")))
    )
    assert verdict.needs_approval and not verdict.forbidden


def test_deleting_sciloop_code_is_forbidden() -> None:
    target = mcp_tools.policy.sciloop_root() / "web" / "src" / "main.ts"
    verdict = asyncio.run(
        mcp_tools.judge(_call("files_on_computer", action="delete", path=str(target)))
    )
    assert verdict.forbidden


def test_overwriting_existing_file_asks_first(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("SCILOOP_PROJECT_ROOTS", str(tmp_path))

    async def fake_fs(**_kwargs) -> dict:
        return {"ok": True, "kind": "file"}  # 目标已存在

    monkeypatch.setattr(mcp_tools.host_runner, "call_fs", fake_fs)
    verdict = asyncio.run(
        mcp_tools.judge(
            _call("files_on_computer", action="write", path=str(tmp_path / "r.csv"), content="x")
        )
    )
    assert verdict.needs_approval


def test_command_verdicts() -> None:
    assert asyncio.run(mcp_tools.judge(_call("run_on_computer", command="git status"))).allowed
    assert asyncio.run(
        mcp_tools.judge(_call("run_on_computer", command="sudo reboot"))
    ).needs_approval


def test_legacy_tools_keep_their_old_treatment() -> None:
    assert asyncio.run(mcp_tools.judge(_call("query_library", topic="x"))).allowed
    assert asyncio.run(mcp_tools.judge(_call("run_command", command="ls"))).needs_approval


def test_unknown_tool_is_refused() -> None:
    verdict = asyncio.run(mcp_tools.judge(_call("rm_everything")))
    assert verdict.forbidden


# --------------------------------------------------------------------------- #
# 执行路由
# --------------------------------------------------------------------------- #
def test_forbidden_call_never_reaches_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict] = []

    async def fake_exec(**kwargs):  # pragma: no cover - 不该被调用
        called.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_tools.host_runner, "call_exec", fake_exec)
    payload, summary = asyncio.run(
        mcp_tools.run_tool_call(
            _call("run_on_computer", command="rm -rf server/api", cwd=str(mcp_tools.policy.sciloop_root()))
        )
    )
    assert payload["ok"] is False and payload.get("refused") is True
    assert called == [], "被硬拒的调用绝不能真的执行"
    assert "代码" in summary


def test_high_risk_without_approval_is_not_executed(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[dict] = []

    async def fake_exec(**kwargs):
        called.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_tools.host_runner, "call_exec", fake_exec)
    payload, _ = asyncio.run(mcp_tools.run_tool_call(_call("run_on_computer", command="sudo reboot")))
    assert payload["ok"] is False
    assert called == [], "没批准就不该发出去"


def test_approved_command_goes_to_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []

    async def fake_exec(**kwargs):
        seen.append(kwargs)
        return {"ok": True, "exit_code": 0, "stdout": "done\n", "stderr": "", "workdir": "/tmp"}

    monkeypatch.setattr(mcp_tools.host_runner, "call_exec", fake_exec)
    payload, summary = asyncio.run(
        mcp_tools.run_tool_call(
            _call("run_on_computer", command="sudo reboot"), approval_token="tok"
        )
    )
    assert payload["ok"] is True
    assert seen and seen[0]["command"] == "sudo reboot"
    assert summary == "执行完成（退出码 0）"


def test_runner_unreachable_says_it_in_human_words(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(**_kwargs):
        return {
            "ok": False,
            "unreachable": True,
            "error": "连不上执行器",
            "hint": mcp_tools.host_runner.START_HINT,
        }

    monkeypatch.setattr(mcp_tools.host_runner, "call_exec", fake_exec)
    payload, summary = asyncio.run(
        mcp_tools.run_tool_call(_call("run_on_computer", command="echo hi"), approval_token="tok")
    )
    assert payload["ok"] is False
    assert summary == "还没连上这台电脑的执行器"
    # 给模型的内容里要有"怎么启动"，否则它只能干说"失败了"
    assert "host_runner.py" in json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 工具声明
# --------------------------------------------------------------------------- #
def test_host_tools_are_offered_even_when_mcp_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server.client as mcp_client

    async def boom(*_args, **_kwargs):
        raise RuntimeError("mcp server 没起来")

    monkeypatch.setattr(mcp_client, "list_tools", boom)
    schemas = asyncio.run(mcp_tools.tool_schemas())
    names = {item["function"]["name"] for item in schemas}
    assert {"run_on_computer", "files_on_computer"} <= names, (
        "MCP 挂了也必须把宿主工具摆出去，否则模型会以为自己什么都不能做"
    )


def test_host_tool_labels_are_human() -> None:
    for name in mcp_tools.HOST_TOOLS:
        label = mcp_tools.TOOL_LABELS[name]
        assert "_" not in label and "工具" not in label
        assert name not in label
