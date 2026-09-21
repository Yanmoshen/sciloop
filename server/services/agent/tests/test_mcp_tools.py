# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话侧工具接入的契约测试。

最关键的两条不是"能跑"，而是**边界与诚实**：
1. 自主白名单里**不能**出现写盘/执行类工具（摆出去却没有审批链路 = 把边界开了个洞）；
2. 工具失败时**也要把失败回给模型**，否则模型不知道失败，会继续编。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from services.agent import mcp_tools


# --------------------------------------------------------------------------- #
# 边界：哪些工具能自主调
# --------------------------------------------------------------------------- #
def test_autonomous_tools_exclude_mutating_ones() -> None:
    """写盘 / 执行类**绝不能**进自主白名单 —— 它们需要研究者批准，而批准链路还没建。"""

    for forbidden in ("run_command", "write_file", "edit_file"):
        assert forbidden not in mcp_tools.AUTONOMOUS_TOOLS


def test_run_tool_call_refuses_unknown_and_forbidden_tools() -> None:
    """即使模型报出一个不在白名单里的工具名，也必须拒绝（模型可能被提示词带偏）。"""

    payload, summary = asyncio.run(
        mcp_tools.run_tool_call({"function": {"name": "run_command", "arguments": "{}"}})
    )
    assert payload["ok"] is False
    assert "不允许" in payload["error"] or "白名单" in summary
    assert set(payload) == {"ok", "error"}


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def test_arguments_accepts_json_string() -> None:
    call = {"function": {"name": "query_library", "arguments": '{"topic":"a","text":"b"}'}}
    assert mcp_tools._arguments(call) == {"topic": "a", "text": "b"}


def test_arguments_tolerates_malformed_and_missing() -> None:
    """参数烂掉时返回空字典，交给工具侧按缺必填去拒 —— **不在这里编参数**。"""

    assert mcp_tools._arguments({}) == {}
    assert mcp_tools._arguments({"function": {"arguments": "{不是 JSON"}}) == {}
    assert mcp_tools._arguments({"function": {"arguments": ""}}) == {}
    assert mcp_tools._arguments({"function": {"arguments": {"k": 1}}}) == {"k": 1}
    assert mcp_tools._arguments({"function": {"arguments": "[1,2]"}}) == {}


# --------------------------------------------------------------------------- #
# 呈现（走现有 SSE row 事件）
# --------------------------------------------------------------------------- #
def test_tool_row_has_tone_and_label() -> None:
    call = {"function": {"name": "query_library", "arguments": "{}"}}
    assert mcp_tools.tool_row(call, "start")["tone"] == "info"
    assert "查询论文库" in mcp_tools.tool_row(call, "start")["text"]
    assert mcp_tools.tool_row(call, "ok", "命中 3 条")["tone"] == "ok"
    assert "3 条" in mcp_tools.tool_row(call, "ok", "命中 3 条")["text"]
    assert mcp_tools.tool_row(call, "err", "超时")["tone"] == "warn"


def test_row_kind_is_tool_so_frontend_renders_it_as_system_row() -> None:
    """前端按 `row.kind` 分类渲染；这里必须与既有的节点过程行同构。"""

    row = mcp_tools.tool_row({"function": {"name": "fetch_url"}}, "start")
    assert row["kind"] == "tool"
    assert isinstance(row["text"], str) and row["text"]


def test_tool_message_content_truncates_but_says_so() -> None:
    big = {"text": "x" * 20000}
    content = mcp_tools.tool_message_content(big)
    assert len(content) < 20000
    assert "已截断" in content


def test_tool_message_content_is_json_roundtrippable() -> None:
    content = mcp_tools.tool_message_content({"ok": True, "tool": "query_library", "n": 3})
    assert json.loads(content)["n"] == 3


# --------------------------------------------------------------------------- #
# 工作区
# --------------------------------------------------------------------------- #
def test_agent_workspace_is_under_server_cache() -> None:
    """工作区必须是 `server/.cache/agent-workspace`（与 ingest/reader 的 .cache 口径一致），
    免得 agent 把文件写到别处。"""

    workspace = mcp_tools.agent_workspace()
    assert workspace.name == "agent-workspace"
    assert workspace.parent.name == ".cache"
    assert Path(mcp_tools.__file__).resolve().parents[2] == workspace.parent.parent


def test_fetch_url_is_hidden_without_allowed_hosts(monkeypatch) -> None:
    """没配出网白名单就不把 fetch_url 摆给模型 —— 摆了也一定会被边界拒，不如不摆。"""

    monkeypatch.delenv("SCILOOP_AGENT_ALLOWED_HOSTS", raising=False)
    assert mcp_tools._allowed_hosts() == ()
    monkeypatch.setenv("SCILOOP_AGENT_ALLOWED_HOSTS", "Arxiv.org, api.example")
    assert mcp_tools._allowed_hosts() == ("arxiv.org", "api.example")


# --------------------------------------------------------------------------- #
# 真跑一次：工具声明来自**运行中的 MCP server**（会拉起子进程）
# --------------------------------------------------------------------------- #
def test_tool_schemas_come_from_a_real_mcp_handshake(monkeypatch) -> None:
    """声明不是硬编码的，是走一次 MCP 握手拿回来的。

    这条会真的起 `python -m mcp_server.server` 子进程 —— 只有它能证明"对话侧接对了协议"。
    """

    monkeypatch.delenv("SCILOOP_AGENT_ALLOWED_HOSTS", raising=False)
    schemas = asyncio.run(mcp_tools.tool_schemas())
    names = {item["function"]["name"] for item in schemas}

    assert "query_library" in names, names
    assert "fetch_url" not in names, "没配出网白名单时不该摆联网工具"
    for item in schemas:
        assert item["type"] == "function"
        assert item["function"]["parameters"].get("type") == "object"


# --------------------------------------------------------------------------- #
# 回喂消息的形状（实测在这里踩过 bad_request）
# --------------------------------------------------------------------------- #
def test_normalize_fills_missing_id_and_type() -> None:
    """`id` 缺失时不能带 `null` 出去 —— 部分兼容端直接判参数错。"""

    calls = mcp_tools.normalize_tool_calls(
        [{"function": {"name": "query_library", "arguments": "{}"}}]
    )
    assert calls[0]["id"] == "call_0"
    assert calls[0]["type"] == "function"


def test_normalize_drops_nameless_calls_and_serializes_arguments() -> None:
    calls = mcp_tools.normalize_tool_calls(
        [
            {"id": "a", "function": {"arguments": "{}"}},  # 没 name → 无法执行，丢掉
            {"id": "b", "function": {"name": "fetch_url", "arguments": {"url": "u"}}},
        ]
    )
    assert [c["id"] for c in calls] == ["b"]
    # dict 形式的 arguments 要序列化成字符串（协议要求 string）
    assert json.loads(calls[0]["function"]["arguments"]) == {"url": "u"}


def test_normalize_ignores_garbage() -> None:
    assert mcp_tools.normalize_tool_calls(None) == []
    assert mcp_tools.normalize_tool_calls("nope") == []
    assert mcp_tools.normalize_tool_calls([1, "x", None]) == []


def test_assistant_tool_message_omits_empty_content() -> None:
    """**空 content 整个字段都不写** —— 写成 `content: ""` 会被判 bad_request，
    随后降级链切到没 key 的供应商，报成一句与真因无关的「env 未配置 API Key」。"""

    calls = mcp_tools.normalize_tool_calls([{"function": {"name": "query_library"}}])
    assert "content" not in mcp_tools.assistant_tool_message("", calls)
    assert mcp_tools.assistant_tool_message("先说一句", calls)["content"] == "先说一句"
