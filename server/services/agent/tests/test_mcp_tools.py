# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""对话侧工具接入的契约测试。

最关键的两条不是"能跑"，而是**边界与诚实**：
1. 写盘/执行类工具**绝不能**出现在自主白名单里 —— 它们只能进 `APPROVABLE_TOOLS`，
   摆给模型是为了让它"提出请求"，**不是**让它自己跑（批准链路见 approvals.py）；
2. 工具失败时**也要把失败回给模型**，否则模型不知道失败，会继续编。
"""

from __future__ import annotations

import asyncio
import json

from services.agent import mcp_tools


# --------------------------------------------------------------------------- #
# 边界：哪些工具能自主调、哪些只能"提出"
# --------------------------------------------------------------------------- #
def test_autonomous_tools_exclude_mutating_ones() -> None:
    """写盘 / 执行类**绝不能**进自主白名单 —— 它们必须走研究者批准。"""

    for forbidden in ("run_command", "write_file", "edit_file"):
        assert forbidden not in mcp_tools.AUTONOMOUS_TOOLS
    # 摆给模型 ≠ 允许自主执行：run_command 在"可提出"名单里，并且必须被标记为需批准
    assert "run_command" in mcp_tools.APPROVABLE_TOOLS
    assert mcp_tools.requires_approval("run_command") is True
    assert mcp_tools.requires_approval("query_library") is False


def test_current_time_is_a_read_only_model_tool() -> None:
    schemas = asyncio.run(mcp_tools.tool_schemas())
    assert "clock__curr_time" in {item["function"]["name"] for item in schemas}
    payload, summary = asyncio.run(
        mcp_tools.run_tool_call({"function": {"name": "clock__curr_time", "arguments": "{}"}})
    )
    assert payload["ok"] is True
    assert payload["result"]["local_time"]
    assert payload["result"]["utc_time"].endswith("+00:00")
    assert "时间" in summary


def test_run_tool_call_refuses_unknown_tools() -> None:
    """即使模型报出一个不在白名单里的工具名，也必须拒绝（模型可能被提示词带偏）。"""

    payload, summary = asyncio.run(
        mcp_tools.run_tool_call({"function": {"name": "delete_everything", "arguments": "{}"}})
    )
    assert payload["ok"] is False
    assert "不允许" in payload["error"]
    assert "白名单" in summary
    assert set(payload) == {"ok", "error"}


def test_run_command_without_token_is_not_executed() -> None:
    """**没有令牌 = 一次 MCP 调用都不发**。

    这里刻意不 mock：真发出去的话，工具侧会真去跑 `echo`（在测试机上也会跑起来），
    所以"没执行"这件事本身就能被观察 —— 返回的是"等批准"，而不是命令输出。
    """

    payload, summary = asyncio.run(
        mcp_tools.run_tool_call(
            {"function": {"name": "run_command", "arguments": '{"argv":["echo","hi"]}'}}
        )
    )
    assert payload["ok"] is False
    assert "批准" in payload["error"]
    assert "批准" in summary
    assert set(payload) == {"ok", "error"}


def test_approval_token_is_never_exposed_to_the_model() -> None:
    """令牌字段**必须从工具声明里抹掉**：留在声明里，模型就可能自己编一个塞进来。"""

    raw = {
        "type": "object",
        "properties": {
            "argv": {"type": "array"},
            "approval_token": {"type": "string"},
            "cwd": {"type": "string"},
        },
        "required": ["argv", "approval_token"],
    }
    cleaned = mcp_tools._public_schema(raw)
    assert "approval_token" not in cleaned["properties"]
    assert set(cleaned["properties"]) == {"argv", "cwd"}
    assert cleaned["required"] == ["argv"]
    # 不改动原对象（免得把工具侧的真实声明改坏）
    assert "approval_token" in raw["properties"]


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


def test_tool_rows_keep_exact_command_and_file_action_for_disclosure() -> None:
    command = {
        "id": "call-17",
        "function": {
            "name": "run_on_computer",
            "arguments": json.dumps({"command": "python -c \"print('ready')\"", "cwd": "D:/work"}),
        },
    }
    row = mcp_tools.tool_row(command, "start")
    assert row["action"] == "python -c \"print('ready')\""
    assert row["call_id"] == "call-17"

    file_call = {
        "function": {
            "name": "files_on_computer",
            "arguments": json.dumps({"action": "move", "path": "draft.md", "to": "archive/draft.md"}),
        }
    }
    assert mcp_tools.tool_row(file_call, "start")["action"] == "move draft.md → archive/draft.md"


def test_execution_details_keep_only_process_output_and_exit_facts() -> None:
    payload = {
        "ok": False,
        "result": {
            "stdout": "partial output",
            "stderr": "command failed",
            "exit_code": 7,
            "timed_out": False,
            "duration_ms": 129,
            "workdir": "D:/work",
            "secret": "must not be serialized",
        },
    }
    details = mcp_tools.tool_execution_details(payload)
    assert details == {
        "stdout": "partial output",
        "stderr": "command failed",
        "exit_code": 7,
        "timed_out": False,
        "duration_ms": 129,
        "workdir": "D:/work",
    }
    assert "secret" not in json.dumps(details)


def test_execution_details_include_bounded_directory_entries() -> None:
    children = [
        {"name": "paper.pdf", "kind": "file", "size": 4096, "secret": "drop"},
        {"name": "figures", "type": "directory", "size": 0},
        "README.md",
    ] + [{"name": f"item-{index}"} for index in range(100)]
    details = mcp_tools.tool_execution_details(
        {"result": {"children": children, "secret": "must not be serialized"}}
    )

    assert details["children_count"] == 103
    assert details["children_truncated"] is True
    assert details["children"][:3] == [
        {"name": "paper.pdf", "kind": "file", "size": 4096},
        {"name": "figures", "kind": "directory", "size": 0},
        {"name": "README.md"},
    ]
    assert len(details["children"]) == 100
    assert "secret" not in json.dumps(details)


def test_execution_details_skip_malformed_directory_entries_and_bound_names() -> None:
    long_name = "x" * 500
    details = mcp_tools.tool_execution_details(
        {"result": {"children": [None, {"path": "/tmp/private"}, {"name": long_name, "type": 3}]}}
    )

    assert details["children_count"] == 3
    assert details["children"] == [{"name": "x" * 240}]
    assert "children_truncated" not in details
    assert "path" not in json.dumps(details)


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
def test_agent_workspace_is_under_knowledge_exports() -> None:
    """Generic agent artifacts live in the configured knowledge base."""

    from services.storage import knowledge_root

    workspace = mcp_tools.agent_workspace()
    assert workspace.name == "agent-workspace"
    assert workspace.parent == knowledge_root() / "exports"


def test_scoped_tool_schemas_do_not_leak_between_projects() -> None:
    raw = [{"function": {"name": "run_on_computer", "description": "执行命令"}}]
    first = mcp_tools._scoped_schemas(json.loads(json.dumps(raw)), "D:/项目甲", {
        "python_env": {"executable": "D:/envs/research/python.exe"},
    })
    second = mcp_tools._scoped_schemas(json.loads(json.dumps(raw)), "D:/项目乙")
    assert "项目甲" in first[0]["function"]["description"]
    assert "research/python.exe" in first[0]["function"]["description"]
    assert "项目甲" not in second[0]["function"]["description"]
    assert "research/python.exe" not in second[0]["function"]["description"]
    assert raw[0]["function"]["description"] == "执行命令"


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
    assert "run_command" in names, "需批准的工具也要摆给模型（由它提出、由研究者批准）"
    for item in schemas:
        assert item["type"] == "function"
        assert item["function"]["parameters"].get("type") == "object"


def test_tool_declarations_carry_the_real_parameters(monkeypatch) -> None:
    """声明的入参**不能是空的**。

    实测过的坑：`client.list_tools()` 早先只带回了 name/description，把 `input_schema`
    丢了（mcp 2.x 是蛇形字段名），于是模型拿到的是"一个不接受任何参数的工具"——
    它只能靠描述文字猜参数名。症状是"工具明明在、却总调不对"，
    而单看每一段代码都"没错"。
    """

    monkeypatch.delenv("SCILOOP_AGENT_ALLOWED_HOSTS", raising=False)
    schemas = {item["function"]["name"]: item["function"]["parameters"] for item in asyncio.run(mcp_tools.tool_schemas())}

    query = schemas["query_library"]["properties"]
    assert {"topic", "text"} <= set(query), query
    assert "topic" in schemas["query_library"]["required"]

    run = schemas["run_command"]
    assert "argv" in run["properties"], "需批准工具的参数也必须带过去，否则模型无从下手"
    assert run["required"] == ["argv"]
    # 真实声明这一层也要确认令牌字段没漏出去（不只是单元测试里的构造对象）
    assert "approval_token" not in run["properties"], "令牌字段不能给模型看"


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
