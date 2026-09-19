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
"""四维评分（WP14-T4，附录 D.6）。

口径
----
``novelty`` / ``rigor`` / ``completeness`` / ``reproducibility`` 各 0–25，
``total`` = 四维之和（**服务端重算**，模型自报的 total 一律忽略）。

**评分是可复现的 rubric**：每一维由真实可核验信号加权重算得到（分项明细写进
``review_scores.comments.rubric``，可审计复算）：

====================================  ==========================================
novelty                               上游 plan_review 的新颖性分（0–20 → 0–1）/
                                      证据池论文广度 / 空白（gaps）信号
rigor                                 claim_coverage / 矛盾 Claim 占比 /
                                      Passport 完整度 / 已绑定证据密度
completeness                          章节覆盖 / 事实性 Claim 覆盖率 / 证据池规模
reproducibility                       Passport 状态 / 真实指标数量 /
                                      实时而非回放 / 溯源哈希齐备度
====================================  ==========================================

- 分项缺失（如 plan_review 未产出）→ 按剩余权重**归一**并记 ``score_coverage``
  （与 ``contracts.ranking_and_influence.null_rule`` 同口径），**禁止编造数值**。
- 四维均无法计算时抛 :class:`ScoringUnavailable`（环节按 L2 处理），不做占位。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sciloop.wp14.scorer")

WP_ID = "WP14"
RUBRIC_VERSION = "review-rubric-v1"

#: 四维与上限（附录 D.6）
DIMENSIONS: tuple[str, ...] = ("novelty", "rigor", "completeness", "reproducibility")
DIM_MAX = 25.0

#: Passport 状态 → 完整度因子（缺失时用 0.5 并在 note 中披露）
PASSPORT_FACTORS: dict[str, float] = {
    "complete": 1.0,
    "incomplete": 0.6,
    "failed": 0.2,
    "missing": 0.5,
}

#: Passport 溯源哈希字段（可复现性证据）
PROVENANCE_FIELDS: tuple[str, ...] = (
    "dataset_sha256",
    "prompt_sha256",
    "code_commit_sha",
    "dependency_lock_sha256",
)


class ScoringUnavailable(RuntimeError):
    """四维均无法计算（例如草稿与证据池全空）：拒绝用占位分冒充。"""

    code = "review_scoring_unavailable"


@dataclass(slots=True)
class RubricPart:
    part: str
    raw: float | None
    weight: float
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.part,
            "raw": None if self.raw is None else round(float(self.raw), 4),
            "weight": self.weight,
            "note": self.note,
        }


@dataclass(slots=True)
class DimensionScore:
    dimension: str
    score: float
    parts: list[RubricPart] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "score": self.score,
            "parts": [part.to_dict() for part in self.parts],
            "notes": list(self.notes),
        }


def _ratio(numerator: float, denominator: float, *, default: float | None = None) -> float | None:
    if denominator <= 0:
        return default
    return max(0.0, min(1.0, float(numerator) / float(denominator)))


def _weighted(parts: Sequence[RubricPart], *, dimension: str) -> DimensionScore:
    """按剩余权重归一（缺失分项不参与，记 note）。"""
    available = [part for part in parts if part.raw is not None and part.weight > 0]
    notes: list[str] = []
    missing = [part.part for part in parts if part.raw is None]
    if missing:
        notes.append("分项缺失，按剩余权重归一（禁止编造数值）：" + ", ".join(missing))
    total_weight = sum(part.weight for part in available)
    if not available or total_weight <= 0:
        raise ScoringUnavailable(f"{dimension} 无任何可用分项（禁止占位打分）")
    raw = sum(float(part.raw) * part.weight for part in available if part.raw is not None)
    score = round(DIM_MAX * raw / total_weight, 2)
    for part in parts:
        if part.raw is None and not part.note:
            part.note = "缺失：不参与计算"
    return DimensionScore(dimension=dimension, score=score, parts=list(parts), notes=notes)


def score_novelty(inputs: Mapping[str, Any]) -> DimensionScore:
    plan = inputs.get("plan_review") if isinstance(inputs.get("plan_review"), Mapping) else None
    novelty_raw: float | None = None
    note: str | None = None
    if plan:
        value = plan.get("novelty")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            novelty_raw = max(0.0, min(1.0, float(value) / 20.0))
            note = f"上游 plan_review 新颖性 {float(value)}/20（盲评产出，非本环节打分）"
        else:
            scores = plan.get("scores")
            if isinstance(scores, Sequence) and scores and isinstance(scores[0], Mapping):
                first = scores[0].get("novelty")
                if isinstance(first, (int, float)) and not isinstance(first, bool):
                    novelty_raw = max(0.0, min(1.0, float(first) / 20.0))
                    note = f"上游 plan_review 候选 A 新颖性 {float(first)}/20"
    if novelty_raw is None:
        note = "上游无 plan_review 新颖性分项：该项缺失并按剩余权重归一"

    breadth = _ratio(float(inputs.get("distinct_span_papers") or 0), 5.0, default=0.0)
    gaps = inputs.get("gap_count")
    gap_raw = (1.0 if float(gaps) > 0 else 0.35) if isinstance(gaps, (int, float)) else None
    parts = [
        RubricPart("plan_review_novelty", novelty_raw, 0.60, note),
        RubricPart(
            "evidence_breadth",
            breadth,
            0.25,
            f"可引用原文片段的论文数={inputs.get('distinct_span_papers')}（5 篇封顶）",
        ),
        RubricPart(
            "gap_signal",
            gap_raw,
            0.15,
            f"文献空白数={gaps}" if isinstance(gaps, (int, float)) else "未取到 gaps 产出",
        ),
    ]
    return _weighted(parts, dimension="novelty")


def score_rigor(inputs: Mapping[str, Any]) -> DimensionScore:
    counts = inputs.get("claim_counts") if isinstance(inputs.get("claim_counts"), Mapping) else {}
    factual = float(counts.get("factual") or 0)
    contradicted = float(counts.get("contradicted") or 0)
    coverage = inputs.get("claim_coverage")
    coverage_raw = float(coverage) if isinstance(coverage, (int, float)) else None
    contradiction_raw = (
        1.0 - _ratio(contradicted, factual, default=0.0) if factual > 0 else None
    )
    passport_raw = _passport_factor(inputs.get("passport"))
    bound = float(inputs.get("bound_evidence_count") or 0)
    density_raw = _ratio(bound, factual, default=None) if factual > 0 else None
    parts = [
        RubricPart(
            "claim_coverage",
            coverage_raw,
            0.45,
            None if coverage_raw is not None else "草稿无事实性 Claim：覆盖率无定义",
        ),
        RubricPart(
            "contradiction_free",
            contradiction_raw,
            0.20,
            f"矛盾 Claim {int(contradicted)}/{int(factual)}",
        ),
        RubricPart("passport_strength", passport_raw, 0.20, _passport_note(inputs.get("passport"))),
        RubricPart(
            "bound_evidence_density",
            density_raw,
            0.15,
            f"已绑定证据 {int(bound)} 条 / 事实性 Claim {int(factual)} 条",
        ),
    ]
    return _weighted(parts, dimension="rigor")


def score_completeness(inputs: Mapping[str, Any]) -> DimensionScore:
    coverage = inputs.get("claim_coverage")
    coverage_raw = float(coverage) if isinstance(coverage, (int, float)) else None
    sections = float(inputs.get("section_count") or 0)
    section_raw = _ratio(sections, 6.0, default=0.0)
    pool_size = float(inputs.get("evidence_pool_size") or 0)
    evidence_raw = _ratio(pool_size, 12.0, default=0.0)
    parts = [
        RubricPart("claim_coverage", coverage_raw, 0.45, "supported / 事实性 Claim"),
        RubricPart("section_coverage", section_raw, 0.25, f"章节数={int(sections)}（6 节封顶）"),
        RubricPart("evidence_pool_size", evidence_raw, 0.30, f"证据池条目={int(pool_size)}（12 条封顶）"),
    ]
    return _weighted(parts, dimension="completeness")


def score_reproducibility(inputs: Mapping[str, Any]) -> DimensionScore:
    passport = inputs.get("passport") if isinstance(inputs.get("passport"), Mapping) else None
    passport_raw = _passport_factor(passport)
    metrics_count = float(inputs.get("metric_count") or 0)
    metrics_raw = _ratio(metrics_count, 3.0, default=0.0)
    is_replay = bool((passport or {}).get("is_replay"))
    realtime_raw = 0.6 if is_replay else 1.0 if passport else None
    provenance_present = 0
    if passport:
        provenance_present = sum(1 for field_name in PROVENANCE_FIELDS if str(passport.get(field_name) or "").strip())
    provenance_raw = _ratio(provenance_present, len(PROVENANCE_FIELDS), default=None) if passport else None
    parts = [
        RubricPart("passport_status", passport_raw, 0.40, _passport_note(passport)),
        RubricPart("metric_coverage", metrics_raw, 0.25, f"真实落库指标={int(metrics_count)}（3 条封顶）"),
        RubricPart(
            "realtime_execution",
            realtime_raw,
            0.20,
            "回放结果（is_replay=true）按 0.6 计，如实低于实时执行" if is_replay else None,
        ),
        RubricPart(
            "provenance_hashes",
            provenance_raw,
            0.15,
            None
            if provenance_raw is None
            else f"溯源字段齐备 {provenance_present}/{len(PROVENANCE_FIELDS)}",
        ),
    ]
    return _weighted(parts, dimension="reproducibility")


def _passport_factor(passport: Mapping[str, Any] | None) -> float | None:
    if not passport:
        return PASSPORT_FACTORS["missing"]
    status = str(passport.get("status") or "").strip() or "missing"
    return PASSPORT_FACTORS.get(status, 0.5)


def _passport_note(passport: Mapping[str, Any] | None) -> str:
    if not passport:
        return "无 Passport（WP11 未产出或本条流水线未覆盖）：按 0.5 计并如实披露"
    status = str(passport.get("status") or "missing")
    replay = "回放" if passport.get("is_replay") else "实时"
    return f"Passport #{passport.get('passport_id')} status={status} 类型={replay}"


def compute_scores(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """计算四维与总分（服务端权威）。"""
    dimensions = [
        score_novelty(inputs),
        score_rigor(inputs),
        score_completeness(inputs),
        score_reproducibility(inputs),
    ]
    result: dict[str, Any] = {item.dimension: item.score for item in dimensions}
    total = round(sum(item.score for item in dimensions), 2)
    result["total"] = total
    result["rubric"] = {
        "version": RUBRIC_VERSION,
        "dimension_max": DIM_MAX,
        "dimensions": [item.to_dict() for item in dimensions],
        "total_rule": "total = novelty + rigor + completeness + reproducibility（服务端重算）",
        "inputs": {
            key: (
                dict(value)
                if isinstance(value, Mapping)
                else list(value)
                if isinstance(value, list)
                else value
            )
            for key, value in inputs.items()
        },
        "score_coverage": _score_coverage(inputs),
    }
    result["score_coverage"] = _score_coverage(inputs)
    return result


def _score_coverage(inputs: Mapping[str, Any]) -> float:
    """已获得真实输入的分项占比（透明披露，不参与打分）。"""
    signals = (
        inputs.get("claim_coverage") is not None,
        bool(inputs.get("section_count")),
        bool(inputs.get("evidence_pool_size")),
        bool(inputs.get("plan_review")),
        bool(inputs.get("passport")),
        bool(inputs.get("metric_count")),
    )
    return round(sum(1 for flag in signals if flag) / float(len(signals)), 3)


# --------------------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------------------
def validate_dimensions(payload: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    """校验外部（模型）返回的四维：越界/缺失/自报 total 不一致都会被指出。"""
    issues: list[str] = []
    scores: dict[str, float] = {}
    for dimension in DIMENSIONS:
        value = (payload or {}).get(dimension)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            issues.append(f"{dimension} 缺失或非数值：{value!r}")
            continue
        number = float(value)
        if number < 0 or number > DIM_MAX:
            issues.append(f"{dimension}={number} 超出 0–{DIM_MAX:.0f}")
            continue
        scores[dimension] = round(number, 2)
    if len(scores) == len(DIMENSIONS):
        reported = (payload or {}).get("total")
        if isinstance(reported, (int, float)) and not isinstance(reported, bool):
            expected = round(sum(scores.values()), 2)
            if abs(float(reported) - expected) > 1e-6:
                issues.append(f"模型自报 total={reported} 与四维之和 {expected} 不一致（以四维之和为准）")
    return scores, issues


def rule_based_comments(inputs: Mapping[str, Any], scores: Mapping[str, float]) -> list[dict[str, str]]:
    """基于真实信号的评语（LLM 不可用时的兜底；不编造事实）。"""
    comments: list[dict[str, str]] = []
    counts = inputs.get("claim_counts") if isinstance(inputs.get("claim_counts"), Mapping) else {}
    factual = int(counts.get("factual") or 0)
    insufficient = int(counts.get("insufficient") or 0)
    contradicted = int(counts.get("contradicted") or 0)
    coverage = inputs.get("claim_coverage")

    if factual == 0:
        comments.append(
            {
                "dimension": "completeness",
                "issue": "草稿中没有可核验的事实性 Claim，覆盖率无定义",
                "suggestion": "补充结论性句子的证据挂载后再评估完整度",
            }
        )
    elif isinstance(coverage, (int, float)) and float(coverage) < 0.8:
        comments.append(
            {
                "dimension": "rigor",
                "issue": f"claim_coverage={coverage}，仍有 {insufficient} 条事实性 Claim 无充分证据",
                "suggestion": "对 insufficient 段落补充全文解析或实验指标证据，或在草稿中如实标注为待验证",
            }
        )
    if contradicted > 0:
        comments.append(
            {
                "dimension": "rigor",
                "issue": f"{contradicted} 条 Claim 的引证未通过校验（与实际证据冲突）",
                "suggestion": "核对该句结论与证据原文，修正表述或替换证据后再评估",
            }
        )
    passport = inputs.get("passport") if isinstance(inputs.get("passport"), Mapping) else None
    if not passport:
        comments.append(
            {
                "dimension": "reproducibility",
                "issue": "本流水线尚未产出 Passport，无法核对数据/模型/提示词溯源哈希",
                "suggestion": "补齐 Passport（含 dataset_sha256 / prompt_sha256 / code_commit_sha）后重跑评审",
            }
        )
    elif str(passport.get("status")) != "complete":
        comments.append(
            {
                "dimension": "reproducibility",
                "issue": f"Passport 状态为 {passport.get('status')}，关键字段不完整",
                "suggestion": "补全 Passport 必需字段后再声称可复现（status 规则：缺字段即 incomplete）",
            }
        )
    if not comments:
        comments.append(
            {
                "dimension": "completeness",
                "issue": f"四维得分 {scores}，未发现需要阻断的问题",
                "suggestion": "继续保持证据挂载密度，并在下一轮迭代中扩大证据池覆盖",
            }
        )
    return comments


__all__ = [
    "DIMENSIONS",
    "DIM_MAX",
    "PASSPORT_FACTORS",
    "PROVENANCE_FIELDS",
    "RUBRIC_VERSION",
    "DimensionScore",
    "RubricPart",
    "ScoringUnavailable",
    "compute_scores",
    "rule_based_comments",
    "score_completeness",
    "score_novelty",
    "score_reproducibility",
    "score_rigor",
    "validate_dimensions",
]
