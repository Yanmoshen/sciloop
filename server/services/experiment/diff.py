# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""父子 Passport 差异报告（WP11-T6）。

``contracts.passport_rules.diff_rule``：replay 与 rerun **都必须**返回与父 Passport
的差异报告。本模块只做「比对」这一件事，产出契约规定的五个字段：

``{metric_deltas, cost_delta, duration_delta, config_changes, verdict}``

判定口径（**先看配置，再看指标**）
---------------------------------
=================  ========================================================
``identical``      配置未变 且 所有数值指标与成本在容差内完全一致
``minor``          有差异但未超出容差（或仅配置项变化而指标未变）
``divergent``      任一数值指标超出容差，或配置变化同时伴随指标变化
=================  ========================================================

诚实性约束
----------
- 只比**落库的真实字段**：缺少的指标不出现在 ``metric_deltas`` 里，**不用 0 补齐**
- 缺失指标（父有子无 / 子有父无）单独记入 ``metric_sets``，避免「悄悄消失」
- ``duration_delta`` 只是环境耗时，**不参与 verdict**（写入 ``verdict_basis``），
  否则重放时的毫秒抖动会把「结论一致」误判成「不一致」
- ``generation_params`` 里的易变字段（回放标记 / 日志窗口 / 降级说明等）不参与
  ``config_changes``，否则 replay 必然被判成「配置变了」
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from services.experiment import metrics as M
from services.experiment import passport as P

logger = logging.getLogger("sciloop.experiment.diff")

#: 契约允许的判定值（``contracts.passport_rules.diff_rule``）
VERDICTS: tuple[str, ...] = ("identical", "minor", "divergent")

#: 数值容差（按指标单位；绝对容差）
ABSOLUTE_TOLERANCES: dict[str, float] = {
    "ratio": 0.05,
    "usd": 0.01,
    "ms": 50.0,
    "count": 0.0,
}
#: 未登记单位时的相对容差
DEFAULT_RELATIVE_TOLERANCE = 0.05

#: 配置比对字段（缺一不可的溯源维度）
CONFIG_FIELDS: tuple[str, ...] = (
    "template_id",
    "provider",
    "model_id",
    "prompt_version",
    "prompt_sha256",
    "dataset_name",
    "dataset_version",
    "dataset_sha256",
    "code_commit_sha",
    "dependency_lock_sha256",
    "template_config",
)

#: ``generation_params`` 中**不参与配置比对**的易变字段（回放/耗时/日志窗口类）
VOLATILE_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "is_replay",
        "replay_only",
        "live_calls",
        "replay_calls",
        "llm_log_window",
        "degradations",
        "notes",
        "egress",
    }
)

#: **不参与 verdict** 的指标：``duration_ms`` 是整 Run 墙钟时间，属环境噪声，
#: 不反映实验结论是否一致（仍会出现在 ``metric_deltas`` 中并标 ``verdict_neutral``）
VERDICT_NEUTRAL_METRICS: frozenset[str] = frozenset({"duration_ms"})


# --------------------------------------------------------------------------- #
# 归一
# --------------------------------------------------------------------------- #
def _as_mapping(value: Any) -> dict[str, Any]:
    """接受 ORM 行或 Mapping（``passport.serialize`` 保证字段齐整）。"""
    if isinstance(value, Mapping):
        return dict(value)
    return P.serialize(value)


def metric_pairs(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """可比对的数值指标集合（只含两侧都是数值的指标）。"""
    left = dict(parent.get("metrics") or {})
    right = dict(child.get("metrics") or {})
    numeric = {
        key: {"parent": float(left[key]), "child": float(right[key])}
        for key in sorted(set(left) & set(right))
        if _is_number(left[key]) and _is_number(right[key])
    }
    only_parent = sorted(key for key in left if not _is_number(left.get(key)) and key not in right)
    only_child = sorted(key for key in right if not _is_number(right.get(key)) and key not in left)
    parent_only = sorted(set(left) - set(right))
    child_only = sorted(set(right) - set(left))
    return {
        "numeric": numeric,
        "parent_only": parent_only,
        "child_only": child_only,
        "non_numeric_ignored": sorted(set(only_parent) | set(only_child)),
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _tolerance_of(metric_name: str) -> dict[str, Any]:
    """指标容差：先按单位查表，未登记单位用相对容差。"""
    unit = M.unit_of(metric_name)
    if unit in ABSOLUTE_TOLERANCES:
        return {"mode": "absolute", "unit": unit, "value": ABSOLUTE_TOLERANCES[unit]}
    return {"mode": "relative", "unit": unit, "value": DEFAULT_RELATIVE_TOLERANCE}


def _exceeds(metric_name: str, parent: float, child: float) -> bool:
    delta = abs(float(child) - float(parent))
    tol = _tolerance_of(metric_name)
    if tol["mode"] == "absolute":
        return delta > float(tol["value"])
    return delta > max(abs(float(parent)) * float(tol["value"]), 1e-9)


def metric_deltas(parent: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    """逐指标差值（``child - parent``；含相对差与是否越界）。"""
    result: dict[str, Any] = {}
    for name, pair in metric_pairs(parent, child)["numeric"].items():
        left, right = pair["parent"], pair["child"]
        delta = round(right - left, 9)
        relative = round(delta / abs(left), 9) if left else None
        result[name] = {
            "parent": left,
            "child": right,
            "delta": delta,
            "relative_delta": relative,
            "unit": M.unit_of(name),
            "tolerance": _tolerance_of(name),
            "verdict_neutral": name in VERDICT_NEUTRAL_METRICS,
            "exceeds_tolerance": _exceeds(name, left, right),
        }
    return result


def first_number(mapping: Mapping[str, Any], keys: Sequence[str]) -> tuple[float | None, str | None]:
    """按候选键取第一个数值（返回 ``(value, key)``；取不到返回 ``(None, None)``）。"""
    for key in keys:
        value = mapping.get(key)
        if _is_number(value):
            return float(value), key
    return None, None


def duration_ms_of(passport: Mapping[str, Any]) -> tuple[float | None, str]:
    """Run 时长（毫秒）：优先 ``metrics.duration_ms``，否则用起止时间差。"""
    value, key = first_number(passport.get("metrics") or {}, ("duration_ms", "wall_ms"))
    if value is not None:
        return value, f"metrics.{key}"
    started, finished = passport.get("started_at"), passport.get("finished_at")
    try:
        start_dt, end_dt = datetime.fromisoformat(str(started)), datetime.fromisoformat(str(finished))
    except (TypeError, ValueError):
        return None, "unavailable"
    return (end_dt - start_dt).total_seconds() * 1000.0, "started_at→finished_at"


def config_changes(parent: Mapping[str, Any], child: Mapping[str, Any]) -> list[dict[str, Any]]:
    """配置维度变更清单（逐字段，**不做静默归一**）。"""
    changes: list[dict[str, Any]] = []
    for field in CONFIG_FIELDS:
        left, right = parent.get(field), child.get(field)
        if _stable(left) != _stable(right):
            changes.append({"field": field, "parent": left, "child": right})
    left_params = _stable_params(parent.get("generation_params"))
    right_params = _stable_params(child.get("generation_params"))
    for key in sorted(set(left_params) | set(right_params)):
        if left_params.get(key) != right_params.get(key):
            changes.append(
                {
                    "field": f"generation_params.{key}",
                    "parent": left_params.get(key),
                    "child": right_params.get(key),
                }
            )
    return changes


def _stable(value: Any) -> str:
    from executor.artifact_collector import canonical_json

    try:
        return canonical_json(value)
    except (TypeError, ValueError):  # pragma: no cover - 极端不可序列化值
        return repr(value)


def _stable_params(payload: Any) -> dict[str, Any]:
    data = dict(payload) if isinstance(payload, Mapping) else {}
    return {key: value for key, value in data.items() if key not in VOLATILE_PARAM_KEYS}


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def diff(parent_passport: Any, child_passport: Any) -> dict[str, Any]:
    """生成差异报告（契约字段：``metric_deltas`` / ``cost_delta`` / ``duration_delta`` /
    ``config_changes`` / ``verdict``）。"""
    parent = _as_mapping(parent_passport)
    child = _as_mapping(child_passport)

    deltas = metric_deltas(parent, child)
    sets = metric_pairs(parent, child)
    exceeded = sorted(
        name
        for name, item in deltas.items()
        if item["exceeds_tolerance"] and not item.get("verdict_neutral")
    )
    neutral = sorted(
        name for name, item in deltas.items() if item["exceeds_tolerance"] and item.get("verdict_neutral")
    )

    parent_cost, child_cost = parent.get("cost_usd"), child.get("cost_usd")
    cost_delta = _delta_payload(parent_cost, child_cost, tolerance={"mode": "absolute", "unit": "usd", "value": ABSOLUTE_TOLERANCES["usd"]})

    parent_duration, parent_source = duration_ms_of(parent)
    child_duration, child_source = duration_ms_of(child)
    duration_delta = _delta_payload(
        parent_duration,
        child_duration,
        tolerance={"mode": "absolute", "unit": "ms", "value": ABSOLUTE_TOLERANCES["ms"]},
        extra={"parent_source": parent_source, "child_source": child_source},
    )

    changes = config_changes(parent, child)
    cfg_changed = bool(changes)

    cost_exceeded = bool(cost_delta.get("exceeds_tolerance"))
    reasons: list[str] = []
    if exceeded:
        reasons.append(f"数值指标超出容差：{exceeded}")
    if neutral:
        reasons.append(f"以下指标超出容差但属环境噪声、不参与 verdict：{neutral}")
    if cost_exceeded:
        reasons.append("成本差值超出容差")
    if cfg_changed:
        reasons.append(f"配置变更 {len(changes)} 项：" + "、".join(str(item["field"]) for item in changes[:6]))

    if not cfg_changed and not exceeded and not cost_exceeded:
        verdict = "identical"
        reasons = ["配置与全部数值指标、成本在容差内完全一致"]
        if neutral:
            reasons.append(f"仅环境耗时类指标（{neutral}）有差异，不影响结论一致性")
    elif exceeded or cost_exceeded:
        verdict = "divergent"
    else:
        verdict = "minor"
        if not reasons:
            reasons.append("仅存在容差内的微小差异")

    report: dict[str, Any] = {
        "parent_passport_id": parent.get("id"),
        "child_passport_id": child.get("id"),
        "parent_passport_uid": parent.get("passport_uid"),
        "child_passport_uid": child.get("passport_uid"),
        "parent_run_id": parent.get("experiment_run_id"),
        "child_run_id": child.get("experiment_run_id"),
        "parent_status": parent.get("status"),
        "child_status": child.get("status"),
        "parent_is_replay": bool(parent.get("is_replay")),
        "child_is_replay": bool(child.get("is_replay")),
        "metric_deltas": deltas,
        # ---- WP15 前端（PassportViewer.vue）读取的别名：与 *_deltas/*_delta 内容完全一致 ----
        "metric_diff": deltas,
        "cost_diff": cost_delta,
        "duration_diff": duration_delta,
        "metric_sets": {
            "parent_only": sets["parent_only"],
            "child_only": sets["child_only"],
            "non_numeric_ignored": sets["non_numeric_ignored"],
            "compared": sorted(deltas),
        },
        "cost_delta": cost_delta,
        "duration_delta": duration_delta,
        "config_changes": changes,
        "verdict": verdict,
        "verdict_reasons": reasons,
        "verdict_basis": [
            "config_changes（非易变字段）",
            "metric_deltas（仅两侧都是数值的指标；duration_ms 等环境耗时类指标不参与）",
            "cost_delta",
            "duration_delta 仅供参考，不参与 verdict（环境耗时噪声）",
        ],
        "verdict_neutral_metrics": sorted(VERDICT_NEUTRAL_METRICS),
        "missing_fields": {
            "parent": list(parent.get("missing_fields") or []),
            "child": list(child.get("missing_fields") or []),
        },
        "note": (
            "回放（is_replay=true）与父 Passport 的差异反映的是「同一输入在回放链路上的重算」；"
            "重跑（is_replay=false）的差异反映当前模型/配置下的重新执行。"
            "父或子为 incomplete 时，本报告的结论不得用于宣称可复现。"
            if child.get("is_replay")
            else "本次为 rerun（is_replay=false）：差异反映当前执行环境的重新结果。"
        ),
    }
    logger.info(
        "Passport 差异报告 parent=%s child=%s verdict=%s exceeded=%s config_changes=%s",
        parent.get("id"),
        child.get("id"),
        verdict,
        exceeded,
        len(changes),
    )
    return report


def _delta_payload(
    parent: Any,
    child: Any,
    *,
    tolerance: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """通用差值载荷（任一侧缺失即 ``comparable=false``，不用 0 冒充）。"""
    left = float(parent) if _is_number(parent) else None
    right = float(child) if _is_number(child) else None
    comparable = left is not None and right is not None
    delta = round(right - left, 9) if comparable else None
    exceeds = (
        abs(float(delta)) > float(tolerance.get("value") or 0.0) if comparable else None
    )
    payload: dict[str, Any] = {
        "parent": left,
        "child": right,
        "delta": delta,
        "relative_delta": round(delta / abs(left), 9) if comparable and left else None,
        "comparable": comparable,
        "tolerance": dict(tolerance),
        "exceeds_tolerance": exceeds,
    }
    if extra:
        payload.update(dict(extra))
    return payload


__all__ = [
    "ABSOLUTE_TOLERANCES",
    "CONFIG_FIELDS",
    "DEFAULT_RELATIVE_TOLERANCE",
    "VERDICTS",
    "VOLATILE_PARAM_KEYS",
    "config_changes",
    "diff",
    "duration_ms_of",
    "first_number",
    "metric_deltas",
    "metric_pairs",
]
