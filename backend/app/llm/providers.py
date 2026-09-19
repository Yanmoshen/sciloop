# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""供应商画像与 JSON 结构化输出策略。

不同供应商对 ``response_format`` 的支持并不一致（WP02.risks 第 1 条），因此这里给出三档策略：

1. ``json_schema`` —— 原生 JSON Schema 约束（OpenAI 结构化输出 / 兼容供应商）
2. ``json_object`` —— 只保证「是合法 JSON」，结构由提示词约束
3. ``prompt_only``  —— 纯提示词约束（本地模型、老网关、部分中转站）

适配层按档位从上往下尝试，**遇到 400 且响应体指向 ``response_format`` 时自动降档并记住**，
该降档只影响结构化输出的实现手段，不影响 schema 校验本身（校验始终由本地完成）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

JsonStrategy = Literal["json_schema", "json_object", "prompt_only"]

_SUFFIXES = ("/chat/completions", "/completions")


@dataclass(frozen=True)
class ProviderCapability:
    """供应商能力画像。"""

    key: str
    label: str
    supports_json_schema: bool = False
    supports_json_object: bool = True
    default_base_url: str | None = None
    #: 是否需要显式指定 max_tokens 上限（部分供应商必填）
    requires_max_tokens: bool = False


PRESETS: dict[str, ProviderCapability] = {
    "openai": ProviderCapability(
        "openai", "OpenAI", supports_json_schema=True, default_base_url="https://api.openai.com/v1"
    ),
    "deepseek": ProviderCapability(
        "deepseek", "DeepSeek", supports_json_schema=True, default_base_url="https://api.deepseek.com/v1"
    ),
    "siliconflow": ProviderCapability(
        "siliconflow",
        "SiliconFlow 硅基流动",
        supports_json_schema=True,
        default_base_url="https://api.siliconflow.cn/v1",
    ),
    "moonshot": ProviderCapability(
        "moonshot", "Moonshot Kimi", supports_json_schema=True, default_base_url="https://api.moonshot.cn/v1"
    ),
    "dashscope": ProviderCapability(
        "dashscope",
        "阿里云百炼（兼容模式）",
        supports_json_schema=False,
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    "zhipu": ProviderCapability(
        "zhipu", "智谱 GLM", supports_json_schema=False, default_base_url="https://open.bigmodel.cn/api/paas/v4"
    ),
    "openrouter": ProviderCapability(
        "openrouter",
        "OpenRouter",
        supports_json_schema=True,
        default_base_url="https://openrouter.ai/api/v1",
    ),
    "ollama": ProviderCapability(
        "ollama", "本地 Ollama", supports_json_schema=True, default_base_url="http://localhost:11434/v1"
    ),
    "oneapi": ProviderCapability(
        "oneapi", "One-API / New-API 网关", supports_json_schema=False
    ),
    "custom": ProviderCapability("custom", "自定义 OpenAI 兼容端点", supports_json_schema=False),
}

_DEFAULT = PRESETS["custom"]

#: 运行期降档记忆：provider slug -> 已被上游拒绝的策略集合
_REJECTED_STRATEGIES: dict[str, set[str]] = {}


def detect_capability(*, provider: str | None = None, base_url: str | None = None) -> ProviderCapability:
    """按显式 provider 名或 base_url 关键字推断能力画像。"""
    if provider:
        key = str(provider).strip().lower()
        if key in PRESETS:
            return PRESETS[key]
    host = (base_url or "").lower()
    if host:
        if "deepseek" in host:
            return PRESETS["deepseek"]
        if "openai.com" in host:
            return PRESETS["openai"]
        if "siliconflow" in host:
            return PRESETS["siliconflow"]
        if "moonshot" in host:
            return PRESETS["moonshot"]
        if "dashscope" in host or "aliyuncs" in host:
            return PRESETS["dashscope"]
        if "bigmodel" in host or "zhipu" in host:
            return PRESETS["zhipu"]
        if "openrouter" in host:
            return PRESETS["openrouter"]
        if "11434" in host:
            return PRESETS["ollama"]
    return _DEFAULT


def chat_completions_url(base_url: str) -> str:
    """把用户填写的 base_url 归一为 ``.../v1/chat/completions``。

    规则（幂等）：已是 ``.../chat/completions`` 直接返回；以 ``/v1`` 结尾追加
    ``/chat/completions``；其余追加 ``/v1/chat/completions``。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise ValueError("base_url 不能为空")
    if url.endswith(_SUFFIXES):
        return url
    if url.endswith("/v1") or url.endswith("/v4") or "/compatible-mode/v1" in url:
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def strategy_chain(capability: ProviderCapability) -> list[JsonStrategy]:
    """该供应商的降档链（从最强到最弱）。"""
    chain: list[JsonStrategy] = []
    if capability.supports_json_schema:
        chain.append("json_schema")
    if capability.supports_json_object:
        chain.append("json_object")
    chain.append("prompt_only")
    return chain


def json_strategy(
    capability: ProviderCapability,
    *,
    provider_key: str | None = None,
) -> JsonStrategy:
    """返回当前应使用的策略（跳过运行期已被拒绝的档位）。"""
    chain = strategy_chain(capability)
    rejected = _REJECTED_STRATEGIES.get((provider_key or capability.key).lower(), set())
    for candidate in chain:
        if candidate not in rejected:
            return candidate
    return "prompt_only"


def note_strategy_rejected(provider_key: str, strategy: str) -> JsonStrategy:
    """记录上游拒绝了某档策略，并返回下一档可用策略。"""
    key = (provider_key or "custom").lower()
    _REJECTED_STRATEGIES.setdefault(key, set()).add(strategy)
    capability = PRESETS.get(key, _DEFAULT)
    return json_strategy(capability, provider_key=key)


def reset_strategy_memory() -> None:
    """清空降档记忆（测试与设置页显式重试时使用）。"""
    _REJECTED_STRATEGIES.clear()


def rejected_strategies(provider_key: str) -> set[str]:
    return set(_REJECTED_STRATEGIES.get((provider_key or "custom").lower(), set()))


def schema_hint(json_schema: dict[str, Any]) -> str:
    """给 ``prompt_only`` / ``json_object`` 档位使用的提示词约束。

    只描述「必须输出符合下列 Schema 的 JSON」，不注入任何业务语义。
    """
    pretty = json.dumps(json_schema, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        "Output requirements (must obey):\n"
        "1. Reply with ONE JSON object only. No markdown fences, no commentary, no trailing text.\n"
        "2. The JSON must validate against the following JSON Schema:\n"
        f"{pretty}\n"
    )


def with_schema_hint(messages: list[dict[str, Any]], json_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """在消息序列前追加一条 system 提示（不修改原消息）。"""
    hint = {"role": "system", "content": schema_hint(json_schema)}
    return [hint, *messages]


def looks_like_schema_rejection(status_code: int | None, body: str | None) -> bool:
    """判断错误响应是否指向 ``response_format`` 不被支持。"""
    if status_code != 400:
        return False
    text = (body or "").lower()
    if "response_format" in text or "json_schema" in text or "json mode" in text:
        return True
    return any(
        phrase in text
        for phrase in (
            "unsupported parameter",
            "unknown parameter",
            "not supported",
            "invalid parameter",
        )
    )


__all__ = [
    "PRESETS",
    "JsonStrategy",
    "ProviderCapability",
    "chat_completions_url",
    "detect_capability",
    "json_strategy",
    "looks_like_schema_rejection",
    "note_strategy_rejected",
    "rejected_strategies",
    "reset_strategy_memory",
    "schema_hint",
    "strategy_chain",
    "with_schema_hint",
]
