# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""三重停止条件（WP09-T6，计划书 §2.5.1）。

======== ================ ==================================================================
条件      判据              演示配置
======== ================ ==================================================================
① 质量达标  ``总分 >= score_threshold``                       默认 80，**演示项目 72**
② 边际停滞  ``本轮总分 - 上轮总分 < marginal_gain_threshold``  默认 2
③ 轮次用尽  ``iteration >= max_iterations``                   默认 3
======== ================ ==================================================================

**取先满足者**：按 ①→②→③ 顺序判定，返回第一个满足的条件；``stop_reason`` 取自
``contracts.enums.stop_reason``（``score_threshold`` / ``marginal_stagnation`` /
``max_iterations`` / ``manual``），必须可查询并在 ``/status`` 响应中返回。

阈值优先级：任务书 > 项目设置 > 环境变量默认值。**演示项目（``projects.is_demo``）默认 72**
（计划书 §2.5.1 演示配置；v1.0 的 80 分会导致演示以「轮次用尽」收场）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sciloop.pipeline.stop_conditions")

WP_ID = "WP09"

#: 演示项目默认质量阈值（计划书 §2.5.1）
DEMO_SCORE_THRESHOLD = 72.0

STOP_REASONS: tuple[str, ...] = (
    "score_threshold",
    "marginal_stagnation",
    "max_iterations",
    "manual",
)


@dataclass
class StopThresholds:
    """三重条件的阈值快照（写入 status 响应，透明可查）。"""

    max_iterations: int = 3
    score_threshold: float = 80.0
    marginal_gain_threshold: float = 2.0
    score_threshold_source: str = "env_default"
    max_iterations_source: str = "env_default"
    marginal_gain_threshold_source: str = "env_default"
    is_demo: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_iterations": self.max_iterations,
            "score_threshold": self.score_threshold,
            "marginal_gain_threshold": self.marginal_gain_threshold,
            "score_threshold_source": self.score_threshold_source,
            "max_iterations_source": self.max_iterations_source,
            "marginal_gain_threshold_source": self.marginal_gain_threshold_source,
            "is_demo": self.is_demo,
        }


@dataclass
class StopDecision:
    """停止判定结果。"""

    should_stop: bool
    reason: str | None
    detail: str
    conditions: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_stop": self.should_stop,
            "stop_reason": self.reason,
            "detail": self.detail,
            "conditions": self.conditions,
            "inputs": self.inputs,
        }


def resolve_thresholds(
    *,
    project: Any | None = None,
    taskbook: Any | None = None,
    defaults: Any | None = None,
) -> StopThresholds:
    """按 任务书 > 项目设置 > 环境默认 合并三重条件阈值。"""
    if defaults is None:
        from app.core.config import get_settings

        defaults = get_settings()

    is_demo = bool(getattr(project, "is_demo", False))
    thresholds = StopThresholds(
        max_iterations=int(getattr(defaults, "pipeline_max_iterations", 3) or 3),
        score_threshold=float(
            getattr(defaults, "pipeline_score_threshold", 80.0)
            if not is_demo
            else DEMO_SCORE_THRESHOLD
        ),
        marginal_gain_threshold=float(
            getattr(defaults, "pipeline_marginal_gain_threshold", 2.0) or 2.0
        ),
        score_threshold_source="demo_default" if is_demo else "env_default",
        is_demo=is_demo,
    )

    settings = getattr(project, "settings", None)
    if isinstance(settings, dict):
        thresholds = _apply_overrides(thresholds, settings, source="project_settings")
    if taskbook is not None:
        thresholds = _apply_overrides(
            thresholds,
            {
                "max_iterations": getattr(taskbook, "max_iterations", None),
                "score_threshold": getattr(taskbook, "score_threshold", None),
                "marginal_gain_threshold": getattr(taskbook, "marginal_gain_threshold", None),
            },
            source="taskbook",
        )
    return thresholds


def _apply_overrides(base: StopThresholds, values: dict[str, Any], *, source: str) -> StopThresholds:
    for key, attr in (
        ("max_iterations", "max_iterations"),
        ("score_threshold", "score_threshold"),
        ("marginal_gain_threshold", "marginal_gain_threshold"),
    ):
        raw = values.get(key) if isinstance(values, dict) else None
        if raw is None:
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        if attr == "max_iterations":
            base.max_iterations = int(number)
        else:
            setattr(base, attr, number)
        setattr(base, f"{attr}_source", source)
    return base


def evaluate_stop_conditions(
    *,
    iteration: int,
    score: float | None,
    previous_score: float | None,
    thresholds: StopThresholds,
) -> StopDecision:
    """三重条件取先满足者（①质量达标 → ②边际停滞 → ③轮次用尽）。"""
    conditions: list[dict[str, Any]] = []
    inputs = {
        "iteration": int(iteration),
        "score": score,
        "previous_score": previous_score,
        "max_iterations": thresholds.max_iterations,
        "score_threshold": thresholds.score_threshold,
        "marginal_gain_threshold": thresholds.marginal_gain_threshold,
    }

    # ① 质量达标
    quality_met = score is not None and float(score) >= thresholds.score_threshold
    conditions.append(
        {
            "condition": "score_threshold",
            "met": bool(quality_met),
            "judgement": f"score={score} >= score_threshold={thresholds.score_threshold}",
            "note": None if score is not None else "本轮无 review 总分（不判定达标）",
        }
    )

    # ② 边际收益停滞（仅第 2 轮起有意义）
    marginal_gain: float | None = None
    if score is not None and previous_score is not None:
        marginal_gain = round(float(score) - float(previous_score), 6)
    stagnant = marginal_gain is not None and marginal_gain < thresholds.marginal_gain_threshold
    conditions.append(
        {
            "condition": "marginal_stagnation",
            "met": bool(stagnant),
            "judgement": (
                f"marginal_gain={marginal_gain} < marginal_gain_threshold="
                f"{thresholds.marginal_gain_threshold}"
                if marginal_gain is not None
                else "缺少上一轮总分，无法判定边际收益"
            ),
            "marginal_gain": marginal_gain,
        }
    )

    # ③ 轮次用尽
    exhausted = int(iteration) >= thresholds.max_iterations
    conditions.append(
        {
            "condition": "max_iterations",
            "met": bool(exhausted),
            "judgement": f"iteration={iteration} >= max_iterations={thresholds.max_iterations}",
        }
    )

    inputs["marginal_gain"] = marginal_gain

    for condition in conditions:
        if condition["met"]:
            decision = StopDecision(
                should_stop=True,
                reason=condition["condition"],
                detail=condition["judgement"],
                conditions=conditions,
                inputs=inputs,
            )
            logger.info(
                json.dumps(
                    {
                        "event": "stop_condition_met",
                        "wp_id": WP_ID,
                        "stop_reason": decision.reason,
                        "detail": decision.detail,
                        **inputs,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            return decision

    return StopDecision(
        should_stop=False,
        reason=None,
        detail="三重停止条件均未满足，进入下一轮迭代",
        conditions=conditions,
        inputs=inputs,
    )


def extract_score(stage_output: dict[str, Any] | None, metrics: dict[str, Any] | None = None) -> float | None:
    """从 review 环节产出中提取总分（真实值；取不到返回 ``None``，禁止编造）。

    review_scores 四维各 0–25，``total`` = 四维之和（计划书 §2.2 7.6）。
    """
    for payload in (metrics or {}, stage_output or {}):
        if not isinstance(payload, dict):
            continue
        for key in ("total", "review_total", "score"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        scores = payload.get("scores")
        if isinstance(scores, dict):
            total = payload.get("total")
            if isinstance(total, (int, float)):
                return float(total)
            dims = [scores.get(k) for k in ("novelty", "rigor", "completeness", "reproducibility")]
            if all(isinstance(v, (int, float)) for v in dims):
                return float(sum(dims))  # type: ignore[arg-type]
    return None


__all__ = [
    "DEMO_SCORE_THRESHOLD",
    "STOP_REASONS",
    "StopDecision",
    "StopThresholds",
    "evaluate_stop_conditions",
    "extract_score",
    "resolve_thresholds",
]
