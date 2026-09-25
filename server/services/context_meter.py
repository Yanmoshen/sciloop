# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""上下文预算的计量：估算 token、用真实用量校正、判断是否超预算。

为什么需要它
------------
用户口径（2026-09-25）：单次请求的上下文**固定 200k 预算**（`llm_context_limit_tokens`），
超了就压缩（先压工具结果、再摘要早期轮次），**不因为超窗就停止**。
要判断"超没超"，先得能量。

为什么不用 tiktoken
-------------------
口径**逐字对齐 Cherry Studio** 的实现
（`D:\\cherry studio\\resources\\app.asar.unpacked\\node_modules\\@cherrystudio\\dsh-bridge\\
dist\\runtime\\token-meter.mjs`）：

    text / reasoning : ceil(len(text) / 4) + 4
    tool-call        : ceil(len(name) / 4) + ceil(len(arguments) / 4) + 4
    tool-result      : 递归其内容 + 4
    其它块           : 4 + ceil(len(json.dumps(block)) / 4)
    单条消息         : 上述求和 + 4
    system           : ceil(len(system) / 4) + 4
    tools 声明       : ceil(len(json.dumps(tools)) / 4) + 4

即**字符数 ÷ 4 向上取整 + 每块 4 的固定开销**：零依赖、不下载词表、中英混排同样适用。
（不引 tiktoken：多一个依赖与一份词表要维护，而估算只用来做"要不要压缩"的判定。）

估算怎么和真实用量对齐
----------------------
Cherry 的做法是"真实 usage 做基准 + 估算算增量"。我们没有它那套事件溯源，
用**比例校正**达到同样效果：每次拿到供应商返回的真实 `prompt_tokens`，
就把 `真实 / 估算` 记进一个滑动平均比例（限制在 0.5~2.0），
之后用 `估算 × 比例` 作为"投影用量"——比纯估算贴得多，又不需要回放历史。
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

__all__ = [
    "BLOCK_OVERHEAD",
    "CHARS_PER_TOKEN",
    "RATIO_MAX",
    "RATIO_MIN",
    "TokenMeter",
    "context_limit_tokens",
    "estimate_message",
    "estimate_messages",
    "estimate_text",
    "estimate_tools",
    "get_meter",
]

#: 每个内容块的固定开销（Cherry 口径里的 `+4`）
BLOCK_OVERHEAD = 4
#: 每个字符按多少 token 折（Cherry 口径：÷4）
CHARS_PER_TOKEN = 4
#: 校正比例的上下限：单次异常用量不该把比例带飞
RATIO_MIN = 0.5
RATIO_MAX = 2.0

#: 缺省预算（与 `core.config.llm_context_limit_tokens` 一致；配置读不到时用它）
DEFAULT_CONTEXT_LIMIT_TOKENS = 200_000


def _ceil_div4(length: int) -> int:
    """`ceil(len / 4)`（Cherry 的 `Math.ceil(len / 4)`）。"""

    return math.ceil(length / CHARS_PER_TOKEN)


def estimate_text(text: str | None) -> int:
    """一段文本的估算（含单块开销）。"""

    return _ceil_div4(len(text or "")) + BLOCK_OVERHEAD


def _estimate_blocks(blocks: Any) -> int:
    """一组内容块的估算（对齐 Cherry 的 `u(blocks)`）。

    ⚠️ **空/缺省 content 记 0**（不是记一块的开销）：`tool_calls` 那一轮的 assistant 消息
    本来就没有 content，把它算成一块会让每条工具回合凭空多出 4 个 token。
    """

    if blocks is None or blocks == "":
        return 0
    if isinstance(blocks, str):
        return estimate_text(blocks)
    if not isinstance(blocks, list):
        return estimate_text(json.dumps(blocks, ensure_ascii=False) if blocks is not None else "")

    total = 0
    for block in blocks:
        if isinstance(block, str):
            total += estimate_text(block)
            continue
        if not isinstance(block, dict):
            total += BLOCK_OVERHEAD + _ceil_div4(len(json.dumps(block, ensure_ascii=False)))
            continue
        kind = str(block.get("type") or "")
        if kind in ("text", "reasoning"):
            total += estimate_text(str(block.get("text") or ""))
        elif kind in ("tool-call", "tool_call"):
            # 工具调用：名字与参数各算一份，再叠固定开销
            arguments = block.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            total += (
                _ceil_div4(len(str(block.get("name") or "")))
                + _ceil_div4(len(str(arguments or "")))
                + BLOCK_OVERHEAD
            )
        elif kind in ("tool-result", "tool_result"):
            total += _estimate_blocks(block.get("content")) + BLOCK_OVERHEAD
        else:
            total += BLOCK_OVERHEAD + _ceil_div4(len(json.dumps(block, ensure_ascii=False)))
    return total


def estimate_message(message: dict[str, Any]) -> int:
    """单条 OpenAI 兼容消息的估算（对齐 Cherry 的 `d(message)`）。

    三种形态都要算到：
    - `content` 是字符串（普通文本）；
    - `content` 是内容块数组（多模态 / 结构化）；
    - `tool_calls`（assistant 要求调工具）与 `reasoning_content`（思考型供应商回传的思考）。
      少算它们会低估——而低估的后果是"该压缩时没压"，最后撞上下文窗口。
    """

    total = _estimate_blocks(message.get("content"))
    reasoning = message.get("reasoning_content")
    if reasoning:
        total += estimate_text(str(reasoning))

    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            function = (call or {}).get("function") or {}
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            total += (
                _ceil_div4(len(str(function.get("name") or "")))
                + _ceil_div4(len(str(arguments or "")))
                + BLOCK_OVERHEAD
            )
    return total + BLOCK_OVERHEAD


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    """一组消息的估算。"""

    return sum(estimate_message(item) for item in messages or [])


def estimate_tools(tools: list[dict[str, Any]] | None) -> int:
    """工具声明的估算（对齐 Cherry 的 `p(header)`）：整份 JSON 折半算，再叠固定开销。"""

    if not tools:
        return 0
    return _ceil_div4(len(json.dumps(tools, ensure_ascii=False))) + BLOCK_OVERHEAD


def context_limit_tokens() -> int:
    """当前预算（token）。优先配置；配置读不出来时用缺省值，并允许环境变量覆盖。

    环境变量 `SCILOOP_LLM_CONTEXT_LIMIT_TOKENS` 优先于配置（本地实验方便）。
    """

    raw = (os.environ.get("SCILOOP_LLM_CONTEXT_LIMIT_TOKENS") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    try:
        from core.config import get_settings

        value = int(getattr(get_settings(), "llm_context_limit_tokens", 0) or 0)
        if value > 0:
            return value
    except Exception:  # noqa: BLE001 - 配置读不出来不该让计量不可用
        pass
    return DEFAULT_CONTEXT_LIMIT_TOKENS


class TokenMeter:
    """一次请求的用量计量：估算 + 真实用量校正 + 超预算判定。

    比例校正（`calibrate`）是**进程级**的：它反映的是"这家供应商 / 这个模型"的
    字符-token 折算关系，与具体对话无关。
    """

    def __init__(self, *, limit_tokens: int | None = None) -> None:
        self._ratio = 1.0
        self._limit = int(limit_tokens) if limit_tokens else context_limit_tokens()

    @property
    def ratio(self) -> float:
        return self._ratio

    @property
    def limit_tokens(self) -> int:
        return self._limit

    @limit_tokens.setter
    def limit_tokens(self, value: int) -> None:
        """允许改预算（测试用固定小预算；将来也可做"按对话设预算"）。"""

        self._limit = max(1, int(value))

    # ------------------------------------------------------------------ #
    # 估算与投影
    # ------------------------------------------------------------------ #
    def estimate(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """纯估算（不校正）：system + tools + 全部消息。"""

        total = estimate_messages(messages)
        if system:
            total += estimate_text(system)
        return total + estimate_tools(tools)

    def project(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        """**投影用量**：估算值 × 校正比例（判断该不该压缩就用它）。"""

        return math.ceil(self.estimate(messages, system=system, tools=tools) * self._ratio)

    def over_budget(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> bool:
        return self.project(messages, system=system, tools=tools) > self._limit

    def breakdown(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        """给日志/界面用的一份明细（各段各占多少）。"""

        system_tokens = estimate_text(system) if system else 0
        tools_tokens = estimate_tools(tools)
        message_tokens = estimate_messages(messages)
        return {
            "system": system_tokens,
            "tools": tools_tokens,
            "messages": message_tokens,
            "estimated": system_tokens + tools_tokens + message_tokens,
            "projected": self.project(messages, system=system, tools=tools),
            "limit": self._limit,
            "ratio_percent": round(self._ratio * 100),
        }

    # ------------------------------------------------------------------ #
    # 用真实用量校正
    # ------------------------------------------------------------------ #
    def calibrate(self, *, prompt_tokens: int, estimated: int) -> float:
        """拿供应商返回的真实 `prompt_tokens` 校正比例（滑动平均，限幅）。

        `estimated` 必须与真实值**同一口径**（同一个 system/tools/messages 集合），
        否则比例就没有意义 —— 调用方要用同一个快照算。
        """

        if prompt_tokens <= 0 or estimated <= 0:
            return self._ratio
        sample = prompt_tokens / estimated
        sample = max(RATIO_MIN, min(RATIO_MAX, sample))
        self._ratio = max(RATIO_MIN, min(RATIO_MAX, (self._ratio + sample) / 2))
        return self._ratio

    def reset(self) -> None:
        """把比例恢复成 1.0（换供应商/模型时用）。"""

        self._ratio = 1.0


_METER: TokenMeter | None = None


def get_meter() -> TokenMeter:
    """进程级计量器（比例校正需要跨请求累积）。"""

    global _METER
    if _METER is None:
        _METER = TokenMeter()
    return _METER
