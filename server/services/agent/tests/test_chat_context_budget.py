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
