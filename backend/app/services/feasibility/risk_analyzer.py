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
"""风险清单（WP08-T5，附录 A.4 ``feasibilities.risk_list``）。

输出 ``[{risk, level, mitigation}]``，``level ∈ {low, medium, high}``。

**无黑箱**：每条风险的 ``level`` 都由一条显式规则从四维分或真实信号推出，
规则与触发值同时写入 ``level_basis`` / ``trigger``，可逐条复核；
未触发的规则也会出现在 ``evaluated_rules`` 中（说明"检查过且未触发"），
避免"只报坏消息"的假象。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger("sciloop.wp08.risk")

LEVEL_LOW = "low"
LEVEL_MEDIUM = "medium"
LEVEL_HIGH = "high"
LEVELS: tuple[str, ...] = (LEVEL_LOW, LEVEL_MEDIUM, LEVEL_HIGH)

#: 风险等级阈值（规则层的唯一判据，不可被 LLM 覆盖）
THRESHOLDS = {
    "data_availability_high": 50,
    "data_availability_medium": 65,
    "compute_cost_high": 40,
    "compute_cost_medium": 60,
    "method_maturity_high": 35,
    "method_maturity_medium": 55,
    "novelty_gap_high": 25,
    "novelty_gap_medium": 45,
}


def _dim_scores(dimensions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {str(dim["key"]): int(dim["score"]) for dim in dimensions}


def analyze(
    dimensions: Sequence[Mapping[str, Any]],
    *,
    bundle: Mapping[str, Any] | None = None,
    mve_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """由四维分与真实信号推出风险清单（纯函数）。"""
    scores = _dim_scores(dimensions)
    data = scores.get("data_availability", 0)
    compute = scores.get("compute_cost", 0)
    maturity = scores.get("method_maturity", 0)
    novelty = scores.get("novelty_gap", 0)

    risks: list[dict[str, Any]] = []
    evaluated: list[dict[str, Any]] = []

    def add_rule(
        key: str,
        condition: bool,
        *,
        risk: str,
        level: str,
        mitigation: str,
        basis: str,
        trigger: Any,
    ) -> None:
        evaluated.append(
            {
                "rule": key,
                "triggered": bool(condition),
                "level_if_triggered": level,
                "basis": basis,
                "observed": trigger,
            }
        )
        if condition:
            risks.append(
                {
                    "key": key,
                    "risk": risk,
                    "level": level,
                    "mitigation": mitigation,
                    "level_basis": basis,
                    "trigger": trigger,
                }
            )

    add_rule(
        "data_availability_low",
        data < THRESHOLDS["data_availability_high"],
        risk="目标数据集可得性不足：卡片结构化字段未提供数据集，只有原文候选，尚未确认能否获取",
        level=LEVEL_HIGH,
        mitigation=(
            "锁定任务书前由研究者确认至少 1 个可用数据集（可从 data_availability.signals."
            "dataset_candidates 中挑选并核验原文）；确认不了则先降级为「小样本自建数据集」或改用"
            "现有论文公开的实验设置复现"
        ),
        basis=f"data_availability={data} < {THRESHOLDS['data_availability_high']}",
        trigger={"data_availability": data},
    )
    add_rule(
        "data_availability_medium",
        THRESHOLDS["data_availability_high"] <= data < THRESHOLDS["data_availability_medium"],
        risk="数据集仅为候选或有论文未过全文闸门，跨论文可比性可能受限",
        level=LEVEL_MEDIUM,
        mitigation="先固定一个数据集做纵向对比，跨数据集结论延后；并在产出物标注覆盖范围（CoverageTag）",
        basis=(
            f"{THRESHOLDS['data_availability_high']} <= data_availability={data} "
            f"< {THRESHOLDS['data_availability_medium']}"
        ),
        trigger={"data_availability": data},
    )
    add_rule(
        "compute_cost_over_budget",
        compute < THRESHOLDS["compute_cost_high"],
        risk="算力/预算紧张：预估成本接近或超过成本护栏，可能触发熔断",
        level=LEVEL_HIGH,
        mitigation=(
            "缩减 sample_size 与轮次（降低 max_iterations / max_retry），或先跑 T1_prompt_variant "
            "最小对照；硬护栏 8.0 USD 与演示配额 3.0 USD 双线在 UI 常驻显示"
        ),
        basis=f"compute_cost={compute} < {THRESHOLDS['compute_cost_high']}",
        trigger={"compute_cost": compute},
    )
    add_rule(
        "compute_cost_tight",
        THRESHOLDS["compute_cost_high"] <= compute < THRESHOLDS["compute_cost_medium"],
        risk="预算余量偏小：轮次放大后可能逼近护栏",
        level=LEVEL_MEDIUM,
        mitigation="按 guardrail 事件监控累计成本，接近配额 3.0 USD 时只告警不熔断，人工决定是否继续",
        basis=(
            f"{THRESHOLDS['compute_cost_high']} <= compute_cost={compute} "
            f"< {THRESHOLDS['compute_cost_medium']}"
        ),
        trigger={"compute_cost": compute},
    )
    add_rule(
        "method_maturity_low",
        maturity < THRESHOLDS["method_maturity_high"],
        risk="方法成熟度低：聚合内几无可借鉴的相近做法，方案落地路径不明确",
        level=LEVEL_HIGH,
        mitigation="先做一次文献补检或把机制降级为 refinement（在已有方法上做小改动），并让 plan_review 盲评把关",
        basis=f"method_maturity={maturity} < {THRESHOLDS['method_maturity_high']}",
        trigger={"method_maturity": maturity},
    )
    add_rule(
        "method_maturity_medium",
        THRESHOLDS["method_maturity_high"] <= maturity < THRESHOLDS["method_maturity_medium"],
        risk="方法成熟度中等：需要更多工程实现与调参",
        level=LEVEL_MEDIUM,
        mitigation="以 T3_model_compare 做稳健性对照，先确认方法在固定数据集上可复现再放大",
        basis=(
            f"{THRESHOLDS['method_maturity_high']} <= method_maturity={maturity} "
            f"< {THRESHOLDS['method_maturity_medium']}"
        ),
        trigger={"method_maturity": maturity},
    )
    add_rule(
        "novelty_gap_low",
        novelty < THRESHOLDS["novelty_gap_high"],
        risk="与已有工作差异度过低：idea 可能只是已有方法的改写，新颖性不足",
        level=LEVEL_HIGH,
        mitigation="换 Gap 或换机制（combination / transfer），并让盲评给出 novelty 维度的对照分",
        basis=f"novelty_gap={novelty} < {THRESHOLDS['novelty_gap_high']}",
        trigger={"novelty_gap": novelty},
    )
    add_rule(
        "novelty_gap_medium",
        THRESHOLDS["novelty_gap_high"] <= novelty < THRESHOLDS["novelty_gap_medium"],
        risk="差异度偏低：需在写作阶段明确与已有工作的边界",
        level=LEVEL_MEDIUM,
        mitigation="在任务书的交付物中强制包含「与已有工作对照表」，逐条标注差异点与证据",
        basis=(
            f"{THRESHOLDS['novelty_gap_high']} <= novelty_gap={novelty} "
            f"< {THRESHOLDS['novelty_gap_medium']}"
        ),
        trigger={"novelty_gap": novelty},
    )

    abstract_only = list((bundle or {}).get("abstract_only_paper_ids") or [])
    add_rule(
        "evidence_scope_abstract_only",
        bool(abstract_only),
        risk="部分论文未通过全文闸门：相关证据仅为摘要级，无法逐段核验",
        level=LEVEL_MEDIUM,
        mitigation="UI 用 CoverageTag 明示「证据覆盖范围：仅摘要」；这些论文的结论不用于支撑强断言",
        basis="abstract_only_paper_ids 非空（fulltext_gate: parse_status='ok' 且 coverage>=0.60 才允许正文级证据）",
        trigger={"abstract_only_paper_ids": abstract_only},
    )

    unsupported = list((mve_plan or {}).get("unsupported_assumptions") or [])
    add_rule(
        "mve_unsupported_assumption",
        bool(unsupported),
        risk="最小可行实验存在未确认假设（如数据集需人工选定），直接照做可能返工",
        level=LEVEL_LOW,
        mitigation="按 mve_plan.human_review_checklist 逐项确认后再启动流水线",
        basis="mve_plan.unsupported_assumptions 非空",
        trigger={"unsupported_assumptions": unsupported},
    )

    order = {LEVEL_HIGH: 0, LEVEL_MEDIUM: 1, LEVEL_LOW: 2}
    risks.sort(key=lambda item: order.get(item["level"], 3))
    counts = {level: sum(1 for item in risks if item["level"] == level) for level in LEVELS}
    return {
        "risk_list": risks,
        "risk_count": len(risks),
        "level_counts": counts,
        "highest_level": risks[0]["level"] if risks else None,
        "evaluated_rules": evaluated,
        "rule_count": len(evaluated),
        "thresholds": dict(THRESHOLDS),
        "policy_note": (
            "本清单只做风险提示，不代替 WP10 的三动作风险策略（auto_execute / need_human / "
            "circuit_break）；熔断判定以硬护栏与 decision_logs 为准"
        ),
    }


__all__ = ["LEVELS", "LEVEL_HIGH", "LEVEL_LOW", "LEVEL_MEDIUM", "THRESHOLDS", "analyze"]
