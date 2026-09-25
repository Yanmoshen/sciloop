"""上下文预算与压缩：模块单元 + 循环接线（2026-09-25 用户口径）。

用户口径：单次请求固定 200k 预算，超了就**先压工具结果、再摘要早期轮次**，
**但不因为超窗就停止** —— 压完继续干。

这组测试盯三件事：

1. 压的是**较早**的工具结果，最近的那条保持完整（模型正在基于它推理）；
2. 压缩必须**如实标注**（模型不能以为看到的是全文），且**不二次套娃**；
3. 接线层：超预算时循环里先压缩、推一行过程行、记进 `state["compactions"]`；
   压不动时**不报错也不停**，只记日志。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from api.v1 import chat as chat_api
from services import context_compaction as compaction
from services import context_meter


# --------------------------------------------------------------------------- #
# ① 模块：只压较早的工具结果，且如实标注
# --------------------------------------------------------------------------- #
def test_compress_tool_results_only_touches_tool_messages() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s" * 100},
        {"role": "user", "content": "u" * 100},
        {"role": "tool", "content": "t" * 5000},
    ]
    outcome = compaction.compress_tool_results(messages)
    assert outcome.changed is True
    assert outcome.count == 1
    assert outcome.freed_chars > 0
    assert messages[0]["content"] == "s" * 100, "system 一字不动"
    assert messages[1]["content"] == "u" * 100, "用户话一字不动"


def test_compressed_tool_result_is_honestly_labelled() -> None:
    """压完必须**说得清楚**：原文多少字符、留下多少、要原文就重跑工具。"""

    messages = [{"role": "tool", "content": "x" * 1000}]
    compaction.compress_tool_results(messages, keep_chars=100)
    content = messages[0]["content"]
    assert content.startswith("x" * 100)
    assert "已被压缩以释放上下文" in content
    assert "1,000 字符" in content
    assert "重新执行一次该工具" in content


def test_short_and_already_compressed_results_are_left_alone() -> None:
    """短结果不必压；已经是压缩态的不二次套娃。"""

    short = [{"role": "tool", "content": "y" * 100}]
    assert compaction.compress_tool_results(short, keep_chars=400).changed is False

    once = [{"role": "tool", "content": "z" * 5000}]
    compaction.compress_tool_results(once)
    after_first = once[0]["content"]
    assert compaction.compress_tool_results(once).changed is False
    assert once[0]["content"] == after_first, "第二次不该再动它"


def test_outcome_record_is_what_lands_in_the_conversation_file() -> None:
    messages = [{"role": "tool", "content": "q" * 2000}]
    record = compaction.compress_tool_results(messages).as_record()
    assert record["kind"] == "tool_results"
    assert record["count"] == 1
    assert record["indexes"] == [0]
    assert record["freed_chars"] == record["original_chars"] - record["kept_chars"] > 0


# --------------------------------------------------------------------------- #
# ② 接线：循环里超预算 → 先压 + 告知 + 记录
# --------------------------------------------------------------------------- #
class _FakeResult:
    def __init__(self, *, content: str, prompt_tokens: int = 0) -> None:
        self.content = content
        self.duration_ms = 1
        self.model_id = "fake"
        self.tool_calls: list[dict[str, Any]] = []
        self.raw: dict[str, Any] = {}
        self.usage = _Usage(prompt_tokens)


class _Usage:
    def __init__(self, prompt_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = 0
        self.total_tokens = prompt_tokens


class _FakeUpdate:
    def __init__(self, kind: str, text: str = "", result: Any = None) -> None:
        self.kind = kind
        self.text = text
        self.result = result


def _drive(messages: list[dict[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    """跑一轮循环（假适配器），返回 (SSE 帧, state)。"""

    async def _chat_stream(msgs: list[dict[str, Any]], **_kwargs: Any):
        _drive.seen.append([dict(m) for m in msgs])
        yield _FakeUpdate(kind="delta", text="好")
        yield _FakeUpdate(kind="done", result=_FakeResult(content="好", prompt_tokens=1234))

    _drive.seen = []
    state: dict[str, Any] = {
        "text": [],
        "reasoning": [],
        "result": None,
        "pending": None,
        "compactions": [],
    }
    original = chat_api.adapter.chat_stream
    chat_api.adapter.chat_stream = _chat_stream
    try:

        async def collect() -> list[str]:
            return [
                frame
                async for frame in chat_api._agent_loop(
                    messages,
                    conversation_id="c1",
                    ref="ds:fake",
                    tool_defs=[],
                    rows=[],
                    approvals_out=[],
                    state=state,
                )
            ]

        return asyncio.run(collect()), state
    finally:
        chat_api.adapter.chat_stream = original


def test_loop_compresses_before_calling_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    meter = context_meter.get_meter()
    original_limit = meter.limit_tokens
    meter.limit_tokens = 300
    try:
        messages = [
            {"role": "system", "content": "s" * 200},
            {"role": "user", "content": "问题"},
            {"role": "tool", "content": "大结果" * 300},  # 较早的结果 → 该被压
            {"role": "tool", "content": "最近的结果"},  # 最近的一条 → 保持完整
        ]
        frames, state = _drive(messages)
    finally:
        meter.limit_tokens = original_limit

    assert any("已压缩" in frame for frame in frames), "该推一行过程行告知研究者"
    assert len(state["compactions"]) == 1
    assert state["compactions"][0]["count"] == 1

    sent = _drive.seen[0]
    assert "已被压缩以释放上下文" in sent[2]["content"], "较早的结果被压"
    assert sent[3]["content"] == "最近的结果", "最近的结果必须完整"


def test_loop_records_the_real_usage_for_calibration() -> None:
    """真实 prompt_tokens 要喂给计量器做校正（比例不再是 1.0）。"""

    meter = context_meter.get_meter()
    meter.reset()
    messages = [{"role": "user", "content": "a" * 400}]
    _drive(messages)
    assert meter.ratio != 1.0, "拿到真实用量后比例应当被校正"


def test_loop_keeps_going_when_nothing_to_compress() -> None:
    """超预算但没有可压的工具结果：**不报错、不停**，照常把回答跑完。"""

    meter = context_meter.get_meter()
    original_limit = meter.limit_tokens
    meter.limit_tokens = 100
    try:
        messages = [{"role": "user", "content": "短问题"}]
        frames, state = _drive(messages)
    finally:
        meter.limit_tokens = original_limit

    assert state["compactions"] == []
    assert not any("已压缩" in frame for frame in frames)
    assert any('"delta"' in frame or "delta" in frame for frame in frames), "回答照常产出"


# --------------------------------------------------------------------------- #
# ③ 第二步：摘要早期轮次（裁点必须保住 tool_calls 的配对）
# --------------------------------------------------------------------------- #
def _messages_with_pairs(count: int) -> list[dict[str, Any]]:
    """造一段历史：每轮 = user + assistant(带 tool_calls) + tool 结果。"""

    messages: list[dict[str, Any]] = [{"role": "system", "content": "系统" * 20}]
    for index in range(count):
        messages.append({"role": "user", "content": f"问题{index}" * 20})
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": f"c{index}", "function": {"name": "query_library", "arguments": "{}"}}
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": f"结果{index}" * 20})
    return messages


def test_summary_span_never_orphans_a_tool_message() -> None:
    """首个**保留**的消息绝不能是 `tool` —— 否则它的 assistant 前驱被抽走，供应商直接 400。"""

    messages = _messages_with_pairs(6)
    span = compaction.select_summary_span(messages, keep_tail=4)
    assert span is not None
    start, end = span
    assert str(messages[start].get("role")) == "user", "开头的 system 永不压"
    assert str(messages[end].get("role")) != "tool", "裁点必须前推到不是 tool 的位置"


def test_summary_span_returns_none_when_nothing_worth_compressing() -> None:
    assert compaction.select_summary_span([{"role": "system", "content": "s"}]) is None
    assert compaction.select_summary_span(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "只此一条"}],
        keep_tail=0,
    ) is None


def test_build_summary_prompt_labels_every_role() -> None:
    messages = _messages_with_pairs(2)
    body = compaction.build_summary_prompt(messages, (1, 4))
    assert "[研究者]" in body and "[助手]" in body and "[工具返回]" in body


def test_summarize_early_turns_records_span_and_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        content = "  摘要正文  "

    async def fake_chat(*_args: Any, **_kwargs: Any) -> Any:
        return _Result()

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    messages = _messages_with_pairs(4)
    span = compaction.select_summary_span(messages, keep_tail=3)
    assert span is not None
    record = asyncio.run(
        compaction.summarize_early_turns(messages, span=span, model_ref="ds:x")
    )
    assert record is not None
    assert record["kind"] == "turns"
    assert record["summary"] == "摘要正文"
    assert record["indexes"] == list(range(span[0], span[1]))
    assert record["original_chars"] > 0


def test_summarize_early_turns_returns_none_without_model_or_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = _messages_with_pairs(4)
    span = compaction.select_summary_span(messages, keep_tail=3)
    assert asyncio.run(compaction.summarize_early_turns(messages, span=span, model_ref=None)) is None

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise TimeoutError("摘要超时")

    monkeypatch.setattr("llm.adapter.chat", boom)
    assert asyncio.run(compaction.summarize_early_turns(messages, span=span, model_ref="ds:x")) is None


def test_apply_turn_summary_replaces_span_with_one_honest_message() -> None:
    messages = _messages_with_pairs(4)
    span = compaction.select_summary_span(messages, keep_tail=3)
    assert span is not None
    before = len(messages)
    freed = compaction.apply_turn_summary(messages, span=span, summary="这里是摘要")
    assert len(messages) == before - (span[1] - span[0]) + 1
    note = messages[span[0]]
    assert note["role"] == "user"
    assert "已压缩为下面这段摘要" in note["content"]
    assert "这里是摘要" in note["content"]
    assert freed > 0


def test_loop_summarizes_early_turns_when_tool_results_are_not_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线：工具结果压不动 → 摘要早期轮次，推告知行并记进 `compactions`。"""

    class _Result:
        content = "早期对话的摘要"

    async def fake_chat(*_args: Any, **_kwargs: Any) -> Any:
        return _Result()

    monkeypatch.setattr("llm.adapter.chat", fake_chat)
    meter = context_meter.get_meter()
    original_limit = meter.limit_tokens
    meter.limit_tokens = 200  # 系统提示 + 早期轮次必然超
    try:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "系统提示" * 30},
            {"role": "user", "content": "第一个问题" * 30},
            {"role": "assistant", "content": "第一个回答" * 30},
            {"role": "user", "content": "第二个问题" * 30},
            {"role": "assistant", "content": "第二个回答" * 30},
            {"role": "user", "content": "当前问题"},
        ]
        frames, state = _drive(messages)
    finally:
        meter.limit_tokens = original_limit

    assert any("摘要化" in frame for frame in frames), "该推一行告知"
    kinds = [item["kind"] for item in state["compactions"]]
    assert kinds == ["turns"]
    sent = _drive.seen[0]
    assert "已压缩为下面这段摘要" in sent[1]["content"] or any(
        "已压缩为下面这段摘要" in str(item.get("content") or "") for item in sent
    ), "真正发给模型的 messages 里应当已经是摘要"
