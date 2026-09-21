# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""流水线、决策与实验域契约：``pipeline_runs`` / ``stage_outputs`` /
``experiments`` / ``experiment_runs`` / ``experiment_metrics`` /
``decision_logs`` / ``interventions`` / ``experiment_passports``。

字段来源：附录 A.5 / A.6 + 附录 B.4。

三条不可退让的口径
------------------

1. **决策留痕必须完整**：每次决策写全 ``context_digest / options_considered /
   chosen / rationale / risk_score / confidence_score / reversibility_score /
   policy_action / policy_version / guardrail_checks / cost_usd``
   （contracts.risk_policy_rules.persistence）。
2. **Passport 不可变**：创建后禁止 UPDATE，重跑生成新记录并用
   ``parent_passport_id`` 关联；``is_replay`` 必须如实标记，禁止把回放冒充实时
   （contracts.passport_rules）。
3. **阶段幂等**：``stage_outputs`` 唯一键为 ``(pipeline_run_id, stage, attempt)``，
   重复提交走 ``ON CONFLICT DO UPDATE``，不得出现两条 ``running``。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from schemas.base import SciLoopModel
from schemas.enums import (
    DecisionPoint,
    InterventionAction,
    InterventionNode,
    PassportStatus,
    PipelineStage,
    PolicyAction,
    ProjectMode,
    StageStatus,
    StopReason,
    TemplateId,
)

__all__ = [
    "DecisionLog",
    "Experiment",
    "ExperimentMetric",
    "ExperimentPassport",
    "ExperimentRun",
    "GuardrailChecks",
    "InterveneRequest",
    "Intervention",
    "PipelineCreateRequest",
    "PipelineRun",
    "PipelineRunRequest",
    "StageOutput",
]


# --------------------------------------------------------------------------- #
# 流水线
# --------------------------------------------------------------------------- #
class PipelineRun(SciLoopModel):
    """``pipeline_runs``：一轮完整流水线（迭代轮次 + 成本 + 停止原因）。"""

    id: int | None = None
    project_id: int
    iteration: int = Field(ge=1)
    mode: ProjectMode
    status: str = Field(description="running | paused | failed | circuit_break | done")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total_cost_usd: float = 0.0
    stop_reason: StopReason | None = Field(
        default=None, description="**必须显著展示**，禁止只给『已结束』"
    )
    created_at: datetime | None = None


class StageOutput(SciLoopModel):
    """``stage_outputs``：六环节产出（``uq_stage_once`` 保证幂等）。"""

    id: int | None = None
    pipeline_run_id: int
    stage: PipelineStage
    status: StageStatus
    output_json: dict[str, Any] | None = None
    output_text: str | None = None
    attempt: int = Field(default=1, description="plan_review 判 revise 回退 plan 时的次数")
    cost_usd: float = 0.0
    duration_ms: int | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class GuardrailChecks(SciLoopModel):
    """``decision_logs.guardrail_checks``：按 ``safety → time → cost`` 短路求值。"""

    safety_ok: bool | None = None
    time_ok: bool | None = None
    cost_ok: bool | None = None


class DecisionLog(SciLoopModel):
    """``decision_logs``：全自动决策留痕（本项目可审计性的核心表）。"""

    id: int | None = None
    project_id: int
    pipeline_run_id: int | None = None
    decision_point: DecisionPoint
    stage: PipelineStage | None = None
    context_digest: str = Field(description="决策上下文摘要（可复现：同输入同结论）")
    options_considered: list[Any] = Field(default_factory=list)
    chosen: str
    rationale: str
    risk_score: float = Field(
        ge=0, le=100, description="0.30*cost_exposure + 0.25*blast_radius + …"
    )
    confidence_score: float = Field(ge=0, le=1)
    reversibility_score: float = Field(ge=0, le=1)
    policy_action: PolicyAction = Field(description="规则层拥有最终决定权；LLM 建议分不可覆盖")
    policy_version: str
    guardrail_checks: GuardrailChecks
    cost_usd: float = 0.0
    created_at: datetime | None = None


class Intervention(SciLoopModel):
    """``interventions``：人工介入记录（N1–N4）。"""

    id: int | None = None
    project_id: int
    pipeline_run_id: int | None = None
    node: InterventionNode
    action: InterventionAction
    payload: dict[str, Any] | None = None
    note: str | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 实验
# --------------------------------------------------------------------------- #
class Experiment(SciLoopModel):
    """``experiments``。"""

    id: int | None = None
    stage_output_id: int | None = None
    template_id: TemplateId
    config: dict[str, Any] = Field(default_factory=dict, description="模板参数（sample_size ≤ 50）")
    script_path: str | None = None
    created_at: datetime | None = None


class ExperimentRun(SciLoopModel):
    """``experiment_runs``：单次执行（进程内执行器，无 Docker 沙箱）。"""

    id: int | None = None
    experiment_id: int
    attempt: int = 1
    executor_task_id: str | None = Field(
        default=None, description="v1.1：由 sandbox_container_id 改"
    )
    status: str = Field(description="running | success | failed | timeout")
    raw_output: str | None = None
    artifact_path: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    created_at: datetime | None = None


class ExperimentMetric(SciLoopModel):
    """``experiment_metrics``：指标必须逐项落库（主观量化量禁止只存总分）。"""

    id: int | None = None
    experiment_run_id: int
    metric_name: str
    metric_value: float | None = None
    metric_unit: str | None = None
    extra: dict[str, Any] | None = None
    created_at: datetime | None = None


class ExperimentPassport(SciLoopModel):
    """``experiment_passports``：可复现凭证（**创建后不可 UPDATE**）。"""

    id: int | None = None
    passport_uid: str = Field(description="UUID，对外暴露的稳定标识")
    experiment_run_id: int
    parent_passport_id: int | None = Field(
        default=None, description="replay / rerun 生成的子凭证指向父凭证"
    )

    dataset_name: str
    dataset_version: str
    dataset_sha256: str
    sample_manifest: list[Any] = Field(default_factory=list)

    provider: str
    model_id: str
    prompt_version: str
    prompt_sha256: str
    generation_params: dict[str, Any] = Field(default_factory=dict)

    template_id: TemplateId
    template_config: dict[str, Any] = Field(default_factory=dict)
    code_commit_sha: str
    dependency_lock_sha256: str

    metrics: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float
    is_replay: bool = Field(default=False, description="回放必须为 true；禁止冒充实时结果")
    artifact_manifest: list[Any] = Field(default_factory=list)
    status: PassportStatus = Field(description="任一关键字段缺失 → incomplete，禁止宣称可复现")

    started_at: datetime
    finished_at: datetime
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 请求体（附录 B.4）
# --------------------------------------------------------------------------- #
class PipelineCreateRequest(SciLoopModel):
    """``POST /pipelines``。"""

    project_id: int


class PipelineRunRequest(SciLoopModel):
    """``POST /pipelines/{project_id}/run?mode=auto|manual``。"""

    mode: ProjectMode = ProjectMode.MANUAL


class InterveneRequest(SciLoopModel):
    """``POST /pipelines/{project_id}/intervene``。"""

    node: InterventionNode
    action: InterventionAction
    payload: dict[str, Any] | None = None
    note: str | None = None
