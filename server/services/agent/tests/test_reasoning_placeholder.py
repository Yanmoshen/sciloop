# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""`reasoning_content` 回传的回归测试（2026-09-25 19:16 真实事故）。

现象：批准一条命令 → 工具**执行失败（退出码 3）** → 模型正要拿到失败信息重新决定时，
下一次请求被供应商 **0.26 秒直接拒收**：

    bad_request: The `reasoning_content` in the thinking mode must be passed back to the API

根因：那一轮的 assistant 消息**带 `tool_calls` 却没有可回传的思考文本**。
白天的补丁只做到"字段**存在**（空串）"，实测**空串不算回传**，照样 400。

用户口径（19:40 确认）：修法 = A 拿不到思考时给**非空占位** + C **发送前先把消息修好**，
让对话不再因为一个协议字段被打断；降级链**保持现状**（工具回合不降级，免得掩盖真因）。
"""

from __future__ import annotations

from typing import Any

from api.v1 import chat as chat_api


def test_real_reasoning_is_preferred() -> None:
    assert chat_api.reasoning_echo("真实思考", "ds:deepseek-flash") == "真实思考"


def test_placeholder_is_non_empty_for_thinking_providers() -> None:
    """**本事故的守卫**：拿不到思考时给的占位必须**非空** —— 空串会被供应商拒。"""

    value = chat_api.reasoning_echo("", "ds:deepseek-flash")
    assert value is not None
    assert value.strip(), "占位不能是空串或纯空白（空串不算回传，实测仍 400）"


def test_no_echo_for_other_providers() -> None:
    """非思考型供应商不塞这个字段（免得被当成未知字段）。"""

    assert chat_api.reasoning_echo("", "openai:gpt-5") is None
    assert chat_api.reasoning_echo("", "") is None


def _assistant_with_calls(**extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "function": {"name": "run_on_computer", "arguments": "{}"}}],
    }
    message.update(extra)
    return message


def test_repair_fills_missing_reasoning_before_sending() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "跑一下"},
        _assistant_with_calls(),  # ← 缺 reasoning_content，正是会 400 的形状
        {"role": "tool", "tool_call_id": "c1", "content": "退出码 3"},
    ]
    fixed = chat_api.repair_reasoning_echo(messages, "ds:deepseek-flash")
    assert fixed == 1
    assert str(messages[2].get("reasoning_content") or "").strip(), "补上的必须非空"


def test_repair_leaves_healthy_messages_alone() -> None:
    messages: list[dict[str, Any]] = [
        _assistant_with_calls(reasoning_content="已有的思考"),
        {"role": "assistant", "content": "普通回答"},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]
    assert chat_api.repair_reasoning_echo(messages, "ds:deepseek-flash") == 0


def test_repair_is_a_noop_for_other_providers() -> None:
    messages: list[dict[str, Any]] = [_assistant_with_calls()]
    assert chat_api.repair_reasoning_echo(messages, "openai:gpt-5") == 0
    assert "reasoning_content" not in messages[0]
