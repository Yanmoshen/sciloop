# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""环节协议（WP09 提供的骨架，供 WP11/WP12/WP14 实现其余四个环节）。

约定
----
每个环节是一个可 await 的处理器：``async def run(ctx: StageContext) -> StageResult``。

- ``ctx`` 提供：任务书、上游环节产出（`stage_outputs.output_json`）、SSE 发布函数、
  LLM 调用助手（统一写 ``llm_call_logs``）、降级提示（L2 处置结果）
- 环节**不得**自行绕过风险策略或护栏做动作；策略判定由 engine 调用 WP10 的
  ``evaluate_decision`` 完成
- 环节缺失时抛 :class:`StageNotImplementedError`（**禁止静默跳过**）
- 环节校验失败时抛 :class:`StageValidationError`，并用 ``level`` 标明 L1/L2
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from llm.types import LLMResult

#: 六环节固定顺序（contracts.enums.pipeline_stage）
STAGE_ORDER: tuple[str, ...] = (
    "survey",
    "plan",
    "plan_review",
    "experiment",
    "writing",
    "review",
)

#: 环节 → 决策点（``contracts.decision_points``）。``plan`` 无决策点（附录 D.2：生成候选，选型在 D2）
STAGE_DECISION_POINT: dict[str, str | None] = {
    "survey": "D1",
    "plan": None,
    "plan_review": "D2",
    "experiment": "D3",
    "writing": "D6",
    "review": "D5",
}

#: 环节 → 人工介入节点（contracts.intervention_mapping）
STAGE_INTERVENTION_NODE: dict[str, str | None] = {
    "survey": None,
    "plan": None,
    "plan_review": "N2",
    "experiment": "N3",
    "writing": "N4",
    "review": None,
}


class StageError(RuntimeError):
    """环节执行失败的基类（携带失败分级与上下文）。"""

    code = "stage_error"
    default_level = "L1"

    def __init__(
        self,
        message: str,
        *,
        level: str | None = None,
        stage: str | None = None,
        detail: Any = None,
        retryable: bool | None = None,
    ) -> None:
        self.level = level or self.default_level
        self.stage = stage
        self.detail = detail
        self.retryable = retryable
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "level": self.level,
            "stage": self.stage,
            "detail": self.detail,
        }


class StageValidationError(StageError):
    """环节产出不满足附录 D 的校验规则。

    ``level`` 决定处置：``L1``（格式/解析类，自动重试）或 ``L2``（质量类，自动降级）。
    """

    code = "stage_validation_failed"


class StageNotImplementedError(StageError):
    """环节尚未实现（WP11/WP12/WP14 未落地）。**不允许被当作成功跳过。**"""

    code = "stage_not_implemented"
    default_level = "L3"

    def __init__(self, stage: str, *, detail: Any = None) -> None:
        super().__init__(
            f"环节 '{stage}' 尚未注册实现：请由对应工作包调用 "
            f"register_stage('{stage}', handler) 注入（禁止静默跳过环节）",
            level="L3",
            stage=stage,
            detail=detail,
            retryable=False,
        )


class StageTimeoutError(StageError):
    """环节超时（时长护栏由 WP10 判定，这里只负责用超时中断执行）。"""

    code = "stage_timeout"
    default_level = "L1"


@dataclass
class StageContext:
    """环节执行上下文（engine 构造，环节只读）。"""

    project_id: int
    pipeline_run_id: int
    stage: str
    iteration: int
    mode: str
    attempt: int
    session: Any  # AsyncSession：环节需要落库时使用（禁止跨环节复用事务）
    taskbook: Any | None = None
    project: Any | None = None
    #: 上游环节产出：``{stage: output_json}``（仅已完成环节）
    inputs: dict[str, Any] = field(default_factory=dict)
    #: L2 降级提示（failure_handler 生成）：如 ``{"sample_size": 8, "queries": [...]}``
    degrade: dict[str, Any] = field(default_factory=dict)
    #: 额外提示（人工 intervene 的 payload 等）
    hints: dict[str, Any] = field(default_factory=dict)
    #: SSE 发布：``await ctx.emit("stage_progress", {...})``
    emit: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None
    #: 环节超时秒数（PIPELINE_MAX_STAGE_MINUTES）
    timeout_seconds: int = 1200
    #: 人工标签 / 其它自由上下文
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def decision_point(self) -> str | None:
        return STAGE_DECISION_POINT.get(self.stage)

    @property
    def taskbook_payload(self) -> dict[str, Any]:
        """任务书关键字段（环节输入按附录 D.1 / D.2）。"""
        tb = self.taskbook
        if tb is None:
            return {}
        return {
            "taskbook_id": getattr(tb, "id", None),
            "research_question": getattr(tb, "research_question", None),
            "target_datasets": getattr(tb, "target_datasets", None),
            "baselines": getattr(tb, "baselines", None),
            "metrics": getattr(tb, "metrics", None),
            "compute_budget": getattr(tb, "compute_budget", None),
            "deliverables": getattr(tb, "deliverables", None),
            "max_iterations": getattr(tb, "max_iterations", None),
            "score_threshold": _float_or_none(getattr(tb, "score_threshold", None)),
            "marginal_gain_threshold": _float_or_none(getattr(tb, "marginal_gain_threshold", None)),
        }

    def upstream(self, stage: str) -> dict[str, Any]:
        value = self.inputs.get(stage)
        return value if isinstance(value, dict) else {}

    def progress(self, percent: int, message: str) -> Awaitable[None]:
        return self.emit_event(
            "stage_progress",
            {"stage": self.stage, "percent": int(percent), "message": message},
        )

    def emit_event(self, event: str, payload: dict[str, Any]) -> Awaitable[None]:
        if self.emit is None:
            return _noop_emit(event, payload)
        return self.emit(event, payload)


async def _noop_emit(event: str, payload: dict[str, Any]) -> None:  # pragma: no cover - 无订阅者场景
    return None


@dataclass
class StageResult:
    """环节产出（engine 负责落 ``stage_outputs``）。"""

    output: dict[str, Any]
    text: str | None = None
    cost_usd: float = 0.0
    duration_ms: int | None = None
    #: 评审类环节的裁决（``contracts.enums.verdict``）
    verdict: str | None = None
    #: 选中方案下标（plan_review）
    selected_method_index: int | None = None
    #: 供 SSE ``stage_done`` 的指标（真实值；没有就留空，禁止编造）
    metrics: dict[str, Any] | None = None
    #: 质量判定：False 触发 L2 降级（如 methods < 2、检索为空、指标不达标）
    quality_ok: bool | None = None
    quality_issues: list[str] = field(default_factory=list)
    #: 需要回退的上游环节（附录 D.3：``revise`` → 回退 plan，全局最多 1 次）
    revert_to: str | None = None
    #: 已发生的降级说明（透明记录）
    degradations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: 本环节建议的 D 决策动作（供 SSE ``decision`` 事件；策略动作以 WP10 为准）
    decision: dict[str, Any] | None = None

    def stage_done_payload(self, stage: str) -> dict[str, Any]:
        """``stage_done`` SSE 载荷（``{stage,verdict?,selected_method_index?,metrics?}``）。"""
        payload: dict[str, Any] = {"stage": stage}
        if self.verdict:
            payload["verdict"] = self.verdict
        if self.selected_method_index is not None:
            payload["selected_method_index"] = self.selected_method_index
        if self.metrics:
            payload["metrics"] = self.metrics
        return payload


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@runtime_checkable
class Stage(Protocol):
    """环节协议：任何实现了 ``run`` 的对象都可注册。"""

    name: str
    #: 决策点（``None`` 表示该环节不触发 D 决策，如 plan）
    decision_point: str | None

    async def run(self, ctx: StageContext) -> StageResult:  # pragma: no cover - 协议
        ...


# --------------------------------------------------------------------------- #
# LLM 调用助手：所有环节的 LLM 调用都必须经此函数（保证写 llm_call_logs）
# --------------------------------------------------------------------------- #
async def call_llm(
    ctx: StageContext,
    messages: list[dict[str, Any]],
    *,
    json_schema: dict[str, Any] | None = None,
    purpose: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    model_ref: str | None = None,
    allow_fallback: bool = True,
) -> LLMResult:
    """统一 LLM 入口：``llm.chat`` 的薄封装（stage/purpose/project_id 固定带上）。

    记账由 WP02 的适配层完成（每次调用一行 ``llm_call_logs``，含重试）。
    """
    from llm.adapter import chat

    started = time.perf_counter()
    result = await chat(
        messages,
        model_ref,
        temperature,
        max_tokens,
        json_schema,
        project_id=ctx.project_id,
        stage=ctx.stage,
        purpose=purpose,
        allow_fallback=allow_fallback,
    )
    _ = time.perf_counter() - started
    return result


def result_cost_of(*results: LLMResult | None) -> float:
    """累计若干次 LLM 调用的成本（单价缺失的调用记 0 并在 notes 中披露）。"""
    total = 0.0
    for result in results:
        if result is None:
            continue
        if result.cost_usd is None:
            continue
        total += float(result.cost_usd)
    return round(total, 6)


def unknown_cost_notes(*results: LLMResult | None) -> list[str]:
    notes: list[str] = []
    for result in results:
        if result is not None and result.cost_usd is None:
            notes.append(
                f"LLM 调用单价缺失（{result.model_ref}）：cost_usd=null，未估算"
                f"{('：' + result.cost_unknown_reason) if result.cost_unknown_reason else ''}"
            )
    return notes


__all__ = [
    "STAGE_DECISION_POINT",
    "STAGE_INTERVENTION_NODE",
    "STAGE_ORDER",
    "Stage",
    "StageContext",
    "StageError",
    "StageNotImplementedError",
    "StageResult",
    "StageTimeoutError",
    "StageValidationError",
    "call_llm",
    "result_cost_of",
    "unknown_cost_notes",
]
