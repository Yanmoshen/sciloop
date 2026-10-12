"""Regression coverage for providers that ignore the streaming request."""

from __future__ import annotations

import json

import pytest

import llm.adapter as adapter
from llm.adapter import chat_stream
from llm.http_client import OpenAICompatibleClient, extract_delta, extract_tool_call_deltas
from llm.types import ResolvedModel

MODEL = ResolvedModel(
    model_ref="fake:model",
    provider="fake",
    model_id="model",
    base_url="https://example.invalid/v1",
    api_key="secret",
    source="explicit",
)


class _JsonResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def aread(self) -> bytes:
        return json.dumps(self.payload).encode()

    async def aiter_lines(self):
        # A JSON response has no SSE lines. The client must use ``aread``.
        if False:
            yield ""


class _StreamContext:
    def __init__(self, response: _JsonResponse) -> None:
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _HttpClient:
    def __init__(self, response: _JsonResponse) -> None:
        self.response = response

    def stream(self, *_args, **_kwargs):
        return _StreamContext(self.response)


@pytest.mark.asyncio
async def test_http_client_adapts_json_completion_to_one_stream_chunk() -> None:
    payload = {"choices": [{"message": {"content": "普通回答"}, "finish_reason": "stop"}]}
    client = OpenAICompatibleClient(client=_HttpClient(_JsonResponse(payload)))
    chunks = [
        item async for item in client.chat_completions_stream(model=MODEL, payload={"stream": True})
    ]
    assert chunks[0]["_sciloop_stream_mode"] == "adapted"
    assert extract_delta(chunks[0])[0] == "普通回答"


def test_message_shape_is_accepted_for_adapted_content_and_tools() -> None:
    chunk = {
        "choices": [
            {
                "message": {
                    "content": "需要搜索",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "search.query", "arguments": '{"q":"x"}'},
                        }
                    ],
                }
            }
        ]
    }
    assert extract_delta(chunk)[0] == "需要搜索"
    calls = extract_tool_call_deltas(chunk)
    assert calls[0]["index"] == 0
    assert calls[0]["id"] == "call-1"


@pytest.mark.asyncio
async def test_adapter_emits_stable_deltas_for_one_shot_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve_chain(*_args, **_kwargs):
        return [MODEL]

    async def no_log(*_args, **_kwargs):
        return None

    async def one_shot(*_args, **_kwargs):
        yield {
            "_sciloop_stream_mode": "adapted",
            "choices": [{"message": {"content": "这是一段较长的普通回答。"}, "finish_reason": "stop"}],
        }

    monkeypatch.setattr(adapter, "_resolve_chain", resolve_chain)
    monkeypatch.setattr(adapter, "_log_attempts", no_log)

    class Transport:
        async def chat_completions_stream(self, **_kwargs):
            async for item in one_shot():
                yield item

    updates = [
        item
        async for item in chat_stream(
            [{"role": "user", "content": "你好"}],
            model_ref="fake:model",
            store=object(),
            router=object(),
            transport=Transport(),
        )
    ]
    deltas = [item.text for item in updates if item.kind == "delta"]
    assert deltas
    assert "".join(deltas) == "这是一段较长的普通回答。"
    assert all(len(item) <= 48 for item in deltas)
    done = updates[-1]
    assert done.kind == "done"
    assert done.result is not None
    assert done.result.raw["streaming_mode"] == "adapted"


@pytest.mark.asyncio
async def test_adapter_discards_late_content_after_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve_chain(*_args, **_kwargs):
        return [MODEL]

    async def no_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(adapter, "_resolve_chain", resolve_chain)
    monkeypatch.setattr(adapter, "_log_attempts", no_log)

    class Transport:
        async def chat_completions_stream(self, **_kwargs):
            yield {"choices": [{"delta": {"content": "完成"}, "finish_reason": "stop"}]}
            yield {"choices": [{"delta": {"content": "迟到内容"}}]}

    updates = [
        item
        async for item in chat_stream(
            [{"role": "user", "content": "你好"}],
            model_ref="fake:model",
            store=object(),
            router=object(),
            transport=Transport(),
        )
    ]
    assert "".join(item.text for item in updates if item.kind == "delta") == "完成"
