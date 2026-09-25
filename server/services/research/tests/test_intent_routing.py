# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""意图路由：**规则兜明确的 + 模型兜底**（用户口径 2026-09-25）。

为什么要有模型兜底：规则是**词表子串匹配**（`_find_node` 只做 lower），
别名表里是 `literature_review`（下划线）、动作词只有那几个中文词 + continue/run/start，
于是「start a literature review」这类英文祈使句**必然掉进通用回答**。

兜底的代价（多一次模型调用、多一点延迟）必须花在刀刃上，所以这组测试盯三件事：

1. 只有"像指令"的话才配走兜底 —— 闲聊与提问都不该走（不为闲聊付钱、不加延迟）；
2. 模型给的**任何**不可信值都要降级：不存在的节点名 / 不认识的查询主题 / 解析不出来的输出；
3. 模型失败或超时时**一定回落规则结论**，不能把对话卡在路由上。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services.research import dialog, intent


class _FakeResult:
    """`adapter.chat` 的最小替身：只用到 `.content`。"""

    def __init__(self, content: str) -> None:
        self.content = content


# --------------------------------------------------------------------------- #
# ① 值不值得兜底：只有"像指令"的才走模型
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "开始文献调研",
        "帮我跑一下实验",
        "start a literature review",  # 英文祈使句：规则盖不住，正是要救的那类
        "Please run the results analysis",
        "继续",
    ],
)
def test_looks_like_command_accepts_real_imperatives(text: str) -> None:
    assert intent.looks_like_command(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "你好",
        "谢谢",
        "今天天气不错",
        "文献调研该怎么做？",  # 提问 → 通用回答，不该被当成"要执行"
        "什么是文献综述?",
    ],
)
def test_looks_like_command_rejects_chat_and_questions(text: str) -> None:
    assert intent.looks_like_command(text) is False


# --------------------------------------------------------------------------- #
# ② 模型给的不可信值一律降级
# --------------------------------------------------------------------------- #
def test_model_verdict_unknown_node_degrades_to_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型编一个不存在的节点名 → 按普通对话处理，绝不放行去跑流程。"""

    async def fake_chat(*_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult('{"kind": "node", "node": "delete_everything", "reason": "随便编的"}')

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    judged = asyncio.run(
        intent.classify_with_model("run it", model_ref="ds:deepseek-flash", has_chain=False)
    )
    assert judged is not None
    assert judged.kind == "chat"
    assert "不在清单里" in judged.reason


def test_model_verdict_known_node_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(*_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult(
            '前面夹带一句废话 {"kind": "node", "node": "literature_review", "reason": "祈使句"} 后面也有'
        )

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    judged = asyncio.run(
        intent.classify_with_model("start a literature review", model_ref="ds:x", has_chain=False)
    )
    assert judged is not None
    assert judged.kind == "node"
    assert judged.node == "literature_review", "夹带文字也要能把 JSON 抠出来"


def test_model_verdict_unknown_topic_degrades_to_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(*_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult('{"kind": "query", "topic": "火星上的论文"}')

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    judged = asyncio.run(intent.classify_with_model("查一下", model_ref="ds:x", has_chain=True))
    assert judged is not None and judged.kind == "chat"


def test_model_verdict_unparsable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_chat(*_args: Any, **_kwargs: Any) -> _FakeResult:
        return _FakeResult("我想想……应该是执行吧（没有 JSON）")

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    assert (
        asyncio.run(intent.classify_with_model("跑", model_ref="ds:x", has_chain=False)) is None
    )


def test_model_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时/报错一律 None —— 调用方据此回落规则，不把对话卡住。"""

    async def boom(*_args: Any, **_kwargs: Any) -> _FakeResult:
        raise TimeoutError("超时")

    monkeypatch.setattr("llm.adapter.chat", boom)
    assert asyncio.run(intent.classify_with_model("跑", model_ref="ds:x", has_chain=False)) is None


def test_no_model_ref_skips_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有可用模型时不硬造一次调用（不硬编码供应商）。"""

    called = False

    async def fake_chat(*_args: Any, **_kwargs: Any) -> _FakeResult:
        nonlocal called
        called = True
        return _FakeResult("{}")

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    assert asyncio.run(intent.classify_with_model("跑", model_ref=None, has_chain=False)) is None
    assert called is False


# --------------------------------------------------------------------------- #
# ③ route()：规则优先、兜底只补规则漏掉的、失败回落
# --------------------------------------------------------------------------- #
def test_route_keeps_rule_result_for_plain_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """闲聊不触发兜底（不为闲聊付钱）。"""

    called = False

    async def fake_classify(*_args: Any, **_kwargs: Any) -> intent.Intent:
        nonlocal called
        called = True
        return intent.Intent(kind="node", node="literature_review")

    async def no_chain(_conversation_id: str) -> bool:
        return False

    monkeypatch.setattr(dialog, "has_chain", no_chain)
    monkeypatch.setattr(intent, "classify_with_model", fake_classify)
    routing = asyncio.run(dialog.route("你好呀", "c1", model_ref="ds:x"))
    assert routing.kind == "chat"
    assert called is False, "闲聊不该走模型兜底"


def test_route_uses_model_fallback_for_english_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """英文祈使句：规则判 chat，兜底把它救成 node。"""

    async def fake_classify(*_args: Any, **_kwargs: Any) -> intent.Intent:
        return intent.Intent(kind="node", node="literature_review", keyword="literature_review")

    async def no_chain(_conversation_id: str) -> bool:
        return False

    monkeypatch.setattr(dialog, "has_chain", no_chain)
    monkeypatch.setattr(intent, "classify_with_model", fake_classify)
    routing = asyncio.run(dialog.route("start a literature review", "c1", model_ref="ds:x"))
    assert routing.kind == "node"
    assert routing.node == "literature_review"


def test_route_falls_back_to_rule_when_model_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底失败 = 与没有兜底一样（回落规则），不能报错。"""

    async def fake_classify(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_chain(_conversation_id: str) -> bool:
        return False

    monkeypatch.setattr(dialog, "has_chain", no_chain)
    monkeypatch.setattr(intent, "classify_with_model", fake_classify)
    routing = asyncio.run(dialog.route("start a literature review", "c1", model_ref="ds:x"))
    assert routing.kind == "chat", "模型不可用时回落规则结论（chat），而不是报错"


def test_route_rule_hit_does_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """规则命中的明确命令（中文）零延迟，不调模型。"""

    called = False

    async def fake_classify(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        return None

    async def no_chain(_conversation_id: str) -> bool:
        return False

    monkeypatch.setattr(dialog, "has_chain", no_chain)
    monkeypatch.setattr(intent, "classify_with_model", fake_classify)
    routing = asyncio.run(dialog.route("开始文献调研", "c1", model_ref="ds:x"))
    assert routing.kind == "node"
    assert called is False
