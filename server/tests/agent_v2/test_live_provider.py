"""真实供应商适配器的协议边界测试（HTTP 层完全替身化）。"""

from __future__ import annotations

import asyncio

import pytest

import services.model_gateway_v2.provider as provider_module
from contracts.agent_v2.cancellation import CancelledError, CancelToken
from contracts.agent_v2.enums import StopReason
from contracts.agent_v2.models import (
    ModelRequest,
    StreamCompleted,
    StreamError,
    TextDelta,
    ToolCallCompleted,
    Usage,
)
from llm.adapter import StreamUpdate
from llm.types import LLMResult
from llm.types import Usage as LLMUsage
from services.model_gateway_v2.provider import RegisteredModelProvider


def request(*, tools: list[dict] | None = None) -> ModelRequest:
    item = ModelRequest(
        request_id="req-1",
        messages=[{"role": "user", "content": "hello"}],
        model="test:model",
        tools=tools or [],
        params={"temperature": 0.1},
    )
    # metadata is deliberately an extension point outside the frozen wire schema.
    item.metadata = {"model": "override:model"}
    return item


async def collect(provider: RegisteredModelProvider, req: ModelRequest, cancel=None):
    return [item async for item in provider.stream(req, cancel)]


@pytest.mark.asyncio
async def test_text_and_tool_round_trip_preserves_messages_and_tools(monkeypatch):
    seen = {}

    async def fake_chat(messages, **kwargs):
        seen["messages"] = messages
        seen["tools"] = kwargs["tools"]
        yield StreamUpdate(kind="delta", text="answer")
        yield StreamUpdate(
            kind="done",
            result=LLMResult(
                content="answer",
                model_ref="override:model",
                provider="override",
                model_id="model",
                usage=LLMUsage(prompt_tokens=3, completion_tokens=2),
                finish_reason="tool_calls",
                tool_calls=[
                    {"id": "call-1", "function": {"name": "search.query", "arguments": '{"q":"x"}'}}
                ],
            ),
        )

    monkeypatch.setattr(provider_module, "chat_stream", fake_chat)
    tool_spec = {"type": "function", "function": {"name": "search.query", "parameters": {}}}
    items = await collect(RegisteredModelProvider(), request(tools=[tool_spec]))

    assert isinstance(items[0], TextDelta)
    assert any(isinstance(item, Usage) for item in items)
    call = next(item for item in items if isinstance(item, ToolCallCompleted))
    assert call.name == "search.query"
    assert call.arguments == {"q": "x"}
    assert isinstance(items[-1], StreamCompleted)
    assert items[-1].stop_reason == StopReason.TOOL_USE
    assert seen["messages"][0]["role"] == "user"
    assert seen["tools"][0]["type"] == "function"
    assert seen["tools"][0]["function"]["name"] == "search_query"


@pytest.mark.asyncio
async def test_cancel_interrupts_in_flight_provider_stream(monkeypatch):
    started = asyncio.Event()

    async def fake_chat(messages, **kwargs):
        started.set()
        await asyncio.sleep(60)
        yield StreamUpdate(kind="done", result=None)

    monkeypatch.setattr(provider_module, "chat_stream", fake_chat)
    token = CancelToken()
    task = asyncio.create_task(collect(RegisteredModelProvider(), request(), token))
    await started.wait()
    token.cancel("user stopped")
    with pytest.raises(CancelledError):
        await task


@pytest.mark.asyncio
async def test_missing_completion_is_a_protocol_error(monkeypatch):
    async def fake_chat(messages, **kwargs):
        yield StreamUpdate(kind="delta", text="truncated")

    monkeypatch.setattr(provider_module, "chat_stream", fake_chat)
    items = await collect(RegisteredModelProvider(), request())
    assert isinstance(items[0], TextDelta)
    assert isinstance(items[-1], StreamError)
    assert items[-1].error_class == "fatal"


@pytest.mark.asyncio
async def test_provider_exception_is_classified_without_fallback(monkeypatch):
    async def fake_chat(messages, **kwargs):
        raise ConnectionError("upstream unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(provider_module, "chat_stream", fake_chat)
    items = await collect(RegisteredModelProvider(), request())
    assert len(items) == 1
    assert isinstance(items[0], StreamError)
    assert items[0].error_class == "retryable"
    assert "upstream unavailable" in items[0].message
