# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""单价与成本计算（Cherry Studio 口径：每百万 token + 币种）。

定价结构（``model_configs.models[]`` 的一项）::

    {"model_id": "x",
     "pricing": {"input":  {"currency": "USD", "perMillionTokens": 0.27},
                 "output": {"currency": "USD", "perMillionTokens": 1.1}}}

三条红线：

1. **单价缺失时 ``cost_usd`` 必须为 null**（contracts.forbidden_actions / WP02-T4），
   禁止用「类目均价」「上次单价」等任何方式估算。
2. **不做汇率换算**。护栏基准是 USD；``currency`` 不是 USD 时一律返回
   ``(None, "non_usd_currency:<币种>")``，该次调用 ``cost_usd`` 保持 null
   —— 缺价只许未知，不许估，换汇同理。
3. 旧口径（``input_price``/``output_price``/``price_unit``）仍需可读：按
   ``per_million = price * 1_000_000 / price_unit`` 换算，属**算术换算**而非估算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

#: 旧口径 ``models[].xxx_price`` 的默认计价单位（token 数）；仅供旧结构兼容解析
DEFAULT_PRICE_UNIT = 1000

#: 单价缺失时的告警文案（会进 ``llm_call_logs.error`` 与日志）
PRICE_MISSING_WARNING = "unit_price_missing:cost_usd=null"

#: 成本护栏的计价基准币种；非此币种的定价不参与护栏比较
GUARDRAIL_CURRENCY = "USD"

#: 每百万 token —— 新口径的唯一单位
PER_MILLION = Decimal(1_000_000)

#: 非 USD 定价的原因串前缀（写进 ``llm_call_logs.error``，供 /costs/summary 分桶）
NON_USD_REASON_PREFIX = "non_usd_currency:"

_COST_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class PriceInfo:
    """某个模型的单价信息（统一换算为「每百万 token」）。"""

    input_per_million: float | None = None
    output_per_million: float | None = None
    currency: str = GUARDRAIL_CURRENCY

    @property
    def has_any(self) -> bool:
        return self.input_per_million is not None or self.output_per_million is not None

    @property
    def is_usd(self) -> bool:
        return self.normalized_currency == GUARDRAIL_CURRENCY

    @property
    def normalized_currency(self) -> str:
        return str(self.currency or GUARDRAIL_CURRENCY).strip().upper() or GUARDRAIL_CURRENCY


def parse_price_info(model_entry: dict[str, Any] | None) -> PriceInfo:
    """从 ``model_configs.models[]`` 的一项中提取单价。

    优先读新口径 ``pricing.{input,output}.{currency,perMillionTokens}``；
    ``pricing`` 缺失/为空时回落到旧键（``input_price``/``output_price``/``price_unit``），
    按 ``per_million = price * 1_000_000 / price_unit`` 精确换算。

    ``pricing`` 两端的币种不一致时**不猜测**：取 ``input`` 的币种（缺失则取 ``output``），
    并把该条视为非 USD 处理（只要任一端不是 USD，整条不参与 USD 护栏）。
    """
    entry = model_entry or {}
    pricing = entry.get("pricing")
    if isinstance(pricing, dict) and pricing:
        return _parse_cherry_pricing(pricing)

    unit = _as_int(entry.get("price_unit")) or DEFAULT_PRICE_UNIT
    return PriceInfo(
        input_per_million=_legacy_to_per_million(_as_float(entry.get("input_price")), unit),
        output_per_million=_legacy_to_per_million(_as_float(entry.get("output_price")), unit),
        currency=_currency_of(entry),
    )


def prices_from_info(
    price: PriceInfo,
) -> tuple[float | None, float | None, str]:
    """便于调用方解包：``(input_per_million, output_per_million, currency)``。"""
    return price.input_per_million, price.output_per_million, price.normalized_currency


def compute_cost_usd(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_price_per_million: float | None,
    output_price_per_million: float | None,
    currency: str = GUARDRAIL_CURRENCY,
) -> tuple[float | None, str | None]:
    """计算本次调用成本（USD）。

    返回 ``(cost_usd, unknown_reason)``：

    - 币种非 USD → ``(None, "non_usd_currency:<币种>")``，**不做汇率换算**
    - 关键单价缺失 → ``(None, "unit_price_missing:...")``，由调用方写 warning
    - 单价齐全 → 正常计算（每百万 token 计价）
    """
    normalized = str(currency or GUARDRAIL_CURRENCY).strip().upper() or GUARDRAIL_CURRENCY
    if normalized != GUARDRAIL_CURRENCY:
        return None, f"{NON_USD_REASON_PREFIX}{normalized}"

    prompt = prompt_tokens or 0
    completion = completion_tokens or 0
    unit_label = "perMillionTokens"

    if prompt > 0 and input_price_per_million is None:
        return None, f"unit_price_missing:input_price,unit={unit_label}"
    if completion > 0 and output_price_per_million is None:
        return None, f"unit_price_missing:output_price,unit={unit_label}"
    if input_price_per_million is None and output_price_per_million is None:
        # 没有 token 也没有单价：无成本可言，但仍如实标记 unknown 原因以便排查
        return None, f"unit_price_missing:all,unit={unit_label}"

    total = Decimal("0")
    total += (Decimal(prompt) / PER_MILLION) * Decimal(str(input_price_per_million or 0))
    total += (Decimal(completion) / PER_MILLION) * Decimal(str(output_price_per_million or 0))
    return float(total.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)), None


def is_non_usd_reason(reason: str | None) -> bool:
    """该 reason 是否表示「定价不是 USD，未计入护栏」。"""
    return bool(reason) and str(reason).startswith(NON_USD_REASON_PREFIX)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _parse_cherry_pricing(pricing: dict[str, Any]) -> PriceInfo:
    input_side = pricing.get("input") if isinstance(pricing.get("input"), dict) else {}
    output_side = pricing.get("output") if isinstance(pricing.get("output"), dict) else {}
    currencies = [
        _currency_of(side) for side in (input_side, output_side) if _currency_of(side)
    ]
    currency = currencies[0] if currencies else GUARDRAIL_CURRENCY
    if len(set(currencies)) > 1:
        # 两端币种不一致：不换算、不猜测，整条按非 USD 处理
        currency = next((c for c in currencies if c != GUARDRAIL_CURRENCY), currency)
    return PriceInfo(
        input_per_million=_as_float(input_side.get("perMillionTokens")),
        output_per_million=_as_float(output_side.get("perMillionTokens")),
        currency=currency,
    )


def _currency_of(side: dict[str, Any]) -> str:
    raw = (side or {}).get("currency")
    text = str(raw).strip().upper() if raw is not None else ""
    return text or GUARDRAIL_CURRENCY


def _legacy_to_per_million(price: float | None, unit: int) -> float | None:
    """旧口径（每 ``unit`` 个 token 的价格）精确换算为每百万 token 价格。"""
    if price is None:
        return None
    if not unit:
        return None
    return float(Decimal(str(price)) * PER_MILLION / Decimal(unit))


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_PRICE_UNIT",
    "GUARDRAIL_CURRENCY",
    "NON_USD_REASON_PREFIX",
    "PER_MILLION",
    "PRICE_MISSING_WARNING",
    "PriceInfo",
    "compute_cost_usd",
    "is_non_usd_reason",
    "parse_price_info",
    "prices_from_info",
]
