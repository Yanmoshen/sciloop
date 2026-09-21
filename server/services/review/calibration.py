# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""人工标签与一致率校准（WP12-T4，附录 D.3「校准」行）。

契约（``contracts.blind_review_rules.calibration``）：

- 人工标签**至少 3 条**（``CALIBRATION_HUMAN_LABEL_MIN``），不足时
  ``status='pending'`` 且**不展示一致率**；
- 分类任务用 **Cohen kappa**，连续分用 **MAE**；
- **必须展示 ``sample_size``**；小样本（< 10）**不得宣称统计显著**——只报一致率与样本数，
  并显式写明「仅作可靠性示意」。

落库字段与附录 A.6 ``review_calibrations`` 一一对应（13 个业务字段）：
``generator_model_ref`` / ``reviewer_model_ref`` / ``anonymization_version`` /
``shuffle_seed`` / ``candidate_order`` / ``model_scores`` / ``human_labels`` /
``sample_size`` / ``agreement_metric`` / ``agreement_value`` /
``confidence_interval`` / ``status``（+ ``pipeline_run_id``）。

本模块不做任何数据编造：算不出来就返回 ``computable=False`` 与原因。
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("sciloop.review.calibration")

#: 五维（附录 D.3）
DIMENSIONS: tuple[str, ...] = (
    "novelty",
    "feasibility",
    "rigor",
    "cost_reasonableness",
    "risk_control",
)

SCORE_MAX = 20
TOTAL_MAX = SCORE_MAX * len(DIMENSIONS)

#: 允许的一致率指标（``review_calibrations`` 的 CHECK 约束）
AGREEMENT_METRICS: tuple[str, ...] = ("cohen_kappa", "mae")

#: 人工标签的判定（通过 / 退回 / 拒绝）
HUMAN_DECISIONS: tuple[str, ...] = ("approve", "revise", "reject")

#: 校准状态（``review_calibrations`` 的 CHECK 约束）
STATUS_PENDING = "pending"
STATUS_CALIBRATED = "calibrated"

#: 「小样本不得宣称统计显著」的阈值（附录 D.3 / 工作包硬约束）
SMALL_SAMPLE_THRESHOLD = 10

#: 未达阈值时的固定措辞（前端与报告共用，避免各处自行发挥）
NON_SIGNIFICANT_NOTE = (
    "样本量 {n}（< {threshold}）：一致率仅作可靠性示意，**不构成统计显著结论**，"
    "不得用于宣称评审质量或推广到总体。"
)

#: bootstrap 重采样次数与随机种子（固定种子 → 区间可复现）
BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SEED = 20260917


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
@dataclass
class HumanLabel:
    """一条人工标签（通过 / 退回 + 五维分）。"""

    candidate_alias: str | None
    method_index: int | None
    decision: str
    scores: dict[str, int] = field(default_factory=dict)
    total: int | None = None
    note: str | None = None
    labeler: str | None = None
    labeled_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_alias": self.candidate_alias,
            "method_index": self.method_index,
            "decision": self.decision,
            "scores": dict(self.scores),
            "total": self.total,
            "note": self.note,
            "labeler": self.labeler,
            "labeled_at": self.labeled_at,
        }


class CalibrationError(RuntimeError):
    """校准入参或数据状态不合法（接口层会映射为 4xx）。"""

    code = "calibration_error"

    def __init__(self, message: str, *, detail: Any = None, code: str | None = None) -> None:
        self.detail = detail
        if code:
            self.code = code
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "detail": self.detail}


def normalize_human_label(raw: Mapping[str, Any]) -> HumanLabel:
    """校验并规范化一条人工标签。

    - ``decision`` 必须属于 ``approve|revise|reject``；
    - 五维分若给出，必须是 0-20 的整数（越界即报错，**不静默截断**）；
    - ``total`` 由服务端按五维之和重算（忽略调用方传入的 total），避免前后不一致。
    """
    decision = str(raw.get("decision") or raw.get("verdict") or "").strip().lower()
    if decision not in HUMAN_DECISIONS:
        raise CalibrationError(
            f"人工标签 decision 非法：{raw.get('decision')!r}，合法值 {list(HUMAN_DECISIONS)}",
            detail={"allowed": list(HUMAN_DECISIONS)},
            code="invalid_human_label",
        )

    alias_raw = raw.get("candidate_alias") or raw.get("alias")
    alias = str(alias_raw).strip().upper() if alias_raw not in (None, "") else None

    index_raw = raw.get("method_index")
    method_index: int | None = None
    if index_raw not in (None, ""):
        try:
            method_index = int(index_raw)
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                f"人工标签 method_index 非整数：{index_raw!r}",
                code="invalid_human_label",
            ) from exc
        if method_index < 0:
            raise CalibrationError(
                f"人工标签 method_index 不能为负：{method_index}",
                code="invalid_human_label",
            )

    if alias is None and method_index is None:
        raise CalibrationError(
            "人工标签必须给出 candidate_alias 或 method_index 之一（用于与模型评审对齐）",
            code="invalid_human_label",
        )

    raw_scores = raw.get("scores") if isinstance(raw.get("scores"), Mapping) else raw
    scores: dict[str, int] = {}
    for dimension in DIMENSIONS:
        value = raw_scores.get(dimension) if isinstance(raw_scores, Mapping) else None
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalibrationError(
                f"人工标签 {dimension} 非数值：{value!r}",
                code="invalid_human_label",
            )
        number = float(value)
        if number != int(number):
            raise CalibrationError(
                f"人工标签 {dimension} 必须为整数（0-20）：{value!r}",
                code="invalid_human_label",
            )
        number_int = int(number)
        if not 0 <= number_int <= SCORE_MAX:
            raise CalibrationError(
                f"人工标签 {dimension} 超出 0-{SCORE_MAX}：{number_int}",
                code="invalid_human_label",
            )
        scores[dimension] = number_int

    total = sum(scores.values()) if scores else None
    return HumanLabel(
        candidate_alias=alias,
        method_index=method_index,
        decision=decision,
        scores=scores,
        total=total,
        note=str(raw.get("note")).strip() if raw.get("note") else None,
        labeler=str(raw.get("labeler")).strip() if raw.get("labeler") else None,
        labeled_at=str(raw.get("labeled_at") or datetime.now(UTC).isoformat()),
    )


def normalize_human_labels(labels: Iterable[Mapping[str, Any]]) -> list[HumanLabel]:
    return [normalize_human_label(item) for item in labels]


# --------------------------------------------------------------------------- #
# 对齐：模型评审分 × 人工标签
# --------------------------------------------------------------------------- #
@dataclass
class PairedLabel:
    """同一候选的「模型分 + 人工分」配对（两侧都在才算样本）。"""

    alias: str
    method_index: int | None
    model_total: int | None
    human_total: int | None
    model_selected: bool
    human_positive: bool
    model_scores: dict[str, int] = field(default_factory=dict)
    human_scores: dict[str, int] = field(default_factory=dict)


def pair_labels(
    model_scores: Sequence[Mapping[str, Any]],
    human_labels: Sequence[HumanLabel],
) -> tuple[list[PairedLabel], list[str]]:
    """按 ``candidate_alias`` 优先、``method_index`` 兜底对齐两侧标签。

    返回 ``(配对成功列表, 未配对原因列表)``——**未配对的部分不参与计算，也不丢弃证据**
    （原因会原样出现在报告里，避免「悄悄少算几条」）。
    """
    warnings: list[str] = []
    by_alias: dict[str, Mapping[str, Any]] = {}
    by_index: dict[int, Mapping[str, Any]] = {}
    for item in model_scores:
        if not isinstance(item, Mapping):
            continue
        alias = str(item.get("alias") or item.get("candidate_alias") or "").strip().upper()
        if alias:
            by_alias[alias] = item
        index = item.get("method_index")
        if isinstance(index, int) and not isinstance(index, bool):
            by_index[index] = item

    used: set[str] = set()
    paired: list[PairedLabel] = []
    for label in human_labels:
        model: Mapping[str, Any] | None = None
        key = label.candidate_alias or (str(label.method_index) if label.method_index is not None else None)
        if label.candidate_alias and label.candidate_alias in by_alias:
            model = by_alias[label.candidate_alias]
        elif label.method_index is not None and label.method_index in by_index:
            model = by_index[label.method_index]
        if model is None:
            warnings.append(
                f"人工标签 {label.candidate_alias or label.method_index} 在模型评审结果中找不到对应候选，"
                "未纳入一致率计算"
            )
            continue
        if key is not None:
            if key in used:
                warnings.append(f"同一候选 {key} 存在重复人工标签，仅保留最后一条")
                paired = [item for item in paired if item.alias != key]
            used.add(key)

        model_scores_map = {
            dimension: _as_int(model.get(dimension)) for dimension in DIMENSIONS
        }
        model_scores_map = {k: v for k, v in model_scores_map.items() if v is not None}
        model_total = _as_int(model.get("total"))
        if model_total is None and model_scores_map:
            model_total = sum(model_scores_map.values())

        paired.append(
            PairedLabel(
                alias=label.candidate_alias or (model.get("alias") or "") or "",
                method_index=(
                    label.method_index
                    if label.method_index is not None
                    else _as_int(model.get("method_index"))
                ),
                model_total=model_total,
                human_total=label.total,
                model_selected=bool(model.get("selected")),
                human_positive=label.decision == "approve",
                model_scores=model_scores_map,
                human_scores=dict(label.scores),
            )
        )
    return paired, warnings


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def cohen_kappa(pairs: Sequence[PairedLabel]) -> dict[str, Any]:
    """分类一致率：Cohen kappa（判定「该候选是否应通过」的二元一致性）。

    口径：模型侧 positive = 该候选被模型选为主要方案（``selected=True``）；
    人工侧 positive = 人工判定 ``approve``。两侧都只有二元，故用 kappa 而非加权 kappa。
    """
    n = len(pairs)
    if n == 0:
        return _not_computable("cohen_kappa", "no_paired_labels", sample_size=0)

    counts = {"n11": 0, "n10": 0, "n01": 0, "n00": 0}
    for item in pairs:
        model_pos = bool(item.model_selected)
        human_pos = bool(item.human_positive)
        if model_pos and human_pos:
            counts["n11"] += 1
        elif model_pos and not human_pos:
            counts["n10"] += 1
        elif not model_pos and human_pos:
            counts["n01"] += 1
        else:
            counts["n00"] += 1

    po = (counts["n11"] + counts["n00"]) / n
    model_pos_rate = (counts["n11"] + counts["n10"]) / n
    human_pos_rate = (counts["n11"] + counts["n01"]) / n
    pe = model_pos_rate * human_pos_rate + (1 - model_pos_rate) * (1 - human_pos_rate)

    if math.isclose(pe, 1.0):
        # 双方判定完全同向（例如全部通过或全部不通过）：kappa 分母为 0，数学上无定义
        return _not_computable(
            "cohen_kappa",
            "degenerate_marginals_pe_equals_1",
            sample_size=n,
            extra={"observed_agreement": round(po, 4), "expected_agreement": round(pe, 4), "confusion": counts},
        )

    kappa = (po - pe) / (1 - pe)
    return {
        "metric": "cohen_kappa",
        "value": round(kappa, 4),
        "computable": True,
        "reason": None,
        "sample_size": n,
        "observed_agreement": round(po, 4),
        "expected_agreement": round(pe, 4),
        "confusion": counts,
        "per_candidate": [
            {
                "alias": item.alias,
                "method_index": item.method_index,
                "model_positive": bool(item.model_selected),
                "human_positive": bool(item.human_positive),
                "agree": bool(item.model_selected) == bool(item.human_positive),
            }
            for item in pairs
        ],
        "definition": "model positive = 该候选被模型选为主方案；human positive = 人工判定 approve",
    }


def mae(pairs: Sequence[PairedLabel], *, field_name: str = "total") -> dict[str, Any]:
    """连续分一致率：MAE（五维总分 0-100，单位「分」）。

    只对「模型与人工都给了分」的候选计算；缺失一侧的候选计入 ``skipped`` 并说明。
    """
    errors: list[float] = []
    skipped: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for item in pairs:
        if item.model_total is None or item.human_total is None:
            skipped.append(
                {
                    "alias": item.alias,
                    "method_index": item.method_index,
                    "reason": "模型分缺失" if item.model_total is None else "人工分缺失",
                }
            )
            continue
        error = abs(float(item.model_total) - float(item.human_total))
        errors.append(error)
        details.append(
            {
                "alias": item.alias,
                "method_index": item.method_index,
                "model_total": item.model_total,
                "human_total": item.human_total,
                "abs_error": round(error, 4),
            }
        )

    if not errors:
        return _not_computable("mae", "no_continuous_pairs", sample_size=0, extra={"skipped": skipped})

    mean_error = sum(errors) / len(errors)
    per_dimension = _per_dimension_mae(pairs)
    return {
        "metric": "mae",
        "value": round(mean_error, 4),
        "computable": True,
        "reason": None,
        "sample_size": len(errors),
        "unit": "分（0-100 总分尺度）",
        "max_possible": TOTAL_MAX,
        "per_dimension_mae": per_dimension,
        "per_candidate": details,
        "skipped": skipped,
        "definition": "MAE = mean(|模型五维之和 - 人工五维之和|)",
    }


def _per_dimension_mae(pairs: Sequence[PairedLabel]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for dimension in DIMENSIONS:
        deltas: list[float] = []
        for item in pairs:
            if dimension in item.model_scores and dimension in item.human_scores:
                deltas.append(float(abs(item.model_scores[dimension] - item.human_scores[dimension])))
        result[dimension] = {
            "mae": round(sum(deltas) / len(deltas), 4) if deltas else None,
            "sample_size": len(deltas),
            "max_possible": SCORE_MAX,
        }
    return result


def _not_computable(
    metric: str, reason: str, *, sample_size: int, extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metric": metric,
        "value": None,
        "computable": False,
        "reason": reason,
        "sample_size": sample_size,
    }
    if extra:
        payload.update(dict(extra))
    return payload


def bootstrap_ci(
    statistic: Any,
    items: Sequence[Any],
    *,
    rounds: int = BOOTSTRAP_ROUNDS,
    seed: int = BOOTSTRAP_SEED,
    level: float = 0.95,
) -> dict[str, Any]:
    """百分位 bootstrap 置信区间（固定种子 → 可复现）。

    小样本下区间会非常宽——这本身就是「不显著」的诚实呈现，因此照实给出。
    """
    if not items:
        return {"low": None, "high": None, "level": level, "method": "insufficient_sample", "rounds": 0}
    rng = random.Random(seed)
    values: list[float] = []
    n = len(items)
    for _ in range(max(1, rounds)):
        sample = [items[rng.randrange(n)] for _ in range(n)]
        try:
            value = statistic(sample)
        except ZeroDivisionError:
            continue
        except Exception:  # noqa: BLE001 - bootstrap 失败不得影响主结果
            continue
        if value is None:
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            continue
        values.append(float(value))
    if not values:
        return {"low": None, "high": None, "level": level, "method": "bootstrap_failed", "rounds": 0}
    values.sort()
    alpha = (1.0 - level) / 2.0
    low = values[max(0, min(len(values) - 1, int(alpha * len(values))))]
    high = values[max(0, min(len(values) - 1, int((1 - alpha) * len(values)) - 1))]
    return {
        "low": round(low, 4),
        "high": round(high, 4),
        "level": level,
        "method": "bootstrap_percentile",
        "rounds": len(values),
        "seed": seed,
    }


# --------------------------------------------------------------------------- #
# 汇总报告
# --------------------------------------------------------------------------- #
def compute_agreement(
    model_scores: Sequence[Mapping[str, Any]],
    human_labels: Sequence[HumanLabel],
    *,
    primary_metric: str = "cohen_kappa",
    min_samples: int = 3,
    report_ci: bool = True,
) -> dict[str, Any]:
    """计算完整校准报告（人工标签不足时 ``status='pending'`` 且不展示一致率）。

    ``primary_metric`` 取 ``CALIBRATION_METRIC``（默认 kappa）；若主指标在小样本下
    数学上不可计算（例如 kappa 边际退化）而另一指标可算，则**如实降级到另一个指标**
    并在 ``metric_fallback`` 里写明原因——绝不编造 kappa 数值。
    """
    pairs, warnings = pair_labels(model_scores, human_labels)
    sample_size = len(pairs)

    report: dict[str, Any] = {
        "status": STATUS_PENDING,
        "sample_size": sample_size,
        "sample_size_requirement": min_samples,
        "small_sample_threshold": SMALL_SAMPLE_THRESHOLD,
        "primary_metric": primary_metric,
        "agreement_metric": None,
        "agreement_value": None,
        "confidence_interval": None,
        "significance": "not_established" if sample_size < SMALL_SAMPLE_THRESHOLD else "approximate",
        "note": None,
        "metric_fallback": None,
        "warnings": warnings,
        "pairs": [
            {
                "alias": item.alias,
                "method_index": item.method_index,
                "model_total": item.model_total,
                "human_total": item.human_total,
                "model_selected": item.model_selected,
                "human_decision": "approve" if item.human_positive else "not_approve",
                "agree": bool(item.model_selected) == bool(item.human_positive),
            }
            for item in pairs
        ],
    }

    if sample_size < min_samples:
        report["note"] = (
            f"人工标签 {sample_size} 条 < 阈值 {min_samples} 条："
            "状态 pending，**不展示一致率**（契约要求先补足人工标签）"
        )
        report["classification"] = _not_computable("cohen_kappa", "insufficient_human_labels", sample_size=sample_size)
        report["continuous"] = _not_computable("mae", "insufficient_human_labels", sample_size=sample_size)
        return report

    classification = cohen_kappa(pairs)
    continuous = mae(pairs)
    report["classification"] = classification
    report["continuous"] = continuous

    chosen_metric = primary_metric if primary_metric in AGREEMENT_METRICS else "cohen_kappa"
    chosen = classification if chosen_metric == "cohen_kappa" else continuous
    other_name = "mae" if chosen_metric == "cohen_kappa" else "cohen_kappa"
    other = continuous if chosen_metric == "cohen_kappa" else classification

    if not chosen.get("computable") and other.get("computable"):
        report["metric_fallback"] = (
            f"主指标 {chosen_metric} 不可计算（{chosen.get('reason')}），"
            f"按契约改为展示 {other_name}；不编造数值"
        )
        chosen_metric, chosen, other_name = other_name, other, chosen_metric
    if not chosen.get("computable"):
        report["note"] = (
            f"人工标签 {sample_size} 条已达标，但一致率不可计算"
            f"（kappa：{classification.get('reason')}；mae：{continuous.get('reason')}）"
        )
        return report

    report["agreement_metric"] = chosen_metric
    report["agreement_value"] = chosen.get("value")
    report["status"] = STATUS_CALIBRATED

    if report_ci:
        statistic = (
            _kappa_statistic if chosen_metric == "cohen_kappa" else _mae_statistic
        )
        ci = bootstrap_ci(statistic, pairs)
        significance = "not_established" if sample_size < SMALL_SAMPLE_THRESHOLD else "approximate"
        ci["significance"] = significance
        ci["sample_size"] = sample_size
        if significance == "not_established":
            ci["note"] = NON_SIGNIFICANT_NOTE.format(n=sample_size, threshold=SMALL_SAMPLE_THRESHOLD)
        else:
            ci["note"] = (
                f"样本量 {sample_size} ≥ {SMALL_SAMPLE_THRESHOLD}，区间为近似区间；"
                "一致率本身不等于评审正确率，仍需人工复核。"
            )
        report["confidence_interval"] = ci
        report["significance"] = significance
        report["note"] = ci["note"]
    else:
        report["note"] = NON_SIGNIFICANT_NOTE.format(n=sample_size, threshold=SMALL_SAMPLE_THRESHOLD)
        report["conclusion"] = report["note"]

    report["summary"] = (
        f"一致率 {report['agreement_metric']} = {report['agreement_value']}，"
        f"sample_size = {sample_size}"
        + ("" if sample_size >= SMALL_SAMPLE_THRESHOLD else "（小样本，不宣称统计显著）")
    )
    return report


def _kappa_statistic(sample: Sequence[PairedLabel]) -> float | None:
    result = cohen_kappa(sample)
    return None if not result.get("computable") else float(result["value"])


def _mae_statistic(sample: Sequence[PairedLabel]) -> float | None:
    result = mae(sample)
    return None if not result.get("computable") else float(result["value"])


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != int(number):
        return int(round(number))
    return int(number)


# --------------------------------------------------------------------------- #
# 库访问：读 / 写 review_calibrations
# --------------------------------------------------------------------------- #
def build_model_scores(
    scores: Sequence[Mapping[str, Any]],
    *,
    candidate_order: Sequence[Mapping[str, Any]],
    selected_method_index: int | None,
) -> list[dict[str, Any]]:
    """把环节打分整理成落库结构（含 alias→method_index 映射与 ``selected`` 标记）。"""
    alias_by_index: dict[int, str] = {}
    for item in candidate_order:
        if not isinstance(item, Mapping):
            continue
        index = _as_int(item.get("method_index"))
        alias = item.get("alias")
        if index is not None and alias:
            alias_by_index[index] = str(alias)

    result: list[dict[str, Any]] = []
    for item in scores:
        if not isinstance(item, Mapping):
            continue
        index = _as_int(item.get("method_index"))
        entry: dict[str, Any] = {
            "alias": str(item.get("candidate_alias") or alias_by_index.get(index or -1) or ""),
            "method_index": index,
            "total": _as_int(item.get("total")),
            "selected": index is not None and index == selected_method_index,
            "comments": list(item.get("comments") or []),
        }
        for dimension in DIMENSIONS:
            entry[dimension] = _as_int(item.get(dimension))
        result.append(entry)
    return result


async def latest_run_id(project_id: int) -> int | None:
    """项目最近一次 pipeline_run（校准记录挂在 run 上）。"""
    from sqlalchemy import select

    from db.models import PipelineRun
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:
        raise CalibrationError("数据库不可用（AsyncSessionLocal 未初始化）", code="db_unavailable")
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(PipelineRun.id)
                .where(PipelineRun.project_id == int(project_id))
                .order_by(PipelineRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return int(row) if row is not None else None


async def get_calibration_row(calibration_id: int) -> Any:
    from db.models import ReviewCalibration
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:
        raise CalibrationError("数据库不可用（AsyncSessionLocal 未初始化）", code="db_unavailable")
    async with AsyncSessionLocal() as session:
        row = await session.get(ReviewCalibration, int(calibration_id))
    if row is None:
        raise CalibrationError(f"review_calibrations 无 id={calibration_id} 的记录", code="not_found")
    return row


def row_to_dict(row: Any) -> dict[str, Any]:
    """``review_calibrations`` → 契约字段字典（13 个业务字段一次给全）。"""
    return {
        "calibration_id": int(row.id),
        "pipeline_run_id": int(row.pipeline_run_id),
        "generator_model_ref": row.generator_model_ref,
        "reviewer_model_ref": row.reviewer_model_ref,
        "anonymization_version": row.anonymization_version,
        "shuffle_seed": int(row.shuffle_seed),
        "candidate_order": row.candidate_order or [],
        "model_scores": row.model_scores or [],
        "human_labels": row.human_labels or [],
        "sample_size": int(row.sample_size or 0),
        "agreement_metric": row.agreement_metric,
        "agreement_value": float(row.agreement_value) if row.agreement_value is not None else None,
        "confidence_interval": row.confidence_interval,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def compute(calibration_id: int) -> dict[str, Any]:
    """按 ``review_calibrations`` 当前内容重算校准报告（只读，不写库）。

    对应 ``interfaces.provides`` 的 ``calibration.compute(calibration_id)``；
    返回值同时包含 ``metric`` / ``value`` / ``sample_size`` / ``ci`` 四个扁平键，
    便于 SSE ``review_calibrated`` 直接取用。
    """
    from core.config import get_settings

    row = await get_calibration_row(calibration_id)
    settings = get_settings()
    labels = normalize_human_labels(row.human_labels or [])
    report = compute_agreement(
        row.model_scores or [],
        labels,
        primary_metric=str(settings.calibration_metric or "cohen_kappa"),
        min_samples=int(settings.calibration_human_label_min or 3),
        report_ci=bool(settings.calibration_report_ci),
    )
    report.update(row_to_dict(row))
    report["metric"] = report.get("agreement_metric")
    report["value"] = report.get("agreement_value")
    report["ci"] = report.get("confidence_interval")
    return report


async def submit_human_labels(
    project_id: int,
    labels: Sequence[Mapping[str, Any]],
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """录入人工标签 → 重算一致率 → 落库（Owner 操作）。

    - 默认**按候选合并**（同一 alias/index 的新标签覆盖旧标签），便于反复标注；
    - ``replace=True`` 时整体替换；
    - 找不到 ``plan_review`` 产生的校准记录时报错（不允许凭空造一致率）。
    """
    from sqlalchemy import select

    from core.config import get_settings
    from db.models import PipelineRun, ReviewCalibration
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:
        raise CalibrationError("数据库不可用（AsyncSessionLocal 未初始化）", code="db_unavailable")

    parsed = normalize_human_labels(labels)
    settings = get_settings()

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ReviewCalibration)
                .join(PipelineRun, ReviewCalibration.pipeline_run_id == PipelineRun.id)
                .where(PipelineRun.project_id == int(project_id))
                .order_by(ReviewCalibration.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise CalibrationError(
                f"项目 {project_id} 尚无 plan_review 盲评记录，无法录入人工标签"
                "（请先运行流水线跑出 plan_review 环节）",
                code="calibration_not_found",
            )

        existing = [] if replace else list(row.human_labels or [])
        merged: dict[str, dict[str, Any]] = {}
        for item in existing:
            if not isinstance(item, Mapping):
                continue
            merged[_label_key(item)] = dict(item)
        for label in parsed:
            merged[_label_key(label.to_dict())] = label.to_dict()
        merged_list = list(merged.values())

        labels_parsed = normalize_human_labels(merged_list)
        report = compute_agreement(
            row.model_scores or [],
            labels_parsed,
            primary_metric=str(settings.calibration_metric or "cohen_kappa"),
            min_samples=int(settings.calibration_human_label_min or 3),
            report_ci=bool(settings.calibration_report_ci),
        )

        row.human_labels = merged_list
        row.sample_size = int(report["sample_size"])
        row.agreement_metric = report["agreement_metric"]
        row.agreement_value = report["agreement_value"]
        row.confidence_interval = {
            "ci": report.get("confidence_interval"),
            "significance": report.get("significance"),
            "note": report.get("note"),
            "classification": report.get("classification"),
            "continuous": report.get("continuous"),
            "metric_fallback": report.get("metric_fallback"),
            "small_sample_threshold": SMALL_SAMPLE_THRESHOLD,
        }
        row.status = report["status"]
        await session.commit()
        await session.refresh(row)

    report.update(row_to_dict(row))
    report["metric"] = report.get("agreement_metric")
    report["value"] = report.get("agreement_value")
    report["ci"] = report.get("confidence_interval")
    report["submitted_labels"] = [item.to_dict() for item in parsed]
    return report


async def latest_calibration_for_project(project_id: int) -> dict[str, Any] | None:
    """项目最近一次校准记录（未跑过 ``plan_review`` 时返回 ``None``）。"""
    from sqlalchemy import select

    from db.models import PipelineRun, ReviewCalibration
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:
        raise CalibrationError("数据库不可用（AsyncSessionLocal 未初始化）", code="db_unavailable")
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ReviewCalibration)
                .join(PipelineRun, ReviewCalibration.pipeline_run_id == PipelineRun.id)
                .where(PipelineRun.project_id == int(project_id))
                .order_by(ReviewCalibration.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return None if row is None else row


def _label_key(item: Mapping[str, Any]) -> str:
    alias = item.get("candidate_alias") or item.get("alias")
    if alias not in (None, ""):
        return f"alias:{str(alias).strip().upper()}"
    return f"index:{item.get('method_index')}"


__all__ = [
    "AGREEMENT_METRICS",
    "BOOTSTRAP_ROUNDS",
    "BOOTSTRAP_SEED",
    "CalibrationError",
    "DIMENSIONS",
    "HUMAN_DECISIONS",
    "HumanLabel",
    "NON_SIGNIFICANT_NOTE",
    "PairedLabel",
    "SCORE_MAX",
    "SMALL_SAMPLE_THRESHOLD",
    "STATUS_CALIBRATED",
    "STATUS_PENDING",
    "TOTAL_MAX",
    "bootstrap_ci",
    "build_model_scores",
    "cohen_kappa",
    "compute",
    "compute_agreement",
    "get_calibration_row",
    "latest_calibration_for_project",
    "latest_run_id",
    "mae",
    "normalize_human_label",
    "normalize_human_labels",
    "pair_labels",
    "row_to_dict",
    "submit_human_labels",
]
