# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""指标采集与落库（WP11-T4）。

红线：
- **指标只能来自真实执行结果**：本模块只做「解析 / 归一 / 统计 / 落库」，
  不生成任何默认值、不补齐缺失样本、不做平滑或估算。
- 每个数值指标写一行 ``experiment_metrics``（含 ``metric_unit`` 与 ``extra`` 溯源：
  样本数、分母、判分规则、模型、原始输出摘要）；不可量化的信息进 ``extra`` 的
  ``metric_meta``，**不硬凑成数值**。
- 采集后用 ``INSERT ... ON CONFLICT DO UPDATE`` 回填 ``stage_outputs.output_json``
  （唯一约束 ``(pipeline_run_id, stage, attempt)``，见附录 A.0）。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

logger = logging.getLogger("sciloop.experiment.metrics")

#: ``<namespace>__<group>`` 的命名分隔符（见模块文档；与前端展示约定一致）
GROUP_SEP = "__"

#: 指标名 → 单位（写 ``experiment_metrics.metric_unit``）
UNIT_BY_METRIC: dict[str, str] = {
    "accuracy": "ratio",
    "answer_consistency": "ratio",
    "agreement_rate": "ratio",
    "accuracy_delta": "ratio",
    "accuracy_spread": "ratio",
    "samples_correct": "count",
    "samples_total": "count",
    "cost_usd": "usd",
    "cost_delta": "usd",
    "latency_ms": "ms",
    "duration_ms": "ms",
    "latency_delta_ms": "ms",
    "cost_per_correct_usd": "usd",
}

#: 从答案中剥掉的包装（提示词要求「答案：X」时模型可能带前缀）
_ANSWER_PREFIX = re.compile(r"^\s*(答案|answer|输出|结果)\s*[:：]\s*", re.IGNORECASE)
_PUNCT = re.compile(r"[\s\u3000]+")
_TRAILING_PUNCT = re.compile(r"[。．.,，;；:：!！?？\"'“”‘’()（）\[\]【】]+$")


def normalize_answer(text: Any) -> str:
    """答案归一（判分口径，**对预测与参考答案一视同仁**）。

    步骤：全角→半角 → 去掉「答案：」前缀 → 去掉包裹符号与首尾标点 → 小写 → 压缩空白。
    不做同义词改写、不做容错匹配（避免把错答判成对答）。
    """
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).strip()
    value = _ANSWER_PREFIX.sub("", value).strip()
    value = value.strip("`*_# ")
    value = _TRAILING_PUNCT.sub("", value).strip()
    value = _PUNCT.sub(" ", value).strip()
    return value.lower()


def _as_number(text: str) -> float | None:
    """把纯数字答案转成 float（``10000`` 与 ``10000.0`` 视为同一答案）。"""
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def answers_match(prediction: Any, reference: Any) -> bool:
    """判分：归一后字符串相等；两侧都是数字时按数值相等。"""
    left, right = normalize_answer(prediction), normalize_answer(reference)
    if not left or not right:
        return False
    if left == right:
        return True
    left_num, right_num = _as_number(left.replace(",", "")), _as_number(right.replace(",", ""))
    if left_num is not None and right_num is not None:
        return abs(left_num - right_num) <= 1e-9
    return False


# --------------------------------------------------------------------------- #
# 纯统计函数（可单测、可复算）
# --------------------------------------------------------------------------- #
def accuracy(rows: Sequence[Mapping[str, Any]], *, prediction_key: str = "prediction", reference_key: str = "reference") -> dict[str, Any]:
    """准确率（逐样本真实比较；分母为实际参与判分的样本数）。"""
    correct = 0
    scored = 0
    for row in rows:
        reference = row.get(reference_key)
        prediction = row.get(prediction_key)
        if reference is None or reference == "":
            continue
        scored += 1
        if answers_match(prediction, reference):
            correct += 1
    return {
        "correct": correct,
        "scored": scored,
        "accuracy": (correct / scored) if scored else None,
    }


def pairwise_agreement(
    predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """一致性：两组预测在同样本上的归一答案相同比例（两两组合后取均值）。

    :param predictions: ``{group: {sample_id: prediction}}``
    :return: ``{pairs: {f"{a}|{b}": {"agreed": n, "compared": m, "agreement": r}},
              "mean_agreement": r|None}``
    """
    groups = sorted(predictions)
    pairs: dict[str, dict[str, Any]] = {}
    values: list[float] = []
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            left_rows, right_rows = predictions[left], predictions[right]
            shared = sorted(set(left_rows) & set(right_rows))
            agreed = sum(
                1
                for key in shared
                if normalize_answer(left_rows[key]) == normalize_answer(right_rows[key])
            )
            ratio = (agreed / len(shared)) if shared else None
            pairs[f"{left}|{right}"] = {
                "agreed": agreed,
                "compared": len(shared),
                "agreement": ratio,
            }
            if ratio is not None:
                values.append(ratio)
    return {
        "pairs": pairs,
        "mean_agreement": (sum(values) / len(values)) if values else None,
    }


def mean_of(values: Iterable[float | int | None]) -> float | None:
    """均值（跳过 None；**全空返回 None，不用 0 冒充**）。"""
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def sum_of(values: Iterable[float | int | None]) -> float | None:
    """求和（跳过 None；全空返回 None）。"""
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    if not numbers:
        return None
    return sum(numbers)


# --------------------------------------------------------------------------- #
# 指标装配
# --------------------------------------------------------------------------- #
def metric_key(namespace: str, group: str | None = None) -> str:
    """指标名：``accuracy__v1_zeroshot``（无 group 时退化为 namespace）。"""
    return f"{namespace}{GROUP_SEP}{group}" if group else namespace


def unit_of(metric_name: str) -> str | None:
    """指标单位：按去掉 group 后的命名空间查表（未知即 None，不猜）。"""
    return UNIT_BY_METRIC.get(str(metric_name).split(GROUP_SEP)[0])


def build_rows(
    metrics: Mapping[str, Any],
    *,
    meta: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把指标 dict 转成 ``experiment_metrics`` 行（非数值项不落数值行）。"""
    rows: list[dict[str, Any]] = []
    shared = dict(meta or {})
    for name, value in metrics.items():
        if isinstance(value, bool) or value is None:
            continue
        if not isinstance(value, (int, float)):
            continue
        group = str(name).split(GROUP_SEP, 1)[1] if GROUP_SEP in str(name) else None
        rows.append(
            {
                "metric_name": str(name)[:64],
                "metric_value": Decimal(str(round(float(value), 6))),
                "metric_unit": unit_of(name),
                "extra": {**shared, "metric_group": group},
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #
async def _session_or_new(session: Any) -> tuple[Any, bool]:
    if session is not None:
        return session, False
    from app.db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise RuntimeError("数据库会话不可用（DATABASE_URL 未就绪）")
    return AsyncSessionLocal(), True


async def persist_run_metrics(
    *,
    run_id: int,
    metrics: Mapping[str, Any],
    meta: Mapping[str, Any] | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """把指标写进 ``experiment_metrics``（**先删同 run 旧行**，保证幂等重放）。"""
    from sqlalchemy import delete

    from app.db.models.pipeline import ExperimentMetric

    own_session, should_close = await _session_or_new(session)
    rows = build_rows(metrics, meta=meta)
    try:
        await own_session.execute(
            delete(ExperimentMetric).where(ExperimentMetric.experiment_run_id == int(run_id))
        )
        for row in rows:
            own_session.add(ExperimentMetric(experiment_run_id=int(run_id), **row))
        await own_session.commit()
    finally:
        if should_close:
            await own_session.close()
    logger.info("实验指标已落库 run=%s rows=%s", run_id, len(rows))
    return {"run_id": int(run_id), "rows": len(rows), "metric_names": [row["metric_name"] for row in rows]}


async def load_run_metrics(*, run_id: int, session: Any = None) -> list[dict[str, Any]]:
    """读取某 Run 的指标（``GET /experiments/runs/{run_id}/metrics``）。"""
    from sqlalchemy import select

    from app.db.models.pipeline import ExperimentMetric

    own_session, should_close = await _session_or_new(session)
    try:
        rows = (
            (
                await own_session.execute(
                    select(ExperimentMetric)
                    .where(ExperimentMetric.experiment_run_id == int(run_id))
                    .order_by(ExperimentMetric.id)
                )
            )
            .scalars()
            .all()
        )
    finally:
        if should_close:
            await own_session.close()
    return [
        {
            "id": int(row.id),
            "metric_name": row.metric_name,
            "metric_value": float(row.metric_value) if row.metric_value is not None else None,
            "metric_unit": row.metric_unit,
            "extra": row.extra,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def backfill_stage_output(
    *,
    pipeline_run_id: int | None,
    stage: str,
    attempt: int,
    block: Mapping[str, Any],
    session: Any = None,
) -> dict[str, Any]:
    """把实验摘要合并进 ``stage_outputs.output_json``（供看板展示；并发安全）。

    无 ``pipeline_run_id``（独立跑实验）时如实跳过，不伪造 stage_outputs 行。
    """
    if not pipeline_run_id:
        return {"updated": False, "reason": "no_pipeline_run"}

    from sqlalchemy import select

    from app.db.models.pipeline import StageOutput

    own_session, should_close = await _session_or_new(session)
    try:
        row = (
            await own_session.execute(
                select(StageOutput).where(
                    StageOutput.pipeline_run_id == int(pipeline_run_id),
                    StageOutput.stage == str(stage),
                    StageOutput.attempt == int(attempt),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            logger.warning(
                "stage_outputs 行不存在，跳过回填 pipeline_run=%s stage=%s attempt=%s",
                pipeline_run_id,
                stage,
                attempt,
            )
            return {"updated": False, "reason": "stage_output_missing"}
        merged = dict(row.output_json or {})
        merged["experiment"] = dict(block)
        row.output_json = merged
        await own_session.commit()
    finally:
        if should_close:
            await own_session.close()
    return {"updated": True, "pipeline_run_id": int(pipeline_run_id), "stage": stage}


# --------------------------------------------------------------------------- #
# 成本：从真实 llm_call_logs 汇总（禁止估算）
# --------------------------------------------------------------------------- #
async def collect_run_llm_logs(
    *,
    stage: str,
    purpose_prefix: str,
    project_id: int | None,
    log_id_after: int | None = None,
    log_id_until: int | None = None,
    session: Any = None,
) -> dict[str, Any]:
    """按「环节 + purpose 前缀 + id 窗口」汇总本 Run 的真实调用日志。

    :return: ``{calls, cost_usd, cost_complete, is_replay, by_model:{ref:{...}},
                log_id_range:{after,until}, unknown_cost_calls, notes}``
    """
    from sqlalchemy import select

    from app.db.models.review import LlmCallLog

    own_session, should_close = await _session_or_new(session)
    try:
        stmt = select(LlmCallLog).where(LlmCallLog.stage == stage)
        if log_id_after:
            stmt = stmt.where(LlmCallLog.id > int(log_id_after))
        if log_id_until:
            stmt = stmt.where(LlmCallLog.id <= int(log_id_until))
        if project_id is None:
            stmt = stmt.where(LlmCallLog.project_id.is_(None))
        else:
            stmt = stmt.where(LlmCallLog.project_id == int(project_id))
        rows = (await own_session.execute(stmt.order_by(LlmCallLog.id))).scalars().all()
    finally:
        if should_close:
            await own_session.close()

    prefix = str(purpose_prefix or "")
    matched = [row for row in rows if str(row.purpose or "").startswith(prefix)]
    by_model: dict[str, dict[str, Any]] = {}
    by_purpose: dict[str, dict[str, Any]] = {}
    total = 0.0
    unknown = 0
    replay_calls = 0
    for row in matched:
        ref = f"{row.provider}:{row.model}"
        bucket = by_model.setdefault(
            ref, {"calls": 0, "cost_usd": 0.0, "latency_ms_total": 0, "unknown_cost_calls": 0}
        )
        bucket["calls"] += 1
        bucket["latency_ms_total"] += int(row.duration_ms or 0)
        purpose_bucket = by_purpose.setdefault(
            str(row.purpose or ""),
            {"calls": 0, "cost_usd": 0.0, "latency_ms_total": 0, "unknown_cost_calls": 0},
        )
        purpose_bucket["calls"] += 1
        purpose_bucket["latency_ms_total"] += int(row.duration_ms or 0)
        if row.cost_usd is None:
            unknown += 1
            bucket["unknown_cost_calls"] += 1
            purpose_bucket["unknown_cost_calls"] += 1
        else:
            bucket["cost_usd"] = round(bucket["cost_usd"] + float(row.cost_usd), 6)
            purpose_bucket["cost_usd"] = round(purpose_bucket["cost_usd"] + float(row.cost_usd), 6)
            total += float(row.cost_usd)
        if row.is_replay:
            replay_calls += 1
    for bucket in list(by_model.values()) + list(by_purpose.values()):
        # 该分组只要有调用缺单价，分组成本就是**未知**而不是已统计到的那部分：
        # 写成部分和会让「未知成本」被读成「真实成本」
        bucket["cost_usd"] = (
            round(bucket["cost_usd"], 6)
            if bucket["calls"] and not bucket["unknown_cost_calls"]
            else None
        )
        bucket["latency_ms_mean"] = (
            round(bucket["latency_ms_total"] / bucket["calls"], 2) if bucket["calls"] else None
        )
    notes: list[str] = []
    if unknown:
        notes.append(
            f"{unknown} 次调用缺少单价（llm_call_logs.cost_usd IS NULL）：成本不完整，未做估算"
        )
    return {
        "calls": len(matched),
        # 任一次调用缺单价 → 总成本**未知**（置 null）。禁止用「已统计到的部分和」冒充总成本，
        # 更禁止用 0.0 冒充「真实零成本」：Passport 会据此判 cost_usd 缺失 → status=incomplete。
        "cost_usd": round(total, 6) if matched and not unknown else None,
        "cost_complete": unknown == 0 and bool(matched),
        "unknown_cost_calls": unknown,
        "is_replay_calls": replay_calls,
        "by_model": by_model,
        "by_purpose": by_purpose,
        "log_id_range": {"after": log_id_after, "until": log_id_until},
        "purpose_prefix": prefix,
        "stage": stage,
        "notes": notes,
    }


async def current_max_log_id(*, session: Any = None) -> int:
    """当前 ``llm_call_logs`` 最大 id（作为本 Run 的成本统计窗口起点）。"""
    from sqlalchemy import func, select

    from app.db.models.review import LlmCallLog

    own_session, should_close = await _session_or_new(session)
    try:
        value = (await own_session.execute(select(func.max(LlmCallLog.id)))).scalar()
    finally:
        if should_close:
            await own_session.close()
    return int(value or 0)


__all__ = [
    "GROUP_SEP",
    "UNIT_BY_METRIC",
    "accuracy",
    "answers_match",
    "backfill_stage_output",
    "build_rows",
    "collect_run_llm_logs",
    "current_max_log_id",
    "load_run_metrics",
    "mean_of",
    "metric_key",
    "normalize_answer",
    "pairwise_agreement",
    "persist_run_metrics",
    "sum_of",
    "unit_of",
]
