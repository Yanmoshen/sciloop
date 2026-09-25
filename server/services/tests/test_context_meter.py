# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""上下文计量：估算口径、校正比例、超预算判定。

口径必须**逐字对齐** Cherry Studio 的 `token-meter.mjs`（字符÷4 + 每块 4），
所以这里的断言写的是**算出来的数**，不是"大于零"这类含糊判断 ——
口径一旦漂移，"200k 该不该压"就会跟着漂。
"""

from __future__ import annotations

import pytest

from services import context_meter as meter


def test_text_estimation_matches_the_cherry_formula() -> None:
    """`ceil(len / 4) + 4`。"""

    assert meter.estimate_text("") == 4
    assert meter.estimate_text("a") == 5  # ceil(1/4)=1 + 4
    assert meter.estimate_text("a" * 4) == 5  # ceil(4/4)=1 + 4
    assert meter.estimate_text("a" * 5) == 6  # ceil(5/4)=2 + 4
    assert meter.estimate_text("a" * 100) == 29  # 25 + 4
    # 中文按字符数算（不按字节），口径与 Cherry 一致
    assert meter.estimate_text("中" * 8) == 6  # ceil(8/4)=2 + 4


def test_message_estimation_adds_the_per_message_overhead() -> None:
    """单条消息 = 内容 + 4。"""

    assert meter.estimate_message({"role": "user", "content": "a" * 100}) == 33  # 29 + 4
    # tool 结果走同一条路（内容 + 4），不会漏算
    assert meter.estimate_message({"role": "tool", "content": "a" * 40}) == 18  # 14 + 4


def test_tool_calls_and_reasoning_are_counted() -> None:
    """`tool_calls` 与 `reasoning_content` 都要算进去 —— 少算的后果是"该压没压"。"""

    message = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": "run_command", "arguments": '{"argv": ["ls", "-la"]}'},
            }
        ],
        "reasoning_content": "a" * 40,
    }
    # 名字 ceil(11/4)=3 + 参数 ceil(23/4)=6 + 4 ；思考 14 ；消息开销 4
    assert meter.estimate_message(message) == 3 + 6 + 4 + 14 + 4


def test_tools_declaration_is_counted_once() -> None:
    tools = [{"type": "function", "function": {"name": "query_library", "parameters": {}}}]
    assert meter.estimate_tools(tools) == meter.estimate_text(
        __import__("json").dumps(tools, ensure_ascii=False)
    )
    assert meter.estimate_tools(None) == 0
    assert meter.estimate_tools([]) == 0


def test_project_applies_the_correction_ratio() -> None:
    m = meter.TokenMeter(limit_tokens=1000)
    messages = [{"role": "user", "content": "a" * 400}]
    raw = m.estimate(messages)
    assert m.project(messages) == raw, "还没校正过时比例是 1.0"
    assert m.over_budget(messages) is False

    # 真实用量是估算的 1.5 倍 → 比例从 1.0 走到 1.25（滑动平均）
    m.calibrate(prompt_tokens=raw * 3 // 2, estimated=raw)
    assert m.ratio == pytest.approx(1.25)
    assert m.project(messages) > raw


def test_calibration_is_clamped() -> None:
    """单次异常用量不该把比例带飞（限幅 0.5~2.0）。"""

    m = meter.TokenMeter(limit_tokens=1000)
    m.calibrate(prompt_tokens=10_000, estimated=1)  # 单次异常大
    assert meter.RATIO_MIN <= m.ratio <= meter.RATIO_MAX
    m.calibrate(prompt_tokens=1_000_000, estimated=1_000_000)
    assert meter.RATIO_MIN <= m.ratio <= meter.RATIO_MAX


def test_calibration_ignores_garbage_samples() -> None:
    """真实值缺失（0）或估算为 0 时都不动比例。"""

    m = meter.TokenMeter(limit_tokens=1000)
    m.calibrate(prompt_tokens=0, estimated=100)
    assert m.ratio == pytest.approx(1.0)
    m.calibrate(prompt_tokens=100, estimated=0)
    assert m.ratio == pytest.approx(1.0)


def test_over_budget_trips_at_the_limit() -> None:
    m = meter.TokenMeter(limit_tokens=100)
    small = [{"role": "user", "content": "a" * 40}]
    big = [{"role": "user", "content": "a" * 1000}]
    assert m.over_budget(small) is False
    assert m.over_budget(big) is True
    assert m.breakdown(big)["limit"] == 100
    assert m.breakdown(big)["projected"] > 100


def test_breakdown_splits_system_tools_and_messages() -> None:
    m = meter.TokenMeter(limit_tokens=200_000)
    parts = m.breakdown(
        [{"role": "user", "content": "a" * 40}],
        system="s" * 40,
        tools=[{"type": "function", "function": {"name": "x"}}],
    )
    assert parts["system"] == 14
    assert parts["tools"] > 0
    assert parts["messages"] == 18  # 内容 14 + 单条消息开销 4
    assert parts["estimated"] == parts["system"] + parts["tools"] + parts["messages"]
    assert parts["ratio_percent"] == 100


def test_default_limit_is_two_hundred_thousand(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户口径：固定 200k 配置项（环境变量优先，便于本地实验）。"""

    monkeypatch.delenv("SCILOOP_LLM_CONTEXT_LIMIT_TOKENS", raising=False)
    assert meter.context_limit_tokens() == 200_000
    monkeypatch.setenv("SCILOOP_LLM_CONTEXT_LIMIT_TOKENS", "8000")
    assert meter.context_limit_tokens() == 8000
    monkeypatch.setenv("SCILOOP_LLM_CONTEXT_LIMIT_TOKENS", "不是数字")
    assert meter.context_limit_tokens() == 200_000, "环境变量不可信时回落配置/缺省"
