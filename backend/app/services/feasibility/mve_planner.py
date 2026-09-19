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
"""最小可行实验（MVE）建议（WP08-T5，附录 A.4 ``feasibilities.mve_plan``）。

目标：让一个研究生拿着这份 plan **照着做就能跑一次最小实验**（WP08-A6）。
因此 ``steps`` 里每一步都给出**真实存在的端点 / 具体动作 / 期望产出**，
而不是"设计实验方案"这类空话。

诚实性约束
----------
- 模板只能从 contracts.enums.template_p0 = ``T1_prompt_variant`` / ``T3_model_compare``
  里选（P0 范围），选择理由写入 ``template_rationale``。
- **成本不做估算**：本机没有可用计价时 ``expected_cost_usd = null`` 并写明口径
  （以 ``llm_call_logs`` 累计为唯一权威，禁止编造单价）；成本维度据此按保守口径计分。
- 数据集：优先用从**真实 span** 抽出的候选（带 span id 与原文），并标
  ``confirmed=false``；材料没有就写 ``null`` 并说明，不编造数据集名。
- 耗时用 contracts 的**护栏上限**（run 300s / stage 1200s）作为上界，标注
  ``duration_source``，不冒充实测预测。
- ``sample_size <= 50`` 为契约硬上限，超出会在 guardrail 说明中报错。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger("sciloop.wp08.mve")

#: P0 模板白名单（contracts.enums.template_p0）
TEMPLATE_P0: tuple[str, ...] = ("T1_prompt_variant", "T3_model_compare")

#: 契约护栏（contracts.guardrails）
RUN_TIMEOUT_SECONDS = 300
STAGE_TIMEOUT_SECONDS = 1200
SAMPLE_SIZE_LIMIT = 50
MAX_LLM_COST_USD = 8.0
DEMO_COST_QUOTA_USD = 3.0

#: 默认轮次配置（与附录 A.4 默认值一致；任务书可覆盖）
DEFAULT_ITERATIONS = {"max_iterations": 3, "score_threshold": 80, "marginal_gain_threshold": 2, "max_retry": 2}


def choose_template(mechanism: str | None, dimensions: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """按机制与四维分选一个 P0 模板，并给出可复核的理由。"""
    scores = {str(dim["key"]): int(dim["score"]) for dim in dimensions}
    maturity = scores.get("method_maturity", 0)
    data = scores.get("data_availability", 0)
    if mechanism in ("refinement", "combination") and maturity >= 50:
        return (
            "T1_prompt_variant",
            f"机制={mechanism} 且方法成熟度 {maturity}>=50：聚合内已有相近做法，"
            "先用 T1_prompt_variant 做提示词变体对照，成本最低、最快验证方向是否成立",
        )
    if mechanism == "transfer" or data < 65:
        return (
            "T3_model_compare",
            f"机制={mechanism}、数据可得性 {data}：数据集需人工确认，先用 T3_model_compare "
            "在两个模型上做同设定对照，验证结论不依赖单一模型，再决定是否投入更大样本",
        )
    return (
        "T3_model_compare",
        "默认选 T3_model_compare：改动面小、结论稳健性可核验（P0 模板白名单内）",
    )


def _dataset_choice(bundle: Mapping[str, Any]) -> dict[str, Any]:
    mentions = list(bundle.get("dataset_mentions") or [])
    if mentions:
        top = mentions[0]
        return {
            "name": top["name"],
            "confirmed": False,
            "needs_confirmation": True,
            "source": "paper_span_regex",
            "paper_id": top.get("paper_id"),
            "paper_span_id": top.get("paper_span_id"),
            "document_version": top.get("document_version"),
            "section_name": top.get("section_name"),
            "quote_text": top.get("quote_text"),
            "jump_url": top.get("jump_url"),
            "candidates": [item["name"] for item in mentions],
            "note": (
                "该名称来自真实原文段落，但**尚未确认可获取**；"
                "锁定任务书前请按 human_review_checklist 第 1 条核验"
            ),
        }
    return {
        "name": None,
        "confirmed": False,
        "needs_confirmation": True,
        "source": "unavailable",
        "candidates": [],
        "note": (
            "卡片结构化字段 datasets 全为占位值 unknown，且原文未抽出 "
            "『Xxx dataset/benchmark/corpus』形式的候选：**材料未提供数据集信息，不编造**；"
            "请研究者指定一个可用数据集后重跑可行性"
        ),
    }


def _metrics(bundle: Mapping[str, Any]) -> dict[str, Any]:
    metrics = list(bundle.get("metrics") or [])
    if metrics:
        return {
            "values": metrics,
            "source": "paper_cards.experimental_setup.metrics（真实取值）",
            "missing": False,
        }
    return {
        "values": [],
        "source": "unavailable",
        "missing": True,
        "note": "卡片未提供评价指标，需研究者在任务书中指定（不编造指标名）",
    }


def _steps(
    *,
    project_id: int | None,
    template_id: str,
    dataset: Mapping[str, Any],
    metrics: Sequence[str],
    sample_size: int,
) -> list[dict[str, Any]]:
    """可照做的步骤列表（端点全部来自 contracts.api_contract）。"""
    project_ref = project_id if project_id is not None else "<project_id>"
    return [
        {
            "step": 1,
            "action": "确认数据集与指标（人工，必须做）",
            "detail": (
                f"数据集候选：{dataset.get('candidates') or '材料未提供'}；"
                f"指标候选：{list(metrics) or '材料未提供'}。"
                "确认数据可公开获取后填入任务书 target_datasets"
            ),
            "endpoint": None,
            "expected_output": "taskbook.target_datasets / metrics 填好并保存",
            "duration_minutes": 0,
            "duration_source": "human_estimate_not_recorded",
        },
        {
            "step": 2,
            "action": "创建并锁定任务书",
            "detail": "任务书锁定后是流水线唯一执行依据，锁定后只读",
            "endpoint": "POST /api/v1/taskbooks  →  PATCH /api/v1/taskbooks/{id}  →  POST /api/v1/taskbooks/{id}/lock",
            "expected_output": "taskbook.status = locked；再 PATCH 返回 409",
            "duration_minutes": 3,
            "duration_source": "human_estimate",
        },
        {
            "step": 3,
            "action": "创建流水线（manual 模式先跑一遍）",
            "detail": "manual 模式下每个决策点（D1–D6）停下等人确认，便于观察环境是否就绪",
            "endpoint": f"POST /api/v1/pipelines {{\"project_id\": {project_ref}}}",
            "expected_output": "pipeline_runs 行 status=running",
            "duration_minutes": 1,
            "duration_source": "human_estimate",
        },
        {
            "step": 4,
            "action": "跑实验环节（template 指定为 " + template_id + "）",
            "detail": (
                f"sample_size={sample_size}（契约上限 {SAMPLE_SIZE_LIMIT}）；"
                "模板 ID 必须在白名单内，参数走 schema 校验"
            ),
            "endpoint": f"POST /api/v1/pipelines/{project_ref}/run?mode=manual",
            "expected_output": "stage_outputs.experiment 落库；experiment_runs 行生成",
            "duration_minutes": STAGE_TIMEOUT_SECONDS // 60,
            "duration_source": "contracts.guardrails.time.stage_timeout_seconds（上界，非实测预测）",
        },
        {
            "step": 5,
            "action": "读指标与 Passport",
            "detail": "确认指标真实落库、Passport 关键字段完整（缺失即 status=incomplete，禁止宣称可复现）",
            "endpoint": (
                "GET /api/v1/experiments/{id}/runs  →  "
                "GET /api/v1/experiments/runs/{run_id}/metrics  →  "
                "GET /api/v1/experiments/runs/{run_id}/passport"
            ),
            "expected_output": "experiment_metrics + experiment_passports 有真实行",
            "duration_minutes": 2,
            "duration_source": "human_estimate",
        },
        {
            "step": 6,
            "action": "可复现性回放（最小对照）",
            "detail": "回放必须标 is_replay=true，禁止冒充实时结果；rerun 会生成子 Passport 与差异报告",
            "endpoint": "POST /api/v1/passports/{id}/replay",
            "expected_output": "子 Passport + 差异报告（is_replay=true）",
            "duration_minutes": RUN_TIMEOUT_SECONDS // 60,
            "duration_source": "contracts.guardrails.time.run_timeout_seconds（上界，非实测预测）",
        },
        {
            "step": 7,
            "action": "核对迭代判据并决定是否继续",
            "detail": "按 score_threshold / marginal_gain_threshold / max_iterations / max_retry 判定 continue / adjust / stop",
            "endpoint": f"GET /api/v1/pipelines/{project_ref}/status",
            "expected_output": "stop_reason 明确落库（score_threshold|marginal_stagnation|max_iterations|manual）",
            "duration_minutes": 2,
            "duration_source": "human_estimate",
        },
    ]


def build_mve_plan(
    *,
    idea: Mapping[str, Any],
    bundle: Mapping[str, Any],
    dimensions: Sequence[Mapping[str, Any]],
    project_id: int | None = None,
    sample_size: int = 20,
    rounds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """生成可执行的 MVE 计划（纯函数）。"""
    mechanism = idea.get("mechanism")
    template_id, template_rationale = choose_template(
        str(mechanism) if mechanism else None, dimensions
    )
    dataset = _dataset_choice(bundle)
    metrics = _metrics(bundle)
    sample = max(1, min(int(sample_size), SAMPLE_SIZE_LIMIT))
    sample_clamped = int(sample_size) > SAMPLE_SIZE_LIMIT

    unsupported: list[dict[str, str]] = []
    if not dataset.get("name"):
        unsupported.append(
            {
                "assumption": "存在可用且可获取的目标数据集",
                "status": "unconfirmed",
                "reason": str(dataset.get("note")),
            }
        )
    else:
        unsupported.append(
            {
                "assumption": f"数据集 {dataset['name']} 可公开获取",
                "status": "unconfirmed",
                "reason": "名称来自原文候选，尚未核验下载与许可",
            }
        )
    if metrics.get("missing"):
        unsupported.append(
            {
                "assumption": "评价指标可由卡片给出",
                "status": "unconfirmed",
                "reason": str(metrics.get("note")),
            }
        )

    steps = _steps(
        project_id=project_id,
        template_id=template_id,
        dataset=dataset,
        metrics=metrics.get("values") or [],
        sample_size=sample,
    )
    total_minutes = sum(int(step["duration_minutes"]) for step in steps)

    round_cfg = {**DEFAULT_ITERATIONS, **dict(rounds or {})}
    guardrail_notes = [
        f"成本双线：硬护栏 {MAX_LLM_COST_USD} USD 熔断 / 演示配额 {DEMO_COST_QUOTA_USD} USD 只告警",
        f"sample_size <= {SAMPLE_SIZE_LIMIT}（超出即拒绝执行）",
        f"时间护栏：单次 run {RUN_TIMEOUT_SECONDS}s，单环节 {STAGE_TIMEOUT_SECONDS}s（层级差不得相等）",
        "执行器仅模板注册表可执行：模板 ID 白名单 + 参数 schema 校验；禁止任意 shell/eval/exec；禁止运行时装包",
        "出网仅白名单 LLM 域名，禁止访问内网与宿主文件系统路径",
    ]
    if sample_clamped:
        guardrail_notes.append(
            f"注意：请求的 sample_size={int(sample_size)} 超过契约上限，已钳制为 {sample}"
        )

    checklist = [
        {
            "item": "确认目标数据集可获取（许可 / 下载方式 / 版本号）",
            "why": "数据集是 data_availability 分与整个实验的前提，未确认前不要启动流水线",
            "blocking": True,
        },
        {
            "item": f"确认评价指标与 baseline 是否与提出者论文一致（候选指标：{metrics.get('values') or '材料未提供'}）",
            "why": "指标不一致会让结论无法与原文对照",
            "blocking": True,
        },
        {
            "item": f"确认模板 {template_id} 与 sample_size={sample} 的成本是否在演示配额 {DEMO_COST_QUOTA_USD} USD 内",
            "why": "配额超限只告警但会削弱演示说服力",
            "blocking": False,
        },
        {
            "item": "确认 idea 绑定的证据足够支撑任务书的研究问题（打开证据抽屉逐条核验原文）",
            "why": "无证据的断言不允许进入产出物",
            "blocking": True,
        },
    ]

    return {
        "objective": (
            f"用最小成本验证 idea「{idea.get('title')}」所依赖的核心机制是否成立："
            f"机制={mechanism or '未标注'}，在 {dataset.get('name') or '待选定数据集'} 上，"
            f"以 {len(metrics.get('values') or []) or 0} 个真实指标为判据"
        ),
        "template_id": template_id,
        "template_rationale": template_rationale,
        "template_whitelist": list(TEMPLATE_P0),
        "dataset": dataset,
        "metrics": metrics,
        "baselines": {
            "values": list(bundle.get("baselines") or []),
            "source": "paper_cards.experimental_setup.baselines",
            "note": (
                None
                if bundle.get("baselines")
                else "卡片未提供 baseline（占位值 unknown 视为未提供），需研究者指定"
            ),
        },
        "sample_size": sample,
        "sample_size_limit": SAMPLE_SIZE_LIMIT,
        "steps": steps,
        "step_count": len(steps),
        "expected_duration_minutes": total_minutes,
        "duration_note": (
            "总时长为各步骤之和；其中涉及护栏上限的步骤取 contracts 给定上界，"
            "人工步骤为粗略工作量估计（duration_source 逐条标注），非实测预测"
        ),
        "expected_cost_usd": None,
        "cost_note": (
            "本机无可用单价时不估算成本（禁止编造）：唯一权威口径是 llm_call_logs 累计值，"
            f"上限由护栏给出（硬线 {MAX_LLM_COST_USD} USD / 演示配额 {DEMO_COST_QUOTA_USD} USD）"
        ),
        "rounds": round_cfg,
        "stop_rules": {
            "max_iterations": round_cfg["max_iterations"],
            "score_threshold": round_cfg["score_threshold"],
            "marginal_gain_threshold": round_cfg["marginal_gain_threshold"],
            "max_retry": round_cfg["max_retry"],
        },
        "success_criteria": [
            f"指标可复算：{metrics.get('values') or '（待指定）'} 在同设定下两次运行差异在噪声范围内",
            "Passport 关键字段完整（status != incomplete），回放标 is_replay=true 且有差异报告",
            "结论只引用已绑定证据；无证据的断言不得写入产出物",
        ],
        "guardrail_notes": guardrail_notes,
        "human_review_checklist": checklist,
        "unsupported_assumptions": unsupported,
        "readiness": "ready_with_confirmation" if dataset.get("name") else "blocked_on_dataset",
    }


__all__ = [
    "DEFAULT_ITERATIONS",
    "DEMO_COST_QUOTA_USD",
    "MAX_LLM_COST_USD",
    "RUN_TIMEOUT_SECONDS",
    "SAMPLE_SIZE_LIMIT",
    "STAGE_TIMEOUT_SECONDS",
    "TEMPLATE_P0",
    "build_mve_plan",
    "choose_template",
]
