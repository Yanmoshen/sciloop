# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``T1_prompt_variant``：同一任务下多套 prompt 变体对比（WP11-T2）。

实验设计（唯一自变量 = Prompt 变体；模型、样本、温度、max_tokens 全部固定）：

====================  ==========================================================
样本                   数据集 ``templates/data/eval_set_v1.json``，按 id 升序取 20–50 条
变体                   ``templates/data/T1_prompts.json`` 的 3 套 system 正文
指标                   ``accuracy``（逐变体，与参考答案真实比对）、
                       ``answer_consistency``（变体两两一致率均值）、
                       ``latency_ms`` / ``cost_usd``（**来自 llm_call_logs**）
判分                   归一后字符串相等（数字按数值比较）；见 :mod:`..metrics`
====================  ==========================================================

所有指标都来自真实模型输出；没有执行的样本一律不进分母，并在
``metrics.samples_executed`` / ``notes`` 中如实披露。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.executor.runner import RunContext
from app.services.experiment import metrics as M
from app.services.experiment.templates import common
from app.services.experiment.templates.common import call_model, prepare_targets

logger = logging.getLogger("sciloop.experiment.T1")

TEMPLATE_ID = "T1_prompt_variant"
PROMPT_ASSET = "T1_prompts.json"


async def run(ctx: RunContext) -> dict[str, Any]:
    """执行 T1（返回 ``{status, metrics, records, cost_usd, degradations, notes, prompt_payload}``）。"""
    started = time.perf_counter()
    asset = common.load_prompt_asset(PROMPT_ASSET)
    variants: list[dict[str, Any]] = [dict(v) for v in asset.get("variants") or []]
    if not variants:
        raise ValueError(f"Prompt 资产 {PROMPT_ASSET} 未定义任何变体")

    temperature = ctx.params.get("temperature")
    max_tokens = ctx.params.get("max_tokens")
    explicit = [str(ctx.params["model_ref"])] if ctx.params.get("model_ref") else []
    targets = await prepare_targets(ctx, explicit, count=1)
    target = targets[0] if targets else None

    log_start = await M.current_max_log_id()
    deadline_at = started + max(5.0, float(ctx.deadline_seconds))

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    truncated = False
    for variant in variants:
        group = str(variant.get("id") or "")
        for sample in ctx.samples:
            if time.perf_counter() >= deadline_at:
                truncated = True
                break
            system = common.render(str(variant.get("system") or ""), task_hint=common.task_hint(sample.get("task")))
            user = common.render(str(variant.get("user") or "{question}"), question=str(sample.get("input") or ""))
            messages = common.build_messages(system, user)
            try:
                record = await call_model(
                    ctx,
                    target,
                    messages,
                    group=group,
                    sample_id=str(sample.get("id")),
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - 单样本失败如实记录，不污染其它样本
                errors.append(
                    {
                        "variant": group,
                        "sample_id": str(sample.get("id")),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            record.update(
                {
                    "task": sample.get("task"),
                    "reference": sample.get("reference"),
                    "correct": M.answers_match(record.get("prediction"), sample.get("reference")),
                }
            )
            records.append(record)
        if truncated:
            break

    if truncated:
        ctx.degrade(
            f"单 Run 时间预算（{ctx.deadline_seconds:.0f}s）用尽："
            f"仅完成 {len(records)}/{len(ctx.samples) * len(variants)} 次调用，未执行样本不进分母"
        )

    log_end = await M.current_max_log_id()
    llm_logs = await M.collect_run_llm_logs(
        stage=ctx.stage,
        purpose_prefix=f"{TEMPLATE_ID}:",
        project_id=ctx.project_id,
        log_id_after=log_start,
        log_id_until=log_end,
    )

    result_metrics, predictions, agreement = _aggregate(
        records, variants=[str(v.get("id")) for v in variants], llm_logs=llm_logs
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    result_metrics["duration_ms"] = duration_ms
    result_metrics["samples_executed"] = len(records)
    result_metrics["samples_requested"] = len(ctx.samples) * len(variants)
    result_metrics["calls_failed"] = len(errors)

    notes: list[str] = list(llm_logs.get("notes") or [])
    if errors:
        notes.append(f"{len(errors)} 次调用失败（已如实记录，未做补跑或估算）：{errors[:3]}")
    if not records:
        notes.append("没有任何成功的样本调用：指标为空，未生成任何数值")

    degradations = list(ctx.degradations)
    if llm_logs.get("is_replay_calls"):
        degradations.append(
            f"本次 Run 有 {llm_logs['is_replay_calls']} 次调用来自回放（is_replay=true），"
            "不得作为实时结果使用"
        )
    if records and all(record.get("is_replay") for record in records):
        degradations.append("全部样本均由回放源提供（is_replay=true）：本 Run 是回放，不是实时实验")

    status = "success" if records and not truncated and not errors else ("partial" if records else "failed")
    return {
        "status": status,
        "metrics": result_metrics,
        "records": records,
        "cost_usd": llm_logs.get("cost_usd"),
        "degradations": degradations,
        "notes": notes,
        "prompt_payload": {
            "template_id": TEMPLATE_ID,
            "template_version": ctx.template_version,
            "prompt_version": asset.get("prompt_version"),
            "prompt_entries": common.prompt_digest_payload(
                [
                    {
                        "id": variant.get("id"),
                        "system": variant.get("system"),
                        "user_template": variant.get("user"),
                    }
                    for variant in variants
                ]
            ),
            "generation_params": {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "variants": [str(v.get("id")) for v in variants],
                "judge_rule": "normalize+exact_match（数字按数值比较）",
            },
            "model_refs": [getattr(target, "model_ref", None)] if target else [],
            "dataset": dict(ctx.dataset or {}),
            "egress": common.egress_report(targets),
            "llm_log_window": llm_logs.get("log_id_range"),
            "agreement_pairs": agreement.get("pairs") or {},
        },
    }


def _aggregate(
    records: list[dict[str, Any]],
    *,
    variants: list[str],
    llm_logs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]], dict[str, Any]]:
    """按变体计算 accuracy / 延迟 / 成本（成本取自 ``llm_call_logs`` 的 purpose 分组）。"""
    metrics: dict[str, Any] = {}
    predictions: dict[str, dict[str, str]] = {}
    by_purpose: dict[str, dict[str, Any]] = dict(llm_logs.get("by_purpose") or {})
    for group in variants:
        rows = [record for record in records if str(record.get("group")) == group]
        scored = M.accuracy(rows, prediction_key="prediction", reference_key="reference")
        predictions[group] = {
            str(record.get("sample_id")): str(record.get("prediction") or "") for record in rows
        }
        metrics[M.metric_key("accuracy", group)] = scored["accuracy"]
        metrics[M.metric_key("samples_correct", group)] = scored["correct"] if scored["scored"] else None
        metrics[M.metric_key("samples_total", group)] = scored["scored"] if scored["scored"] else None
        metrics[M.metric_key("latency_ms", group)] = M.mean_of(
            [record.get("duration_ms") for record in rows]
        )
        bucket = by_purpose.get(f"{TEMPLATE_ID}:{group}") or {}
        metrics[M.metric_key("cost_usd", group)] = bucket.get("cost_usd")
        metrics[M.metric_key("llm_calls", group)] = bucket.get("calls") if bucket else None

    accuracies = [
        metrics[M.metric_key("accuracy", group)]
        for group in variants
        if isinstance(metrics.get(M.metric_key("accuracy", group)), (int, float))
    ]
    metrics["accuracy_spread"] = (max(accuracies) - min(accuracies)) if len(accuracies) >= 2 else None
    metrics["accuracy_mean"] = M.mean_of(accuracies)
    agreement = M.pairwise_agreement(predictions) if len(predictions) >= 2 else {"pairs": {}, "mean_agreement": None}
    metrics["answer_consistency"] = agreement["mean_agreement"]
    metrics["avg_latency_ms"] = M.mean_of(
        [metrics[M.metric_key("latency_ms", group)] for group in variants]
    )
    metrics["total_cost_usd"] = llm_logs.get("cost_usd")
    metrics["llm_calls"] = llm_logs.get("calls")
    return metrics, predictions, agreement


__all__ = ["PROMPT_ASSET", "TEMPLATE_ID", "run"]
