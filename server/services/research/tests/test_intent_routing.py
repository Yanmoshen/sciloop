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
import json
from typing import Any

import pytest

from services.research import dialog, intent


class _FakeResult:
    """`adapter.chat` 的最小替身：只用到 `.content`。"""

    def __init__(self, content: str) -> None:
        self.content = content


@pytest.mark.parametrize(
    "text",
    [
        "请看看当前项目根目录里有哪些文件夹，只列名称，不要修改文件。",
        "帮我列出项目目录下的文件",
        "查看 D:\\research\\project 里面的项目文件",
        "开始查看论文项目文件夹",
        "Please list folders in the project root directory",
    ],
)
def test_filesystem_requests_bypass_platform_database_and_research_router(text: str) -> None:
    assert intent.resolve_intent(text).kind == "chat"
    assert intent.detect_query_topic(text) is None
    assert intent.looks_like_command(text) is False


def test_explicit_platform_projects_query_remains_available() -> None:
    result = intent.resolve_intent("查看平台上我的项目有哪些")
    assert result.kind == "query"
    assert result.topic == "projects"


def test_filesystem_request_skips_model_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected(*args, **kwargs):
        raise AssertionError("filesystem requests must not call the research router model")

    monkeypatch.setattr("llm.adapter.chat", unexpected)
    result = asyncio.run(intent.classify_with_model("请列出项目根目录", model_ref="ds:x", has_chain=False))
    assert result.kind == "chat"


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
    assert routing.kind == "guide"
    assert called is False


@pytest.mark.parametrize("text", [
    "一次预热，七次计时，使用中位数和四分位距，这样的统计足以支持一篇短论文吗？",
    "请帮我分析论文中的统计结果，先不要进行检索。",
    "这个算法是否降低了时间复杂度，可以写进论文吗？",
])
def test_scientific_discussion_is_not_a_library_query(text: str) -> None:
    assert intent.detect_query_topic(text) is None
    assert intent.resolve_intent(text).kind == "chat"


def test_library_query_requires_explicit_local_scope() -> None:
    assert intent.resolve_intent("统计论文库里的论文数量").kind == "query"


def test_explicit_research_starts_without_a_second_confirmation(monkeypatch) -> None:
    async def no_chain(_conversation_id):
        return False

    monkeypatch.setattr(dialog, "has_chain", no_chain)
    record = {}
    first = asyncio.run(dialog.route("开始文献调研，主题是排序算法", "c1", conversation=record))
    assert first.kind == "node"
    assert first.node == "literature_review"
    assert "排序算法" in dialog._research_text("开始文献调研，主题是排序算法", record)


@pytest.mark.parametrize(
    "text",
    [
        "帮我调研有关优化时间复杂度",
        "请你开始文献调研，主题是排序算法",
        "现在搜索图算法的时间复杂度",
    ],
)
def test_explicit_research_language_is_a_node_request(text: str) -> None:
    result = intent.resolve_intent(text)
    assert result.kind == "node"
    assert result.node == "literature_review"


def test_greetings_and_ui_questions_are_not_research_scope() -> None:
    record = {
        "turns": [
            {"role": "user", "content": "你好"},
            {"role": "user", "content": "怎么开始"},
            {"role": "user", "content": "你是谁"},
            {"role": "user", "content": "为什么会有演示入口"},
        ]
    }
    assert dialog._research_text("开始文献调研", record) == ""


def test_start_without_topic_asks_one_question(monkeypatch) -> None:
    async def no_chain(_conversation_id):
        return False

    monkeypatch.setattr(dialog, "has_chain", no_chain)
    record = {}
    result = asyncio.run(dialog.route("开始文献调研", "c1", conversation=record))
    assert result.kind == "guide"
    assert record["research_confirmation_pending"]["node"] == "literature_review"


def test_natural_language_confirmation_steers_active_waiting_run(monkeypatch) -> None:
    """A waiting research turn consumes a full user confirmation in place."""

    async def active_run(_conversation_id):
        return ("literature_review", "waiting_human")

    monkeypatch.setattr(dialog, "_active_research_run", active_run)
    record = {}
    result = asyncio.run(dialog.route(
        "按你的推荐默认执行，开始公开检索和实验，完成后写入论文",
        "c-active",
        conversation=record,
    ))
    assert result.kind == "node"
    assert result.node == "literature_review"
    assert result.keyword == "继续"
    assert record["research_confirmed"] is True


def test_active_run_does_not_reinterpret_unrelated_follow_up(monkeypatch) -> None:
    async def active_run(_conversation_id):
        return ("literature_review", "waiting_human")

    monkeypatch.setattr(dialog, "_active_research_run", active_run)
    result = asyncio.run(dialog.route("这个方法的空间复杂度是多少？", "c-active", conversation={}))
    assert result.kind == "chat"


def test_explicit_next_stage_bypasses_waiting_literature(monkeypatch) -> None:
    async def active_run(_conversation_id):
        return ("literature_review", "waiting_human")

    monkeypatch.setattr(dialog, "_active_research_run", active_run)
    result = asyncio.run(dialog.route(
        "先按主切口继续，完成构思与可行性方案，再进入实验准备",
        "c-active",
        conversation={},
    ))
    assert result.kind == "node"
    assert result.node == "idea_and_feasibility"


def test_deferred_research_never_starts_the_node(monkeypatch) -> None:
    record = {}
    result = asyncio.run(dialog.route("文献调研先别开始，请先确认范围", "c1", conversation=record))
    assert result.kind == "chat"
    assert record["research_confirmation_pending"]["node"] == "literature_review"


def test_real_search_rows_are_streamed_and_persisted_without_internal_rows(monkeypatch):
    search_row = {"kind": "search", "label": "联网检索", "total": 2, "groups": []}

    async def run_node(*_args, **_kwargs):
        yield "attempt", {"attempt": 1, "max_attempts": 3}
        yield "row", {"row": search_row}
        yield "done", {"status": "done", "refs": {}, "next_node": "end"}

    async def conclusion(**_kwargs):
        return "已找到两篇可核对的论文。"

    monkeypatch.setattr("db.session.AsyncSessionLocal", object())
    monkeypatch.setattr(dialog.orchestrator, "run_node", run_node)
    monkeypatch.setattr(dialog, "_write_conclusion", conclusion)
    monkeypatch.setattr(dialog.conversations_service, "write", lambda _record: None)
    record = {"turns": []}

    async def collect():
        return [frame async for frame in dialog.stream_node(
            conversation_id="c1", text="现在开始", scope={"conversation": record, "project_id": None},
            routing=intent.Intent(kind="node", node="literature_review"),
        )]

    frames = asyncio.run(collect())
    rows = [json.loads(frame.split("data: ", 1)[1])["row"] for frame in frames if frame.startswith("event: row")]
    assert search_row in rows
    assert any(row.get("text") == "attempt" for row in rows)
    assert record["turns"][-1]["rows"] == rows
    assert record["agent_history"][-1]["content"] == "已找到两篇可核对的论文。"


def test_stream_node_deadline_emits_terminal_done(monkeypatch):
    """A stalled node must settle the SSE instead of leaving the UI spinning."""

    async def run_node(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        yield "done", {"status": "done", "refs": {}}

    monkeypatch.setattr("db.session.AsyncSessionLocal", object())
    monkeypatch.setattr(dialog.orchestrator, "run_node", run_node)
    monkeypatch.setattr(dialog, "_node_deadline_seconds", lambda: 0.01)
    monkeypatch.setattr(dialog, "_write_conclusion", lambda **_kwargs: pytest.fail("timeout must not start a follow-up model call"))
    monkeypatch.setattr(dialog.conversations_service, "write", lambda _record: None)
    record = {"turns": []}

    async def collect():
        return [frame async for frame in dialog.stream_node(
            conversation_id="c-timeout",
            text="帮我调研算法复杂度",
            scope={"conversation": record, "project_id": None},
            routing=intent.Intent(kind="node", node="literature_review"),
        )]

    frames = asyncio.run(collect())
    events = [frame.splitlines()[0] for frame in frames if frame.startswith("event:")]
    assert "event: done" in events
    joined = "".join(frames)
    assert '"node_status": "waiting_human"' in joined
    assert "research_subscription_timeout" in joined


def test_server_interrupt_stops_active_research_and_skips_conclusion(monkeypatch):
    monkeypatch.setattr("db.session.AsyncSessionLocal", object())
    monkeypatch.setattr(dialog.conversations_service, "write", lambda _record: None)
    conclusion_calls = []

    async def stalled_node(*_args, **_kwargs):
        yield "node", {"node": "literature_review", "entry_index": 1}
        await asyncio.Event().wait()

    async def conclusion(**_kwargs):
        conclusion_calls.append(True)
        return "unexpected"

    monkeypatch.setattr(dialog.orchestrator, "run_node", stalled_node)
    monkeypatch.setattr(dialog, "_write_conclusion", conclusion)
    record = {"turns": []}

    async def collect_and_interrupt():
        task = asyncio.create_task(_collect_stream(record))
        while "c-interrupt" not in dialog._ACTIVE_RESEARCH:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert dialog.request_node_interrupt("c-interrupt") is True
        return await task

    async def _collect_stream(conversation):
        return [frame async for frame in dialog.stream_node(
            conversation_id="c-interrupt",
            text="帮我调研算法复杂度",
            scope={"conversation": conversation, "project_id": None},
            routing=intent.Intent(kind="node", node="literature_review"),
        )]

    frames = asyncio.run(collect_and_interrupt())
    joined = "".join(frames)
    assert "research_interrupted" in joined
    assert '"node_status": "interrupted"' in joined
    assert conclusion_calls == []


def test_search_exceptions_become_structured_results(monkeypatch):
    async def broken_search(_query, **_kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("services.agent.web_search.search_academic", broken_search)
    rows = asyncio.run(dialog.orchestrator._run_academic_queries(["algorithm runtime"]))
    assert rows[0]["ok"] is False
    assert rows[0]["reason"] == "search_error"
    assert rows[0]["sources_failed"][0]["detail"] == "upstream unavailable"
