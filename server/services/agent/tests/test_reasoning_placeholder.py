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


def test_repair_fills_plain_history_assistant_too() -> None:
    """**2026-09-26 的真实事故**：只补「带 `tool_calls` 的那些」**不够**。

    受控实验（四种报文形状各跑一次真实调用）证明，DeepSeek thinking 模式的规则是：
    **只要请求里出现带 `tool_calls` 的 assistant 消息，请求里所有 assistant 消息都必须带
    `reasoning_content`** —— 缺任何一条都 400：

    | 报文形状 | 实测 |
    |---|---|
    | 历史 assistant 无该字段 + 工具助理有 | **400** |
    | 历史 assistant 补上真实思考 | 通过 |
    | 干脆没有那条 assistant | 通过 |
    | 历史 assistant 补**空串** | 通过 |

    而历史轮走 `conversations.context_messages()`，只回 `role`/`content`（不带思考）
    → 「已有对话里的多轮工具调用」与「批准后续答」**必 400、整轮被强制中断**。
    这条用例就是那个形状（旧实现在这里会返回 0，正是 bug 本身）。
    """

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "普通回答"},  # ← 历史轮：旧实现漏掉的那一条
        _assistant_with_calls(reasoning_content="工具轮的思考"),
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
    ]
    fixed = chat_api.repair_reasoning_echo(messages, "ds:deepseek-flash")
    assert fixed == 1
    assert "reasoning_content" in messages[1], "历史里的 assistant 也必须带上这个字段"
    assert messages[1]["content"] == "普通回答", "只补字段，不动正文（不塞占位句冒充它的思考）"


def test_repair_is_idempotent() -> None:
    """补两次不该重复计数、也不该改写已补好的值。"""

    messages: list[dict[str, Any]] = [
        {"role": "assistant", "content": "普通回答"},
        _assistant_with_calls(),
    ]
    assert chat_api.repair_reasoning_echo(messages, "ds:deepseek-flash") == 2
    assert messages[0]["reasoning_content"] == ""
    assert chat_api.repair_reasoning_echo(messages, "ds:deepseek-flash") == 0


def test_repair_is_a_noop_for_other_providers() -> None:
    """非思考型供应商不需要这个字段 —— 别给它塞（免得被当成未知字段拒收）。"""

    messages: list[dict[str, Any]] = [
        _assistant_with_calls(),
        {"role": "assistant", "content": "普通回答"},
    ]
    assert chat_api.repair_reasoning_echo(messages, "openai:gpt-5") == 0
    assert "reasoning_content" not in messages[0]
    assert "reasoning_content" not in messages[1]
