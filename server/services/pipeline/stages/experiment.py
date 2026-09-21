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
"""``experiment`` 环节（WP11-T7）。

流程（严格按 WP11 任务书）
--------------------------
1. 读上游 ``plan_review`` 的 ``selected_method``（含 ``template_id`` / ``params``）
2. 调 WP10 ``evaluate_decision(project_id, "D3", context)`` 拿策略动作
   （``circuit_break`` → 直接 L3 失败，**不执行任何模板**）
3. 经**受限执行器**（:mod:`executor.runner`）跑模板：白名单 + schema + ``sample_size<=50``
   + 并发 ≤2 + 单 Run ≤300s + 出网白名单
4. 采集真实指标落 ``experiment_metrics``（数值一律来自模板执行结果）
5. 生成 Experiment Passport（不可变；重跑用 ``parent_passport_id`` 关联）
6. 回填 ``stage_outputs.output_json`` 供看板展示

失败分级（交回 WP09 引擎）
--------------------------
- ``L1`` 可自动重试：单 Run 超时、上游产出缺失但参数本身合法
- ``L2`` 自动降级：模型不可用、输出超限、Run 失败（需换模型/缩样本/换模板）
- ``L3`` 熔断：安全红线（出网被拒 / 模板未注册或未实现 / 样本量越界 / 护栏 circuit_break）

诚实性
------
- 指标只来自 ``RunOutcome.metrics``（模板对真实输出的统计），本环节**不做任何补齐或估算**
- Run 使用本地桩（无真实 LLM 凭据）时，``degradations`` 与 Passport provenance 会如实标注
- Passport ``status != complete`` 时不宣称可复现，并把缺失字段写进 ``quality_issues``
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from services.pipeline.stages.base import (
    StageContext,
    StageError,
    StageResult,
    StageValidationError,
)

logger = logging.getLogger("sciloop.pipeline.experiment")

STAGE_NAME = "experiment"
DECISION_POINT = "D3"
INTERVENTION_NODE = "N3"

#: 契约硬上限（``contracts.guardrails.safety``）；本环节**不做截断**，越界交给护栏/执行器拒绝
SAMPLE_SIZE_MAX = 50

#: 执行器错误码 → 失败分级（分级口径见模块文档）
_ERROR_LEVELS: dict[str, str] = {
    "run_timeout": "L1",
    "limits_config_invalid": "L3",
    "template_rejected": "L3",
    "template_not_implemented": "L3",
    "sample_size_exceeded": "L3",
    "egress_denied": "L3",
    "model_unavailable": "L2",
    "output_too_large": "L2",
    "executor_error": "L2",
}
#: L1 可自动重试
_RETRYABLE_LEVELS = frozenset({"L1"})


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def _selected_method(ctx: StageContext) -> tuple[dict[str, Any], int | None]:
    """从上游 ``plan_review`` 取选中方案（``(method, selected_index)``）。"""
    review = ctx.upstream("plan_review") or ctx.upstream("plan")
    method = review.get("selected_method")
    index = review.get("selected_method_index")
    selected = dict(method) if isinstance(method, Mapping) else {}
    selected_index = int(index) if isinstance(index, int) and not isinstance(index, bool) else None
    if not selected and ctx.hints.get("template_id"):
        # 人工介入直接指定模板（N3 intervene）：如实记录来源，不伪装成上游方案
        selected = {"template_id": ctx.hints.get("template_id"), "params": {}, "source": "human_hint"}
    return selected, selected_index


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_template_config(
    ctx: StageContext, selected: Mapping[str, Any]
) -> tuple[str, dict[str, Any], int, list[str], dict[str, Any]]:
    """解析 ``(template_id, params, sample_size, notes, source)``。

    只做**透明收敛**，不做静默截断：sample_size 低于模板下限时改用模板默认值并留痕；
    高于硬上限时**原样交给护栏/执行器拒绝**（不在这里偷偷改小）。
    """
    from services.experiment import registry as registry_mod

    hints = dict(ctx.hints or {})
    template_id = str(hints.get("template_id") or selected.get("template_id") or "").strip()
    if not template_id:
        raise StageValidationError(
            "experiment 环节缺少 template_id：上游 plan_review 未给出 selected_method，"
            "且无人工介入提示（hints.template_id）",
            level="L2",
            stage=STAGE_NAME,
            detail={"upstream_stages": sorted(ctx.inputs)},
        )
    try:
        spec = registry_mod.get_spec(template_id)
    except Exception as exc:  # noqa: BLE001 - 未注册/未实现模板 → 安全红线 L3
        raise StageError(
            f"模板 {template_id!r} 不可执行（未注册或未实现）：{exc}",
            level="L3",
            stage=STAGE_NAME,
            detail={"template_id": template_id, "whitelist": list(registry_mod.TEMPLATE_IDS)},
            retryable=False,
        ) from exc
    if not spec.implemented:
        raise StageError(
            f"模板 {spec.template_id} 为占位条目（{spec.placeholder_reason}），禁止执行",
            level="L3",
            stage=STAGE_NAME,
            detail={"template_id": spec.template_id, "status": spec.status},
            retryable=False,
        )

    notes: list[str] = []
    raw_params = selected.get("params") if isinstance(selected.get("params"), Mapping) else {}
    params = {key: value for key, value in dict(raw_params).items() if key != "sample_size"}
    params.update(dict(hints.get("params") or {}))
    if hints.get("model_ref"):
        params["model_ref"] = hints["model_ref"]

    requested = _int_or_none(hints.get("sample_size")) or _int_or_none(
        (selected.get("params") or {}).get("sample_size")
        if isinstance(selected.get("params"), Mapping)
        else None
    )
    degrade_size = _int_or_none(ctx.degrade.get("sample_size")) if isinstance(ctx.degrade, Mapping) else None

    sample_size = requested if requested is not None else spec.sample_size_default
    if degrade_size is not None and spec.sample_size_min <= degrade_size <= SAMPLE_SIZE_MAX:
        if degrade_size != sample_size:
            notes.append(f"按 L2 降级把 sample_size 由 {sample_size} 调整为 {degrade_size}")
        sample_size = degrade_size
    elif degrade_size is not None:
        notes.append(
            f"L2 降级建议的 sample_size={degrade_size} 不在模板允许区间 "
            f"[{spec.sample_size_min}, {SAMPLE_SIZE_MAX}]，未采用（如实记录，不做静默收敛）"
        )
    if sample_size < spec.sample_size_min:
        notes.append(
            f"上游给出的 sample_size={sample_size} 低于模板 {spec.template_id} 下限 "
            f"{spec.sample_size_min}，改用模板默认值 {spec.sample_size_default}"
        )
        sample_size = spec.sample_size_default
    if sample_size > SAMPLE_SIZE_MAX:
        # 不截断：原样交给 D3 护栏与执行器拒绝（这是契约硬约束的验证路径）
        notes.append(
            f"sample_size={sample_size} 超过硬上限 {SAMPLE_SIZE_MAX}：按契约交护栏/执行器拒绝，"
            "本环节不做截断"
        )

    source = {
        "template_source": "human_hint" if hints.get("template_id") else "plan_review.selected_method",
        "selected_method_index": selected.get("selected_method_index"),
        "selected_method_name": selected.get("name"),
        "sample_size_requested": requested,
        "sample_size_degrade_hint": degrade_size,
    }
    return template_id, params, int(sample_size), notes, source


# --------------------------------------------------------------------------- #
# D3 策略判定
# --------------------------------------------------------------------------- #
async def request_d3(
    ctx: StageContext,
    *,
    template_id: str,
    params: Mapping[str, Any],
    sample_size: int,
) -> dict[str, Any]:
    """把**真实实验配置**交给 WP10 的 D3 决策（规则层拥有最终决定权）。"""
    try:
        from services.pipeline.decision_engine import evaluate_decision
    except Exception as exc:  # noqa: BLE001 - WP10 未挂载属并行期状态，如实降级
        logger.warning("D3 策略模块不可用：%s", exc)
        return {"available": False, "reason": f"policy_not_mounted:{type(exc).__name__}"}

    context = {
        "stage": STAGE_NAME,
        "decision_point": DECISION_POINT,
        "attempt": ctx.attempt,
        "iteration": int(ctx.iteration or 1),
        "mode": ctx.mode,
        "pipeline_run_id": ctx.pipeline_run_id,
        "taskbook_id": getattr(ctx.taskbook, "id", None),
        "upstream_stages": sorted(ctx.inputs),
        "proposed_action": "run_template",
        "action_domain": {
            "template_id": template_id,
            "params": dict(params),
            "sample_size": int(sample_size),
        },
    }
    try:
        outcome = await evaluate_decision(ctx.project_id, DECISION_POINT, context, persist=True)
    except Exception as exc:  # noqa: BLE001 - 策略层异常不得让环节静默跳过判定
        logger.warning("D3 策略调用失败：%s", exc, exc_info=True)
        return {"available": False, "reason": f"policy_call_failed:{type(exc).__name__}: {exc}"}
    if not isinstance(outcome, Mapping):
        return {"available": False, "reason": "policy_returned_non_mapping"}
    return {
        "available": True,
        "decision_point": DECISION_POINT,
        "policy_action": outcome.get("policy_action"),
        "chosen": outcome.get("chosen"),
        "risk_score": outcome.get("risk_score"),
        "confidence_score": outcome.get("confidence_score"),
        "reversibility_score": outcome.get("reversibility_score"),
        "rationale": outcome.get("rationale"),
        "policy_version": outcome.get("policy_version"),
        "decision_log_id": outcome.get("decision_log_id"),
        "guardrails": outcome.get("guardrails"),
        "guardrail_checks": outcome.get("guardrail_checks"),
        "missing_features": list(outcome.get("missing_features") or []),
        "note": (
            "本结论基于真实实验配置（action_domain=template_id/params/sample_size）；"
            "策略动作以 WP10 规则层为准，LLM 建议分无法覆盖"
        ),
    }


# --------------------------------------------------------------------------- #
# 错误分级
# --------------------------------------------------------------------------- #
def classify_executor_error(outcome: Any) -> tuple[str, str]:
    """执行器失败 → ``(level, reason)``。"""
    code = str(getattr(outcome, "error_code", "") or "")
    status = str(getattr(outcome, "status", "") or "")
    if code:
        level = _ERROR_LEVELS.get(code, "L2")
        return level, f"执行器错误 {code}（status={status}）"
    if status in {"timeout", "rejected"}:
        return _ERROR_LEVELS.get(status, "L2"), f"执行器状态 {status}"
    return "L2", f"Run 未成功（status={status}）"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
async def run(ctx: StageContext) -> StageResult:  # noqa: C901 - 按任务书步骤线性展开
    from executor.runner import execute_template
    from services.experiment import metrics as M
    from services.experiment import passport as P

    started = time.perf_counter()
    await ctx.progress(5, "读取上游选中方案并解析实验配置")

    selected, selected_index = _selected_method(ctx)
    template_id, params, sample_size, cfg_notes, source = resolve_template_config(ctx, selected)
    degradations: list[str] = []
    notes: list[str] = list(cfg_notes)

    await ctx.progress(20, f"模板 {template_id} 配置就绪（sample_size={sample_size}），提交 D3 决策")
    d3 = await request_d3(ctx, template_id=template_id, params=params, sample_size=sample_size)
    action = str(d3.get("policy_action") or "")
    if action == "circuit_break":
        guard = d3.get("guardrails") or {}
        raise StageError(
            f"D3 硬护栏熔断（circuit_break）：first_failure={guard.get('first_failure')}；"
            "实验未执行，按契约生成失败报告",
            level="L3",
            stage=STAGE_NAME,
            detail={"d3": d3, "template_id": template_id, "sample_size": sample_size},
            retryable=False,
        )
    human_gate_required = action == "need_human"
    if human_gate_required:
        notes.append(
            f"D3 判定 need_human（人工节点 {INTERVENTION_NODE}）：自动模式下引擎在本环节后置门禁，"
            "本环节按当前策略动作继续执行并如实留痕"
        )
    if not d3.get("available"):
        degradations.append(
            f"D3 策略层不可用（{d3.get('reason')}）：本次未做风险判定，三分不可用（如实记录）"
        )

    await ctx.progress(35, "经受限执行器执行模板")
    config: dict[str, Any] = {"sample_size": int(sample_size), "params": dict(params)}
    outcome = await execute_template(
        template_id,
        config,
        project_id=ctx.project_id,
        stage=STAGE_NAME,
        attempt=ctx.attempt,
    )
    degradations.extend(list(getattr(outcome, "degradations", []) or []))
    is_stub = any(bool(record.get("is_stub")) for record in outcome.records)

    # ---- 指标落库（真实值；失败 Run 的指标可能为空，不补齐） ----
    metric_meta = {
        "template_id": template_id,
        "run_id": outcome.run_id,
        "experiment_id": outcome.experiment_id,
        "sample_size": sample_size,
        "prompt_version": outcome.prompt_payload.get("prompt_version"),
        "model_refs": list((outcome.prompt_payload or {}).get("model_refs") or []),
        "is_replay": outcome.is_replay,
        "is_stub": is_stub,
        "status": outcome.status,
        "duration_ms": outcome.duration_ms,
        "notes": list(outcome.notes or []),
    }
    metric_result: dict[str, Any] = {"rows": 0, "metric_names": []}
    if outcome.metrics:
        metric_result = await M.persist_run_metrics(
            run_id=outcome.run_id, metrics=outcome.metrics, meta=metric_meta
        )

    # ---- Passport（不可变凭证；失败 Run 也留痕，status=failed） ----
    passport_note = (
        "本次 Run 由**本地确定性桩**（provider=local-stub）作答：无真实 LLM 凭据，"
        "指标反映桩的能力，**不得作为实验结果或实时模型结论引用**"
        if is_stub
        else None
    )
    passport = await P.create_passport(
        outcome,
        parent_passport_id=None,
        note=passport_note,
        metric_meta=metric_meta,
    )
    passport_status = str(passport.get("status") or "")
    missing = list(passport.get("missing_fields") or [])
    if passport_status != "complete":
        degradations.append(
            f"Passport 非 complete（status={passport_status}，缺失字段={missing}）："
            "禁止宣称本次实验可复现"
        )

    await ctx.emit_event(
        "passport_ready",
        {
            "passport_id": passport.get("id"),
            "status": passport_status,
            "is_replay": bool(passport.get("is_replay")),
            "template_id": template_id,
        },
    )

    block = {
        "experiment_id": outcome.experiment_id,
        "run_id": outcome.run_id,
        "template_id": template_id,
        "template_version": outcome.prompt_payload.get("template_version"),
        "status": outcome.status,
        "sample_size": sample_size,
        "metrics": dict(outcome.metrics or {}),
        "metric_names": list(metric_result.get("metric_names") or []),
        "passport_id": passport.get("id"),
        "passport_uid": str(passport.get("passport_uid") or ""),
        "passport_status": passport_status,
        "is_replay": bool(passport.get("is_replay")),
        "is_stub": is_stub,
        "artifact_path": outcome.artifact_path,
        "duration_ms": outcome.duration_ms,
        "cost_usd": outcome.cost_usd,
        "degradations": degradations,
        "notes": list(outcome.notes or []),
        "d3": {key: d3.get(key) for key in ("policy_action", "chosen", "rationale", "risk_score")},
    }
    backfill = await M.backfill_stage_output(
        pipeline_run_id=ctx.pipeline_run_id,
        stage=STAGE_NAME,
        attempt=ctx.attempt,
        block=block,
    )
    if not backfill.get("updated"):
        notes.append(f"stage_outputs 未回填（{backfill.get('reason')}）：看板将以本环节产出为准")

    duration_ms = int((time.perf_counter() - started) * 1000)

    # ---- 失败 Run：如实分级交回引擎（Passport 已留痕） ----
    if outcome.status != "success":
        level, reason = classify_executor_error(outcome)
        raise StageError(
            f"experiment Run 未成功：{reason}；error={outcome.error}",
            level=level,
            stage=STAGE_NAME,
            detail={
                "run_id": outcome.run_id,
                "passport_id": passport.get("id"),
                "template_id": template_id,
                "status": outcome.status,
                "notes": notes,
                "degradations": degradations,
            },
            retryable=level in _RETRYABLE_LEVELS,
        )

    quality_issues: list[str] = []
    if passport_status != "complete":
        quality_issues.append(f"Passport status={passport_status}，缺失字段={missing}")
    if is_stub:
        quality_issues.append(
            "本地桩作答（provider=local-stub）：真实链路验收可用，但不得作为实验结果引用"
        )
    if action == "need_human":
        quality_issues.append(f"D3 判定 need_human，等待人工在 {INTERVENTION_NODE} 介入")

    await ctx.progress(100, "实验完成：指标已落库，Passport 已生成")
    logger.info(
        "experiment done run=%s template=%s status=%s sample_size=%s passport=%s(%s) "
        "stub=%s replay=%s max_concurrency=%s duration_ms=%s",
        outcome.run_id,
        template_id,
        outcome.status,
        sample_size,
        passport.get("id"),
        passport_status,
        is_stub,
        bool(passport.get("is_replay")),
        (outcome.concurrency or {}).get("max_observed"),
        duration_ms,
    )

    return StageResult(
        output={
            "template_id": template_id,
            "template_version": outcome.prompt_payload.get("template_version"),
            "experiment_id": outcome.experiment_id,
            "run_id": outcome.run_id,
            "sample_size": sample_size,
            "status": outcome.status,
            "metrics": dict(outcome.metrics or {}),
            "metric_rows": int(metric_result.get("rows") or 0),
            "metric_names": list(metric_result.get("metric_names") or []),
            "passport": {
                "id": passport.get("id"),
                "passport_uid": str(passport.get("passport_uid") or ""),
                "status": passport_status,
                "is_replay": bool(passport.get("is_replay")),
                "missing_fields": missing,
                "dataset_sha256": passport.get("dataset_sha256"),
                "prompt_sha256": passport.get("prompt_sha256"),
                "code_commit_sha": passport.get("code_commit_sha"),
                "dependency_lock_sha256": passport.get("dependency_lock_sha256"),
            },
            "artifact_path": outcome.artifact_path,
            "artifact_manifest": outcome.artifact_manifest,
            "cost_usd": outcome.cost_usd,
            "duration_ms": outcome.duration_ms,
            "is_stub": is_stub,
            "is_replay": outcome.is_replay,
            "config_source": source,
            "d3": {key: d3.get(key) for key in ("policy_action", "chosen", "rationale", "risk_score")},
            "human_gate": {
                "node": INTERVENTION_NODE,
                "decision_point": DECISION_POINT,
                "required": human_gate_required,
                "note": "manual 模式下引擎在本环节后把下一环节标记 waiting_human（N3）",
            },
            "degradations": degradations,
            "compliance": {
                "disclaimer": "本内容由 AI 辅助生成，需研究者自行核验",
                "reproducible_claim": passport_status == "complete" and not is_stub,
                "note": (
                    "仅当 Passport status=complete 且非本地桩作答时才可宣称可复现"
                ),
            },
        },
        cost_usd=float(outcome.cost_usd or 0.0),
        duration_ms=duration_ms,
        selected_method_index=selected_index,
        metrics={
            "run_id": outcome.run_id,
            "passport_id": passport.get("id"),
            "passport_status": passport_status,
            "sample_size": sample_size,
            "metric_rows": int(metric_result.get("rows") or 0),
            "cost_usd": outcome.cost_usd,
            "duration_ms": outcome.duration_ms,
            "is_stub": is_stub,
            "is_replay": outcome.is_replay,
            "max_concurrency_observed": (outcome.concurrency or {}).get("max_observed"),
        },
        quality_ok=not quality_issues,
        quality_issues=quality_issues,
        degradations=degradations,
        notes=list(outcome.notes or []) + notes,
        decision={
            "decision_point": DECISION_POINT,
            "chosen": d3.get("chosen") or "run_template",
            "rationale": d3.get("rationale")
            or (d3.get("reason") or "策略层不可用，按上游方案执行模板"),
        },
    )


class ExperimentStage:
    """``experiment`` 环节处理器（实验配置决策 D3，人工节点 N3）。"""

    name = STAGE_NAME
    decision_point = DECISION_POINT

    async def run(self, ctx: StageContext) -> StageResult:
        return await run(ctx)


STAGE = ExperimentStage()


def register() -> ExperimentStage:
    """把本环节注册进 WP09 的注册表（幂等；供 API 模块导入时调用）。"""
    from services.pipeline.stages import register_stage

    return register_stage(STAGE_NAME, STAGE)  # type: ignore[return-value]


__all__ = [
    "DECISION_POINT",
    "INTERVENTION_NODE",
    "SAMPLE_SIZE_MAX",
    "STAGE",
    "STAGE_NAME",
    "ExperimentStage",
    "classify_executor_error",
    "register",
    "request_d3",
    "resolve_template_config",
    "run",
]
