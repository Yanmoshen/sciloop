# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``T3_model_compare``：多模型横向对照（WP11-T3）。

实验设计（唯一自变量 = 模型；Prompt、样本、温度、max_tokens 全部固定）：

====================  ==========================================================
样本                   数据集 ``templates/data/eval_set_v1.json``（20–50 条，按 id 升序）
Prompt                 ``templates/data/T3_prompt.json``（**两个模型完全同一套**正文）
模型                   ``params.model_refs`` 显式两个；缺省取 ``stage='experiment'`` 路由链前两个
指标                   ``accuracy``（逐模型）、``cost_usd``（**由 llm_call_logs 汇总**）、
                       ``latency_ms``，并给出两模型差值
用途                   结果作为 WP12 盲评校准的对照数据
====================  ==========================================================

成本一律来自 ``llm_call_logs``（按 ``purpose='T3_model_compare:<model_ref>'`` 与 id 窗口过滤），
单价缺失的调用记 ``cost_usd=null`` 并在 ``notes`` 披露，**不做估算**。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from executor.runner import RunContext
from services.experiment import metrics as M
from services.experiment.templates import common
from services.experiment.templates.common import call_model, prepare_targets

logger = logging.getLogger("sciloop.experiment.T3")

TEMPLATE_ID = "T3_model_compare"
PROMPT_ASSET = "T3_prompt.json"


async def run(ctx: RunContext) -> dict[str, Any]:
    """执行 T3（两模型 × N 样本；返回真实指标与逐样本原始输出）。"""
    started = time.perf_counter()
    asset = common.load_prompt_asset(PROMPT_ASSET)
    temperature = ctx.params.get("temperature")
    max_tokens = ctx.params.get("max_tokens")
    explicit = [str(ref) for ref in (ctx.params.get("model_refs") or [])]
    targets = await prepare_targets(ctx, explicit, count=2)
    refs = [str(getattr(target, "model_ref", "") or "") for target in targets]
    if len(set(refs)) < 2 and not ctx.replay_only:
        from executor.errors import ModelUnavailableError

        raise ModelUnavailableError(
            f"T3 需要两个**不同**的模型做对照，当前解析到 {refs}（去重后 {sorted(set(refs))}）；"
            "请在「设置 → 模型配置」中配置第二个供应商或设置 LLM_FALLBACK_*",
            detail={"template_id": TEMPLATE_ID, "model_refs": refs},
        )

    log_start = await M.current_max_log_id()
    deadline_at = started + max(5.0, float(ctx.deadline_seconds))

    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    truncated = False
    for target in targets:
        group = str(getattr(target, "model_ref", "") or "")
        for sample in ctx.samples:
            if time.perf_counter() >= deadline_at:
                truncated = True
                break
            system = common.render(
                str(asset.get("system") or ""), task_hint=common.task_hint(sample.get("task"))
            )
            user = common.render(
                str(asset.get("user") or "{question}"), question=str(sample.get("input") or "")
            )
            try:
                record = await call_model(
                    ctx,
                    target,
                    common.build_messages(system, user),
                    group=group,
                    sample_id=str(sample.get("id")),
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - 单样本失败如实记录
                errors.append(
                    {
                        "model_ref": group,
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
            f"仅完成 {len(records)}/{len(ctx.samples) * len(targets)} 次调用，未执行样本不进分母"
        )

    log_end = await M.current_max_log_id()
    llm_logs = await M.collect_run_llm_logs(
        stage=ctx.stage,
        purpose_prefix=f"{TEMPLATE_ID}:",
        project_id=ctx.project_id,
        log_id_after=log_start,
        log_id_until=log_end,
    )

    result_metrics, by_model = _aggregate(records, refs=refs, llm_logs=llm_logs)
    duration_ms = int((time.perf_counter() - started) * 1000)
    result_metrics["duration_ms"] = duration_ms
    result_metrics["samples_executed"] = len(records)
    result_metrics["samples_requested"] = len(ctx.samples) * len(targets)
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
                        "id": "shared",
                        "system": asset.get("system"),
                        "user_template": asset.get("user"),
                    }
                ]
            ),
            "generation_params": {
                "temperature": temperature,
                "max_tokens": max_tokens,
                "model_refs": refs,
                "judge_rule": "normalize+exact_match（数字按数值比较）",
            },
            "model_refs": refs,
            "dataset": dict(ctx.dataset or {}),
            "egress": common.egress_report(targets),
            "llm_log_window": llm_logs.get("log_id_range"),
            "comparison_table": by_model,
        },
    }


def _aggregate(
    records: list[dict[str, Any]],
    *,
    refs: list[str],
    llm_logs: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """逐模型 accuracy / latency / 成本（成本取自 llm_call_logs.by_model）与差值。"""
    metrics: dict[str, Any] = {}
    by_model_logs = dict(llm_logs.get("by_model") or {})
    table: list[dict[str, Any]] = []
    for ref in refs:
        rows = [record for record in records if str(record.get("model_ref")) == ref]
        scored = M.accuracy(rows, prediction_key="prediction", reference_key="reference")
        latency = M.mean_of([record.get("duration_ms") for record in rows])
        bucket = by_model_logs.get(ref) or {}
        cost = bucket.get("cost_usd")
        metrics[M.metric_key("accuracy", ref)] = scored["accuracy"]
        metrics[M.metric_key("samples_correct", ref)] = scored["correct"] if scored["scored"] else None
        metrics[M.metric_key("samples_total", ref)] = scored["scored"] if scored["scored"] else None
        metrics[M.metric_key("latency_ms", ref)] = latency
        metrics[M.metric_key("cost_usd", ref)] = cost
        metrics[M.metric_key("llm_calls", ref)] = bucket.get("calls") if bucket else None
        metrics[M.metric_key("cost_per_correct_usd", ref)] = (
            round(float(cost) / scored["correct"], 6)
            if isinstance(cost, (int, float)) and scored["correct"]
            else None
        )
        table.append(
            {
                "model_ref": ref,
                "accuracy": scored["accuracy"],
                "samples_correct": scored["correct"],
                "samples_total": scored["scored"],
                "latency_ms": latency,
                "cost_usd": cost,
                "cost_unknown_calls": bucket.get("unknown_cost_calls", 0),
                "llm_calls": bucket.get("calls"),
                "provider": (rows[0].get("provider") if rows else None),
                "model_id": (rows[0].get("model_id") if rows else None),
            }
        )

    pair = table if len(table) == 2 else []
    if pair:
        left, right = pair[0], pair[1]
        if isinstance(left["accuracy"], (int, float)) and isinstance(right["accuracy"], (int, float)):
            metrics["accuracy_delta"] = round(float(left["accuracy"]) - float(right["accuracy"]), 6)
        if isinstance(left["cost_usd"], (int, float)) and isinstance(right["cost_usd"], (int, float)):
            metrics["cost_delta"] = round(float(left["cost_usd"]) - float(right["cost_usd"]), 6)
        if isinstance(left["latency_ms"], (int, float)) and isinstance(right["latency_ms"], (int, float)):
            metrics["latency_delta_ms"] = round(float(left["latency_ms"]) - float(right["latency_ms"]), 2)
        scored_models = [row for row in pair if isinstance(row["accuracy"], (int, float))]
        metrics["accuracy_spread"] = (
            round(max(row["accuracy"] for row in scored_models) - min(row["accuracy"] for row in scored_models), 6)
            if len(scored_models) >= 2
            else None
        )
    metrics["total_cost_usd"] = llm_logs.get("cost_usd")
    return metrics, table


__all__ = ["PROMPT_ASSET", "TEMPLATE_ID", "run"]
