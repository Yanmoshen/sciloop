# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""LLM 调用链的 tools 透传契约。

只测两件**纯函数**上的事，不拉真模型、不碰网络：

1. 传了 `tools` → 请求体里有 `tools`；**没传 → 一个字节都不加**（现有调用的请求体逐字节不变）。
2. 响应里的 `tool_calls` 能被取回来；**取不到就是空列表，绝不臆造**。

为什么单独立这一份：这两条是 agent 循环的地基。地基若是"看起来对"，上面盖的每一层
都会在真模型上才暴露问题 —— 而那时已经很难判断是透传错了、解析错了，还是模型没调。
"""

from __future__ import annotations

import json

from llm.adapter import (
    _build_payload,
    _extract_tool_calls,
    _merge_tool_call_deltas,
    _normalize_messages,
)
from llm.http_client import extract_tool_call_deltas
from llm.types import LLMResult, ResolvedModel

MODEL = ResolvedModel(
    model_ref="ds:deepseek-flash",
    provider="ds",
    model_id="deepseek-flash",
    base_url="https://example.invalid/v1",
    source="explicit",
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_library",
            "description": "查询论文库",
            "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}},
        },
    }
]


def _payload(tools=None):
    payload, _ = _build_payload(
        model=MODEL,
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
        max_tokens=64,
        json_schema=None,
        strategy="prompt_only",
        stage="chat",
        purpose="test",
        tools=tools,
    )
    return payload


# --------------------------------------------------------------------------- #
# 1) 请求侧：透传，且默认零变化
# --------------------------------------------------------------------------- #
def test_tools_are_attached_when_provided() -> None:
    payload = _payload(TOOLS)
    assert payload["tools"] == TOOLS


def test_no_tools_key_when_not_provided() -> None:
    """不传 tools 时**不能**出现 `"tools": null` —— 有些兼容端会因此报参数错。"""

    assert "tools" not in _payload()
    assert "tools" not in _payload(None)
    assert "tools" not in _payload([])


def test_existing_payload_fields_unchanged() -> None:
    """加了 tools 之后，原有的请求体字段必须一个不少、一个不变。"""

    payload = _payload()
    assert payload["model"] == "deepseek-flash"
    assert payload["stream"] is False
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert "response_format" not in payload


# --------------------------------------------------------------------------- #
# 2) 响应侧：解析，且不臆造
# --------------------------------------------------------------------------- #
def test_extract_tool_calls_from_dict_response() -> None:
    raw = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "query_library", "arguments": '{"topic":"a"}'},
                        }
                    ],
                }
            }
        ]
    }
    calls = _extract_tool_calls(raw)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "query_library"


def test_extract_tool_calls_from_object_with_raw() -> None:
    class Outcome:
        raw = {"choices": [{"message": {"tool_calls": [{"id": "x"}]}}]}

    assert _extract_tool_calls(Outcome()) == [{"id": "x"}]


def test_extract_returns_empty_for_plain_reply() -> None:
    raw = {"choices": [{"message": {"content": "就是普通回答"}}]}
    assert _extract_tool_calls(raw) == []


def test_extract_never_fabricates_on_malformed_response() -> None:
    """畸形响应一律空列表：宁可"没解析到"，也不要编一个 tool_call 出来去执行。"""

    assert _extract_tool_calls(None) == []
    assert _extract_tool_calls({}) == []
    assert _extract_tool_calls({"choices": []}) == []
    assert _extract_tool_calls({"choices": "nope"}) == []
    assert _extract_tool_calls({"choices": [{"message": None}]}) == []
    assert _extract_tool_calls({"choices": [{"message": {"tool_calls": "nope"}}]}) == []
    assert _extract_tool_calls({"choices": [{"message": {"tool_calls": [1, "x"]}}]}) == []


def test_llm_result_defaults_to_no_tool_calls() -> None:
    result = LLMResult(content="hi", model_ref="ds:x", provider="ds", model_id="x")
    assert result.tool_calls == []


# --------------------------------------------------------------------------- #
# 3) 流式分片累积（最容易出错的一段）
# --------------------------------------------------------------------------- #
def test_stream_arguments_are_concatenated_not_overwritten() -> None:
    """**这是最关键的一条**：arguments 分三片到达，必须拼成完整 JSON。

    如果实现写成"后一片覆盖前一片"，拿到的会是半截 JSON —— 表现是
    「工具名对、参数解析失败」，最难定位的一类故障。
    """

    acc: list[dict] = []
    _merge_tool_call_deltas(
        acc, [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "query_library"}}]
    )
    _merge_tool_call_deltas(acc, [{"index": 0, "function": {"arguments": '{"to'}}])
    _merge_tool_call_deltas(acc, [{"index": 0, "function": {"arguments": 'pic":"a"}'}}])

    assert len(acc) == 1
    assert acc[0]["id"] == "call_1"
    assert acc[0]["function"]["name"] == "query_library"
    assert acc[0]["function"]["arguments"] == '{"topic":"a"}'
    json.loads(acc[0]["function"]["arguments"])  # 必须是合法 JSON


def test_stream_keeps_id_and_name_from_first_fragment() -> None:
    """后续分片不带 id/name，不能被清空。"""

    acc: list[dict] = []
    _merge_tool_call_deltas(acc, [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": "{"}}])
    _merge_tool_call_deltas(acc, [{"index": 0, "function": {"arguments": "}"}}])
    assert (acc[0]["id"], acc[0]["function"]["name"]) == ("c1", "f")


def test_stream_multiple_calls_are_kept_apart_by_index() -> None:
    acc: list[dict] = []
    _merge_tool_call_deltas(acc, [{"index": 0, "id": "a", "function": {"name": "x"}}])
    _merge_tool_call_deltas(acc, [{"index": 1, "id": "b", "function": {"name": "y"}}])
    _merge_tool_call_deltas(acc, [{"index": 0, "function": {"arguments": "1"}}])
    _merge_tool_call_deltas(acc, [{"index": 1, "function": {"arguments": "2"}}])
    assert [c["function"]["arguments"] for c in acc] == ["1", "2"]


def test_stream_tolerates_gapped_and_missing_index() -> None:
    """跳号的 index 要补齐空洞；没有 index 的网关按到达顺序追加。"""

    acc: list[dict] = []
    _merge_tool_call_deltas(acc, [{"index": 2, "id": "c2", "function": {"name": "z"}}])
    assert len(acc) == 3 and acc[2]["id"] == "c2"
    assert acc[0]["function"]["arguments"] == ""

    _merge_tool_call_deltas(acc, [{"function": {"name": "noidx"}}])
    assert acc[-1]["function"]["name"] == "noidx"


def test_stream_merge_ignores_garbage() -> None:
    acc: list[dict] = []
    for junk in (None, "nope", 42, [1, "x", None], [{"index": "bad"}]):
        _merge_tool_call_deltas(acc, junk)
    # `[{"index": "bad"}]` 会按「无 index」追加一条，其余全部忽略
    assert len(acc) == 1
    assert acc[0]["function"]["arguments"] == ""


def test_extract_tool_call_deltas_shapes() -> None:
    chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1"}]}}]}
    assert extract_tool_call_deltas(chunk) == [{"index": 0, "id": "c1"}]
    # 正文分片里没有 tool_calls → 空列表，而不是报错
    assert extract_tool_call_deltas({"choices": [{"delta": {"content": "hi"}}]}) == []
    assert extract_tool_call_deltas({"choices": [{"delta": {"tool_calls": "nope"}}]}) == []
    assert extract_tool_call_deltas({}) == []
    assert extract_tool_call_deltas({"choices": [{"delta": {"tool_calls": [1, {"index": 0}]}}]}) == [
        {"index": 0}
    ]


# --------------------------------------------------------------------------- #
# 4) 工具回合的消息字段不能被 _normalize_messages 洗掉（真因就在这）
# --------------------------------------------------------------------------- #
def test_normalize_keeps_tool_calls_and_tool_call_id() -> None:
    """这是本轮端到端的真根因，必须锁住。

    `_normalize_messages` 原本只保留 role/content/name，于是 assistant 的 `tool_calls`
    与 tool 的 `tool_call_id` 被**静默洗掉**，供应商报的是
    `missing field tool_call_id` / `bad_request` —— 报错指向错误的方向，极难定位。
    """

    normalized = _normalize_messages(
        [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_0", "type": "function",
                                "function": {"name": "query_library", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_0", "content": "{}"},
        ]
    )
    assert normalized[2]["tool_calls"][0]["id"] == "call_0"
    assert normalized[3]["tool_call_id"] == "call_0"
    # 要求调工具时 content 本就该缺省 —— 硬塞空串会被部分兼容端判参数错
    assert "content" not in normalized[2]


def test_normalize_keeps_plain_messages_unchanged() -> None:
    """普通消息的行为一个字节都不变：仍然补 content=""、仍然带 name。"""

    normalized = _normalize_messages([{"role": "user"}, {"role": "user", "content": "x", "name": "n"}])
    assert normalized[0] == {"role": "user", "content": ""}
    assert normalized[1] == {"role": "user", "content": "x", "name": "n"}
    assert _normalize_messages("hi") == [{"role": "user", "content": "hi"}]
