# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""单价与成本计算。

红线（contracts.forbidden_actions / WP02-T4）：**单价缺失时 ``cost_usd`` 必须为 null**，
禁止用「类目均价」「上次单价」等任何方式估算。成本护栏宁可显示 unknown，也不能显示错数。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

#: ``model_configs.models[].xxx_price`` 的默认计价单位（token 数）
DEFAULT_PRICE_UNIT = 1000

#: 单价缺失时的告警文案（会进 ``llm_call_logs.error`` 与日志）
PRICE_MISSING_WARNING = "unit_price_missing:cost_usd=null"

_COST_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class PriceInfo:
    """某个模型的单价信息。"""

    input_price: float | None
    output_price: float | None
    unit: int = DEFAULT_PRICE_UNIT

    @property
    def has_any(self) -> bool:
        return self.input_price is not None or self.output_price is not None


def parse_price_info(model_entry: dict[str, Any] | None) -> PriceInfo:
    """从 ``model_configs.models[]`` 的一项中提取单价。"""
    entry = model_entry or {}
    return PriceInfo(
        input_price=_as_float(entry.get("input_price")),
        output_price=_as_float(entry.get("output_price")),
        unit=_as_int(entry.get("price_unit")) or DEFAULT_PRICE_UNIT,
    )


def compute_cost_usd(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_price: float | None,
    output_price: float | None,
    price_unit: int = DEFAULT_PRICE_UNIT,
) -> tuple[float | None, str | None]:
    """计算本次调用成本。

    返回 ``(cost_usd, unknown_reason)``：

    - 单价齐全（或缺失的一侧对应 token 为 0）→ 正常计算
    - 关键单价缺失 → ``(None, "unit_price_missing:...")``，由调用方写 warning
    """
    unit = price_unit or DEFAULT_PRICE_UNIT
    prompt = prompt_tokens or 0
    completion = completion_tokens or 0

    if prompt > 0 and input_price is None:
        return None, f"unit_price_missing:input_price,unit={unit}"
    if completion > 0 and output_price is None:
        return None, f"unit_price_missing:output_price,unit={unit}"
    if input_price is None and output_price is None:
        # 没有 token 也没有单价：无成本可言，但仍如实标记 unknown 原因以便排查
        return None, f"unit_price_missing:all,unit={unit}"

    total = Decimal("0")
    total += (Decimal(prompt) / Decimal(unit)) * Decimal(str(input_price or 0))
    total += (Decimal(completion) / Decimal(unit)) * Decimal(str(output_price or 0))
    return float(total.quantize(_COST_QUANT, rounding=ROUND_HALF_UP)), None


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
    "PRICE_MISSING_WARNING",
    "PriceInfo",
    "compute_cost_usd",
    "parse_price_info",
]
