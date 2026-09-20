# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""LLM 层的公共数据类型。

``model_ref`` 是全局唯一可比的模型标识，格式固定为 ``"<provider>:<model_id>"``：

- 供应商来自 ``model_configs.name``（规范化为小写 slug），例如 ``deepseek:deepseek-chat``
- 仅在设置页未配置路由、走环境变量兜底时，provider 固定为 ``"env"``

盲评隔离（``resolve_pair``）比较的就是这个字符串，所以**任何新增决议路径都必须产出该格式**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]

ENV_PROVIDER = "env"


def slugify_provider(name: str) -> str:
    """把供应商名归一为可比较的 slug（小写、空格转连字符）。"""
    return "-".join(str(name).strip().lower().split())


def make_model_ref(provider: str, model_id: str) -> str:
    return f"{slugify_provider(provider)}:{str(model_id).strip()}"


@dataclass(frozen=True)
class ModelRef:
    """可比较的模型引用。"""

    provider: str
    model_id: str

    @classmethod
    def parse(cls, value: str | ModelRef) -> ModelRef:
        if isinstance(value, ModelRef):
            return value
        text = str(value).strip()
        if ":" not in text:
            raise ValueError(f"model_ref 必须形如 'provider:model_id'，收到 {value!r}")
        provider, model_id = text.split(":", 1)
        return cls(provider=slugify_provider(provider), model_id=model_id.strip())

    @classmethod
    def env(cls, model_id: str) -> ModelRef:
        return cls(provider=ENV_PROVIDER, model_id=model_id)

    def __str__(self) -> str:  # pragma: no cover - 直通
        return f"{self.provider}:{self.model_id}"

    @property
    def is_env_fallback(self) -> bool:
        return self.provider == ENV_PROVIDER


@dataclass
class ResolvedModel:
    """路由解析结果：适配层调用所需的一切（含仅在内存中存在的 ``api_key``）。"""

    model_ref: str
    base_url: str
    provider: str
    model_id: str
    #: 仅内存传递，禁止落库 / 禁止出接口
    api_key: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    #: 路由来源：project | global | env_default | env_fallback | explicit
    source: str = "env_default"
    model_config_id: int | None = None
    #: 供应商配置名（用于展示；与 model_ref 的 provider 段一致）
    provider_name: str | None = None
    #: 单价（**每百万 token**，Cherry Studio 口径）；缺失为 None -> cost_usd 记 null
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    #: 定价币种。护栏基准是 USD；非 USD 时不做汇率换算，cost_usd 记 null
    price_currency: str = "USD"
    #: 供应商能力画像（见 providers.py）
    provider_capability: str = "unknown"
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> ModelRef:
        return ModelRef.parse(self.model_ref)

    @property
    def has_price(self) -> bool:
        return self.input_price_per_million is not None or self.output_price_per_million is not None

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> dict[str, Any]:
        """脱敏描述（可安全写日志 / 出接口）。"""
        return {
            "model_ref": self.model_ref,
            "provider": self.provider,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "source": self.source,
            "model_config_id": self.model_config_id,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "input_price_per_million": self.input_price_per_million,
            "output_price_per_million": self.output_price_per_million,
            "price_currency": self.price_currency,
            "api_key_configured": self.api_key_configured,
        }


@dataclass
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_openai(cls, payload: dict[str, Any] | None) -> Usage:
        payload = payload or {}
        prompt = payload.get("prompt_tokens")
        completion = payload.get("completion_tokens")
        total = payload.get("total_tokens")
        if total is None and (prompt is not None or completion is not None):
            total = (prompt or 0) + (completion or 0)
        return cls(
            prompt_tokens=_as_int(prompt),
            completion_tokens=_as_int(completion),
            total_tokens=_as_int(total),
        )

    def is_empty(self) -> bool:
        return self.prompt_tokens is None and self.completion_tokens is None


@dataclass
class LLMResult:
    """统一调用结果。"""

    content: str
    model_ref: str
    provider: str
    model_id: str
    usage: Usage = field(default_factory=Usage)
    #: None 表示单价缺失（禁止估算）；0.0 表示真实零成本（如回放）
    cost_usd: float | None = None
    cost_unknown_reason: str | None = None
    duration_ms: int = 0
    #: 逻辑调用次数（含 JSON 校验重试）
    attempts: int = 1
    #: HTTP 请求次数（含传输层重试）
    http_calls: int = 1
    is_replay: bool = False
    finish_reason: str | None = None
    #: json_schema 校验通过后的解析结果
    parsed: Any = None
    stage: str | None = None
    purpose: str | None = None
    prompt_hash: str | None = None
    #: 路由来源（project / global / env_default / env_fallback / explicit / replay_fixture）
    resolved_from: str | None = None
    #: 降级链上被跳过的模型（可审计）
    fallback_from: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "model_ref": self.model_ref,
            "provider": self.provider,
            "model_id": self.model_id,
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.completion_tokens,
            "cost_usd": self.cost_usd,
            "cost_unknown_reason": self.cost_unknown_reason,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "http_calls": self.http_calls,
            "is_replay": self.is_replay,
            "finish_reason": self.finish_reason,
            "stage": self.stage,
            "purpose": self.purpose,
            "prompt_hash": self.prompt_hash,
            "resolved_from": self.resolved_from,
            "fallback_from": self.fallback_from,
        }


@dataclass
class CallLogEntry:
    """``llm_call_logs`` 的一行（字段与附录 A.6 一一对应）。"""

    provider: str
    model: str
    success: bool
    project_id: int | None = None
    stage: str | None = None
    purpose: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    is_replay: bool = False
    error: str | None = None
    model_ref: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "stage": self.stage,
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "is_replay": self.is_replay,
            "error": self.error,
        }


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
