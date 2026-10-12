"""用户可见正文的回归测试。

这些测试不调用模型或执行器，只锁住两条产品边界：社会性短消息应立即得到
有用的 SciLoop 介绍；供应商误把 scratchpad 放进正文时，不能把它送进页面。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from api.v1 import chat as chat_api
from llm.http_client import _extract_content


@pytest.mark.parametrize("text", [
    "详细介绍一下你现在有哪些工具，按类别说清楚它们叫什么、能做什么。",
    "介绍一下你的工具列表", "你是谁，并介绍工具分类", "请你介绍一下你并帮我创建项目",
])
def test_tool_or_task_questions_are_not_replaced_by_identity_template(text: str) -> None:
    assert not chat_api._is_simple_question(text)


def test_scratchpad_markers_are_detected_before_they_reach_the_ui() -> None:
    leaked = (
        "The user asks what tools I can call. Per instructions, I should list them. "
        "Should I ask questions? Don't reveal thinking."
    )

    assert chat_api._looks_like_internal_reasoning(leaked)
    assert chat_api._sanitize_assistant_content(leaked) == chat_api._INTERNAL_FAILURE_REPLY


def test_tool_diagnostic_does_not_erase_completed_prefix() -> None:
    content = (
        "已完成目录分析。\n\nAll connection attempts failed while running files_on_computer"
        "\n\n我会继续介绍搜索和知识库功能。"
    )

    cleaned = chat_api._sanitize_assistant_content(content)

    assert cleaned.startswith("已完成目录分析。")
    assert "All connection attempts failed" not in cleaned
    assert "服务连接失败" in cleaned
    assert cleaned.endswith("我会继续介绍搜索和知识库功能。")
    assert "自动恢复" not in cleaned


@pytest.mark.parametrize("tool", ["files_on_computer", "run_on_computer", "tool_call_id", "request_id", "reasoning_content"])
def test_tool_or_protocol_descriptions_do_not_invent_a_failure(tool: str) -> None:
    answer = f"我可以使用 `{tool}`。下面介绍搜索、文件管理和知识库功能。"
    assert chat_api._sanitize_assistant_content(answer) == answer


@pytest.mark.parametrize("marker,replacement", chat_api._INTERNAL_DIAGNOSTIC_REPLACEMENTS)
def test_diagnostic_filter_handles_every_chunk_boundary(marker: str, replacement: str) -> None:
    answer = f"已有结果。{marker}。我会继续说明下一步。"
    expected = f"已有结果。{replacement}。我会继续说明下一步。"
    for boundary in range(len(answer) + 1):
        cleaner = chat_api._DiagnosticTextFilter()
        visible = cleaner.feed(answer[:boundary]) + cleaner.feed(answer[boundary:])
        visible += cleaner.feed("", final=True)
        assert visible == expected
        assert chat_api._sanitize_assistant_content(visible) == visible


def test_streaming_tool_description_finishes_without_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        "我按类别说明工具。\n电脑上的文件：`files_",
        "on_computer` 用于读写文件；`run_on_",
        "computer` 用于执行命令。\n",
        "此外还有网页检索、学术搜索和知识库管理。",
    ]
    _assert_stream_matches_saved(monkeypatch, chunks, "".join(chunks))


def test_streaming_actual_diagnostic_keeps_following_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = ["已完成分析。All connection ", "attempts failed。", "后面的功能介绍仍需完整保留。"]
    _assert_stream_matches_saved(
        monkeypatch, chunks, "已完成分析。服务连接失败。后面的功能介绍仍需完整保留。"
    )


def test_streaming_flushes_possible_marker_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_stream_matches_saved(monkeypatch, ["这是普通的 error"], "这是普通的 error")


@pytest.mark.parametrize("text", ["请介绍如何创建项目", "能否帮我写一份文献笔记", "帮我在目录中新建一个文件"])
def test_chat_intent_never_short_circuits_on_executor_health(monkeypatch, text: str) -> None:
    from services.agent import host_runner

    async def fail_probe():
        pytest.fail("A natural-language request must not probe/terminate before any tool call")

    async def model_ref(*_args):
        return "fake:model"

    async def workspace(*_args):
        return None

    routed = []

    async def route(*_args, **_kwargs):
        routed.append(text)
        return SimpleNamespace(kind="answer")

    monkeypatch.setattr(host_runner, "ensure_available", fail_probe)
    monkeypatch.setattr(chat_api, "_resolve_model_ref", model_ref)
    monkeypatch.setattr(chat_api, "_workspace_dir", workspace)
    monkeypatch.setattr(chat_api.dialog, "route", route)
    monkeypatch.setattr(chat_api.conversations, "create", lambda **_kwargs: {"id": "intent-test", "turns": []})

    response = asyncio.run(chat_api.home_chat_stream(
        chat_api.HomeChatRequest(text=text, model_config_id=1, model_id="fake"),
        SimpleNamespace(),
    ))
    assert response.media_type == "text/event-stream"
    assert routed == [text]


def _assert_stream_matches_saved(monkeypatch, chunks: list[str], expected: str) -> None:
    result = SimpleNamespace(content="".join(chunks), tool_calls=[], raw={}, usage=None)

    async def fake_stream(*_args, **_kwargs):
        for chunk in chunks:
            yield SimpleNamespace(kind="delta", text=chunk)
        yield SimpleNamespace(kind="done", result=result)

    monkeypatch.setattr(chat_api.adapter, "chat_stream", fake_stream)
    state = {"text": [], "reasoning": [], "result": None, "pending": None}
    messages = [{"role": "user", "content": "介绍一下你能用哪些工具"}]

    async def collect():
        return [
            frame async for frame in chat_api._agent_loop(
                messages, conversation_id="tool-description-regression", ref="fake",
                tool_defs=[], rows=[], approvals_out=[], state=state,
            )
        ]

    frames = asyncio.run(collect())
    deltas = [
        json.loads(frame.split("data: ", 1)[1])["text"]
        for frame in frames if frame.startswith("event: delta\n")
    ]
    assert "".join(deltas) == expected
    assert "".join(state["text"]) == expected
    assert chat_api._sanitize_assistant_content("".join(state["text"])) == expected
    assert state["result"] is result
    assert "自动恢复" not in expected


def test_single_normal_sentence_is_not_treated_as_scratchpad() -> None:
    answer = "用户问了一个问题，我会先给出结论，再说明依据。"

    assert not chat_api._looks_like_internal_reasoning(answer)
    assert chat_api._sanitize_assistant_content(answer) == answer


def test_non_stream_parser_never_promotes_reasoning_to_content() -> None:
    payload = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "", "reasoning_content": "private scratchpad"},
            }
        ]
    }

    assert _extract_content(payload) == ("", "stop")
