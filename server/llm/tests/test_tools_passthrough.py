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

from llm.adapter import _build_payload, _extract_tool_calls
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
