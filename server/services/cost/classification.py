# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""调用「真实 / 非真实」判定（WP02 成本口径）。

护栏 ``used_usd`` 只应累计**真实计费调用**。本模块定义判定规则，供
:mod:`services.cost.accumulator` 在聚合阶段剔除以下调用：

1. ``is_replay = true`` —— 由 ``demo_fixtures`` 回放产生，未出网、未计费；
2. ``provider`` / ``model`` / ``purpose`` 命中非真实标记（大小写不敏感）——
   本地桩、验收夹具、合成数据均以命名自证（与 WP17 的
   「无 key 时桩必须自证」约定一致）。

**不修改任何历史行**：``llm_call_logs.cost_usd`` 原样保留以便审计，
被剔除的金额单独汇入 ``stub_saved_usd`` / ``replay_saved_usd``。

标记集合可用环境变量 ``COST_NONBILLABLE_MARKERS`` 覆盖（逗号分隔，
置空字符串则只保留 ``is_replay`` 判定）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: 契约口径（任务书 WP02 成本修复）：任一命中即判为非真实调用
DEFAULT_NONBILLABLE_MARKERS: tuple[str, ...] = (
    "stub",
    "mock",
    "fake",
    "replay",
    # 验收/合成夹具：如 provider=wp10-acceptance / model=overrun
    # （见 .tmp/wp10/wp10_acceptance.py 注入的护栏熔断样本，非真实 API 花费）
    "acceptance",
    "fixture",
    "synthetic",
)

#: 非真实标记的覆盖开关
MARKERS_ENV = "COST_NONBILLABLE_MARKERS"

#: 判定结果分类
CLASS_REAL = "real"
CLASS_STUB = "stub"
CLASS_REPLAY = "replay"


@dataclass(frozen=True)
class CallClassification:
    """单次调用的口径判定结果。"""

    classification: str
    marker: str | None = None
    field: str | None = None

    @property
    def billable(self) -> bool:
        return self.classification == CLASS_REAL

    @property
    def reason(self) -> str:
        if self.classification == CLASS_REPLAY:
            return "is_replay=true"
        if self.classification == CLASS_STUB:
            return f"{self.field}~{self.marker}"
        return "billable"


def nonbillable_markers() -> tuple[str, ...]:
    """当前生效的非真实标记集合（环境变量可覆盖）。"""
    raw = os.environ.get(MARKERS_ENV)
    if raw is None:
        return DEFAULT_NONBILLABLE_MARKERS
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def classify_call(
    *,
    provider: str | None,
    model: str | None,
    purpose: str | None = None,
    is_replay: bool | None = False,
) -> CallClassification:
    """判定一次调用是否为真实计费调用。

    判定顺序（先回放后命名，保证 ``replay_rows`` 与 ``stub_rows`` 不重复计数）：

    1. ``is_replay`` 为真 → :data:`CLASS_REPLAY`
    2. ``provider`` / ``model`` / ``purpose`` 命中标记 → :data:`CLASS_STUB`
    3. 其余 → :data:`CLASS_REAL`
    """
    if is_replay:
        return CallClassification(CLASS_REPLAY, marker="is_replay", field="is_replay")

    markers = nonbillable_markers()
    if not markers:
        return CallClassification(CLASS_REAL)

    for field_name, value in (("provider", provider), ("model", model), ("purpose", purpose)):
        text = (value or "").lower()
        if not text:
            continue
        for marker in markers:
            if marker in text:
                return CallClassification(CLASS_STUB, marker=marker, field=field_name)
    return CallClassification(CLASS_REAL)


def classification_rules() -> list[str]:
    """供 ``/costs/summary`` 的 ``notes`` 与审计脚本展示的口径说明。"""
    markers = nonbillable_markers()
    return [
        "used_usd 只累计真实调用：is_replay=false 且 provider/model/purpose 不含非真实标记",
        "非真实标记（大小写不敏感）：" + ("|".join(markers) if markers else "（已由环境变量置空）"),
        "回放调用（is_replay=true）单独汇入 replay_saved_usd/replay_calls，不计入 used_usd",
        "桩/验收夹具调用单独汇入 stub_saved_usd/stub_calls，不计入 used_usd",
        "原始 llm_call_logs.cost_usd 行一律保留，不做修改或删除（可审计）",
        f"标记集合可用环境变量 {MARKERS_ENV} 覆盖（逗号分隔）",
    ]


__all__ = [
    "CLASS_REAL",
    "CLASS_REPLAY",
    "CLASS_STUB",
    "DEFAULT_NONBILLABLE_MARKERS",
    "MARKERS_ENV",
    "CallClassification",
    "classification_rules",
    "classify_call",
    "nonbillable_markers",
]
