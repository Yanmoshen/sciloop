# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""结构化输出档位的降档必须**粘住**（同一轮重试不许重发被拒的档位）。

背景（2026-09-22 现场日志）
---------------------------
实验准备节点连发三次请求：`schema_strategy_rejected:json_schema`（412ms）→
一次真实调用（38.9s，JSON 解析失败）→ **又是** `schema_strategy_rejected:json_schema`（139ms）。

根因：``_live_call`` 的 ``strategy`` 只在尝试循环**外**算了一次，而
``_call_with_strategy_fallback`` 是在函数内部把 ``current`` 降下去的。于是每进入下一轮
JSON 重试，都拿旧档位再发一次**已经被上游拒过**的 ``json_schema``：
白烧一次往返（真金白银的 0 token 但仍是请求），日志上表现为同一 purpose 下
两次 ``schema_strategy_rejected`` 夹着一次真实调用。

这里用假 client 把这条链路跑通，断言「schema 档在整轮里只发一次」。
"""

from __future__ import annotations

import asyncio
from typing import Any

from llm import adapter
from llm.errors import LLMBadRequestError
from llm.http_client import ChatOutcome
from llm.providers import reset_strategy_memory
from llm.types import ResolvedModel, Usage

MODEL = ResolvedModel(
    model_ref="deepseek:deepseek-chat",
    provider="deepseek",
    model_id="deepseek-chat",
    base_url="https://api.deepseek.com/v1",
    source="explicit",
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}

SCHEMA_REJECTION = "This response_format type is unavailable now"


class _FakeClient:
    """假的上游：``reject_schema=True`` 时 ``json_schema`` 档一律 400，其余按脚本返回内容。"""

    def __init__(self, contents: list[str], *, reject_schema: bool = True) -> None:
        self.contents = list(contents)
        self.reject_schema = reject_schema
        self.sent: list[dict[str, Any]] = []

    async def chat_completions(self, *, model: ResolvedModel, payload: dict[str, Any]):
        self.sent.append(payload)
        fmt = payload.get("response_format")
        if (
            self.reject_schema
            and isinstance(fmt, dict)
            and fmt.get("type") == "json_schema"
        ):
            raise LLMBadRequestError(
                SCHEMA_REJECTION,
                provider=model.provider,
                model=model.model_id,
                status_code=400,
            )
        content = self.contents.pop(0) if self.contents else "{}"
        return ChatOutcome(
            payload=payload,
            content=content,
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
            model_returned=model.model_id,
        )

    @property
    def schema_calls(self) -> int:
        return sum(
            1
            for payload in self.sent
            if isinstance(payload.get("response_format"), dict)
            and payload["response_format"].get("type") == "json_schema"
        )


class _FakeStore:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def insert_call_log(self, entry: Any) -> None:
        self.entries.append(entry)


def _live_call(client: _FakeClient, store: _FakeStore, *, json_retry: int = 1):
    return asyncio.run(
        adapter._live_call(
            client=client,
            store=store,
            model=MODEL,
            messages=[{"role": "user", "content": "hi"}],
            prompt_hash="hash",
            json_schema=SCHEMA,
            tools=None,
            temperature=0.0,
            max_tokens=64,
            json_retry=json_retry,
            stage="experiment_prep",
            purpose="test",
            project_id=None,
            strict_logging=False,
            fallback_note=None,
            fallback_from=[],
        )
    )


def _reset_provider_memory() -> None:
    reset_strategy_memory()


def test_rejected_schema_strategy_is_not_resent_on_retry() -> None:
    """第 1 轮被拒 → 第 2 轮直接换档，**不再发** json_schema。"""

    _reset_provider_memory()
    # 第 1 轮的降档重发返回坏 JSON（触发第 2 轮），第 2 轮返回合规 JSON
    client = _FakeClient(["not json at all", '{"n": 1}'])
    store = _FakeStore()

    try:
        result = _live_call(client, store, json_retry=1)
    finally:
        _reset_provider_memory()

    assert result.parsed == {"n": 1}
    assert client.schema_calls == 1, f"schema 档被重发了：{client.sent}"
    # 第 2 轮必须仍在结构化约束下（降档到 json_object 或 prompt_only），而不是裸问
    assert client.sent[1].get("response_format") is not None or "system" in [
        m.get("role") for m in client.sent[1].get("messages", [])
    ]


def test_provider_memory_is_reused_across_calls() -> None:
    """同一个进程里，下一次调用一开始就不该再试被拒的档位。"""

    _reset_provider_memory()
    first = _FakeClient(["not json", '{"n": 2}'])
    try:
        _live_call(first, _FakeStore(), json_retry=1)
        second = _FakeClient(["not json", '{"n": 3}'])
        result = _live_call(second, _FakeStore(), json_retry=1)
    finally:
        _reset_provider_memory()

    assert result.parsed == {"n": 3}
    assert first.schema_calls == 1
    assert second.schema_calls == 0, "进程内记忆没被复用"


def test_successful_schema_call_is_still_one_request() -> None:
    """上游接受 schema 档时，第一发就成功——行为与改动前一致（不多发、不降档）。"""

    _reset_provider_memory()
    client = _FakeClient(['{"n": 4}'], reject_schema=False)
    try:
        result = _live_call(client, _FakeStore(), json_retry=1)
    finally:
        _reset_provider_memory()

    assert result.parsed == {"n": 4}
    assert client.schema_calls == 1
    assert len(client.sent) == 1
