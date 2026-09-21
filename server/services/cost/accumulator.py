# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""成本累计服务（WP02 独占）。

双线口径（contracts.guardrails.cost）：

- **护栏值 ``limit_usd = 8.0``**：硬熔断线，超出必须中断（由 WP10 判定并熔断）
- **演示配额 ``quota_usd = 3.0``**：只告警不熔断，用于演示时提醒

``used_usd`` **只累计真实调用**。判定与排除规则见
:mod:`services.cost.classification`：

1. ``is_replay = true``（``demo_fixtures`` 回放，未出网）→ ``replay_saved_usd``
2. ``provider`` / ``model`` / ``purpose`` 命中 ``stub|mock|fake|replay|acceptance|fixture|synthetic``
   → ``stub_saved_usd``

被排除的调用**原始 ``llm_call_logs.cost_usd`` 行原样保留**（可审计，禁止删改），
仅改变聚合口径；两类金额都不参与 ``limit_exceeded`` / ``quota_exceeded`` 判定。

对外提供两个面：

- HTTP：``GET /api/v1/costs/summary?project_id=``（见 :mod:`api.v1.costs`）
- Python：:func:`accumulate` / :func:`check_cost` / :func:`get_used_usd`，供 WP10 护栏短路判定直接调用
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llm.store import LLMStore, get_store
from services.cost.classification import (
    CLASS_REAL,
    CLASS_REPLAY,
    CLASS_STUB,
    classify_call,
    nonbillable_markers,
)
from services.cost.queries import fetch_cost_buckets

#: 契约默认值（附录 E.2 / contracts.guardrails.cost）
DEFAULT_LIMIT_USD = 8.0
DEFAULT_QUOTA_USD = 3.0


def _round(value: float | None) -> float:
    return round(float(value or 0.0), 6)


def _notes() -> str:
    return (
        "成本口径：used_usd 只累计真实调用（is_replay=false 且 provider/model/purpose 不含 "
        "非真实标记 " + ("|".join(nonbillable_markers()) or "（空）") + "）。"
        "桩/验收夹具调用与回放调用保留原始 cost_usd 行以便审计，分别汇入 "
        "stub_saved_usd/stub_calls 与 replay_saved_usd/replay_calls，不计入 used_usd，"
        "也不参与 limit_exceeded / quota_exceeded 判定。"
        "定价币种不是 USD 的调用（见 non_usd_calls/non_usd_models/non_usd_currencies）"
        "不做汇率换算，cost_usd 为 null，同样不计入 used_usd。"
    )


@dataclass
class CostSummary:
    """成本汇总（``GET /costs/summary`` 的响应体）。"""

    used_usd: float = 0.0
    limit_usd: float = DEFAULT_LIMIT_USD
    quota_usd: float = DEFAULT_QUOTA_USD
    quota_exceeded: bool = False
    limit_exceeded: bool = False
    breakdown_by_stage: dict[str, float] = field(default_factory=dict)
    replay_saved_usd: float = 0.0
    #: 桩/验收夹具调用（非真实）：金额与次数，不计入 used_usd
    stub_saved_usd: float = 0.0
    stub_calls: int = 0
    #: 按 provider 的可追溯分解（含被排除部分，用于核对原始行）
    breakdown_by_provider: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 被排除部分按环节的分解（traceability）
    stub_breakdown_by_stage: dict[str, float] = field(default_factory=dict)
    replay_breakdown_by_stage: dict[str, float] = field(default_factory=dict)
    #: 定价币种不是 USD 的调用：**不做汇率换算**，cost_usd 为 null，未计入 used_usd。
    #: 前端据此显示「非 USD，未计入护栏」徽标
    non_usd_calls: int = 0
    #: 出现过的非 USD 定价模型（``provider:model``，去重排序）
    non_usd_models: list[str] = field(default_factory=list)
    #: 出现过的非 USD 币种（去重排序，例如 ``["CNY"]``）
    non_usd_currencies: list[str] = field(default_factory=list)
    notes: str = ""
    #: 附加审计信息（不属于契约必填字段）
    project_id: int | None = None
    calls: int = 0
    replay_calls: int = 0
    total_calls: int = 0
    failed_calls: int = 0
    unknown_price_calls: int = 0
    cost_complete: bool = True
    warning: str | None = None
    currency: str = "USD"
    source: str = "llm_call_logs"

    def to_dict(self) -> dict[str, Any]:
        return {
            # 契约必填字段（保持兼容）
            "used_usd": self.used_usd,
            "limit_usd": self.limit_usd,
            "quota_usd": self.quota_usd,
            "quota_exceeded": self.quota_exceeded,
            "limit_exceeded": self.limit_exceeded,
            "breakdown_by_stage": self.breakdown_by_stage,
            "replay_saved_usd": self.replay_saved_usd,
            # 本轮扩展字段
            "stub_saved_usd": self.stub_saved_usd,
            "stub_calls": self.stub_calls,
            "breakdown_by_provider": self.breakdown_by_provider,
            "stub_breakdown_by_stage": self.stub_breakdown_by_stage,
            "replay_breakdown_by_stage": self.replay_breakdown_by_stage,
            # 非 USD 定价分桶（不改动任何既有 USD 字段的语义）
            "non_usd_calls": self.non_usd_calls,
            "non_usd_models": self.non_usd_models,
            "non_usd_currencies": self.non_usd_currencies,
            "notes": self.notes or _notes(),
            # 审计信息
            "project_id": self.project_id,
            "calls": self.calls,
            "replay_calls": self.replay_calls,
            "total_calls": self.total_calls,
            "failed_calls": self.failed_calls,
            "unknown_price_calls": self.unknown_price_calls,
            "cost_complete": self.cost_complete,
            "warning": self.warning,
            "currency": self.currency,
            "source": self.source,
        }


@dataclass
class CostCheck:
    """护栏判定结果（WP10 的 ``cost_ok`` 直接用 :attr:`ok`）。"""

    ok: bool
    used_usd: float
    limit_usd: float
    estimated_usd: float = 0.0
    remaining_usd: float = 0.0
    quota_usd: float = DEFAULT_QUOTA_USD
    quota_exceeded: bool = False
    limit_exceeded: bool = False
    reason: str | None = None
    cost_complete: bool = True
    #: 口径证据：被排除的非真实调用（不计入 used_usd）
    stub_saved_usd: float = 0.0
    stub_calls: int = 0
    replay_saved_usd: float = 0.0
    replay_calls: int = 0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_ok": self.ok,
            "used_usd": self.used_usd,
            "estimated_usd": self.estimated_usd,
            "limit_usd": self.limit_usd,
            "remaining_usd": self.remaining_usd,
            "quota_usd": self.quota_usd,
            "quota_exceeded": self.quota_exceeded,
            "limit_exceeded": self.limit_exceeded,
            "cost_complete": self.cost_complete,
            "reason": self.reason,
            "stub_saved_usd": self.stub_saved_usd,
            "stub_calls": self.stub_calls,
            "replay_saved_usd": self.replay_saved_usd,
            "replay_calls": self.replay_calls,
            "notes": self.notes or _notes(),
        }


def default_limits() -> tuple[float, float]:
    """从配置读取 (护栏值, 演示配额)，配置不可用时回落到契约默认值。"""
    try:
        from core.config import get_settings

        settings = get_settings()
        return (
            float(getattr(settings, "pipeline_max_llm_cost_usd", DEFAULT_LIMIT_USD)),
            float(getattr(settings, "pipeline_demo_cost_quota_usd", DEFAULT_QUOTA_USD)),
        )
    except Exception:  # pragma: no cover - 配置未就绪
        return DEFAULT_LIMIT_USD, DEFAULT_QUOTA_USD


async def accumulate(
    project_id: int | None = None,
    *,
    store: LLMStore | None = None,
    limit_usd: float | None = None,
    quota_usd: float | None = None,
) -> CostSummary:
    """累计某项目的成本与双线状态（``used_usd`` 仅真实调用）。"""
    active_store = store or get_store()
    default_limit, default_quota = default_limits()
    limit = default_limit if limit_usd is None else float(limit_usd)
    quota = default_quota if quota_usd is None else float(quota_usd)

    buckets = await fetch_cost_buckets(project_id, store=active_store)

    breakdown: dict[str, float] = {}
    stub_by_stage: dict[str, float] = {}
    replay_by_stage: dict[str, float] = {}
    by_provider: dict[str, dict[str, Any]] = {}
    used = 0.0
    stub_saved = 0.0
    replay_saved = 0.0
    real_calls = stub_calls = replay_calls = total_calls = 0
    failed_calls = unknown = 0
    non_usd_calls = 0
    non_usd_models: set[str] = set()
    non_usd_currencies: set[str] = set()

    for bucket in buckets:
        verdict = classify_call(
            provider=bucket.provider,
            model=bucket.model,
            is_replay=bucket.is_replay,
        )
        stage_key = bucket.stage or "unassigned"
        provider_key = bucket.provider or "unknown"
        entry = by_provider.setdefault(
            provider_key,
            {
                "used_usd": 0.0,
                "stub_saved_usd": 0.0,
                "replay_saved_usd": 0.0,
                "calls": 0,
                "real_calls": 0,
                "stub_calls": 0,
                "replay_calls": 0,
                "models": [],
                "classifications": [],
                "counts_toward_used_usd": True,
                "non_usd_calls": 0,
                "non_usd_currencies": [],
            },
        )
        entry["calls"] += bucket.calls
        entry["models"].append(bucket.model or "unknown")
        if verdict.classification not in entry["classifications"]:
            entry["classifications"].append(verdict.classification)
        total_calls += bucket.calls

        if bucket.non_usd_calls:
            # 非 USD 定价：不换算汇率、不计入 used_usd，只计数并标注币种/模型
            non_usd_calls += bucket.non_usd_calls
            non_usd_models.add(f"{bucket.provider or 'unknown'}:{bucket.model or 'unknown'}")
            non_usd_currencies.update(bucket.non_usd_currencies)
            entry["non_usd_calls"] = entry.get("non_usd_calls", 0) + bucket.non_usd_calls
            entry["non_usd_currencies"] = sorted(
                set(entry.get("non_usd_currencies", [])) | set(bucket.non_usd_currencies)
            )

        if verdict.classification == CLASS_REAL:
            used += bucket.cost_usd
            real_calls += bucket.calls
            breakdown[stage_key] = _round(breakdown.get(stage_key, 0.0) + bucket.cost_usd)
            failed_calls += bucket.failed_calls
            unknown += bucket.unknown_price_calls
            entry["used_usd"] = _round(entry["used_usd"] + bucket.cost_usd)
            entry["real_calls"] += bucket.calls
        elif verdict.classification == CLASS_REPLAY:
            replay_saved += bucket.cost_usd
            replay_calls += bucket.calls
            replay_by_stage[stage_key] = _round(replay_by_stage.get(stage_key, 0.0) + bucket.cost_usd)
            entry["replay_saved_usd"] = _round(entry["replay_saved_usd"] + bucket.cost_usd)
            entry["replay_calls"] += bucket.calls
            entry["counts_toward_used_usd"] = False
        elif verdict.classification == CLASS_STUB:
            stub_saved += bucket.cost_usd
            stub_calls += bucket.calls
            stub_by_stage[stage_key] = _round(stub_by_stage.get(stage_key, 0.0) + bucket.cost_usd)
            entry["stub_saved_usd"] = _round(entry["stub_saved_usd"] + bucket.cost_usd)
            entry["stub_calls"] += bucket.calls
            entry["counts_toward_used_usd"] = False

    used = _round(used)
    for entry in by_provider.values():
        entry["used_usd"] = _round(entry["used_usd"])
        entry["stub_saved_usd"] = _round(entry["stub_saved_usd"])
        entry["replay_saved_usd"] = _round(entry["replay_saved_usd"])
        entry["models"] = sorted(set(entry["models"]))
        # 仅当该 provider 的全部调用都是真实调用时，才整体计入 used_usd
        entry["counts_toward_used_usd"] = entry["classifications"] == [CLASS_REAL]

    summary = CostSummary(
        used_usd=used,
        limit_usd=limit,
        quota_usd=quota,
        quota_exceeded=used > quota,
        limit_exceeded=used > limit,
        breakdown_by_stage=breakdown,
        replay_saved_usd=_round(replay_saved),
        stub_saved_usd=_round(stub_saved),
        stub_calls=stub_calls,
        breakdown_by_provider=by_provider,
        stub_breakdown_by_stage=stub_by_stage,
        replay_breakdown_by_stage=replay_by_stage,
        notes=_notes(),
        project_id=project_id,
        calls=real_calls,
        replay_calls=replay_calls,
        total_calls=total_calls,
        failed_calls=failed_calls,
        unknown_price_calls=unknown,
        cost_complete=unknown == 0,
        non_usd_calls=non_usd_calls,
        non_usd_models=sorted(non_usd_models),
        non_usd_currencies=sorted(non_usd_currencies),
    )
    if unknown:
        summary.warning = (
            f"{unknown} 次**真实**调用缺少单价，cost_usd 记为 null（禁止估算），"
            "实际花费可能高于 used_usd；请在设置页补齐供应商单价"
        )
    if non_usd_calls:
        summary.warning = (summary.warning + " " if summary.warning else "") + (
            f"另有 {non_usd_calls} 次真实调用使用非 USD 定价"
            f"（币种 {'/'.join(sorted(non_usd_currencies)) or '未知'}）："
            "不做汇率换算，cost_usd 记为 null，**未计入 used_usd 护栏**"
        )
    if summary.quota_exceeded and not summary.limit_exceeded:
        summary.warning = (summary.warning + " " if summary.warning else "") + (
            f"演示配额已超出（{used} > {quota} USD）：仅告警，不熔断"
        )
    return summary


async def get_used_usd(project_id: int | None = None, *, store: LLMStore | None = None) -> float:
    """实时累计成本（WP10 护栏用；不含桩与回放）。"""
    summary = await accumulate(project_id, store=store)
    return summary.used_usd


async def check_cost(
    project_id: int | None = None,
    *,
    estimated_usd: float = 0.0,
    store: LLMStore | None = None,
    limit_usd: float | None = None,
    quota_usd: float | None = None,
) -> CostCheck:
    """护栏判定：``真实累计成本 + 本次预估 <= 护栏值``。

    判定顺序契约（附录 E.2）中 ``cost_ok`` 排在 ``safety`` 与 ``time`` 之后，
    本函数只回答成本一项，不做短路编排（编排归 WP10）。

    与 ``/costs/summary`` 同口径：桩/验收夹具调用与回放调用不计入 ``used_usd``，
    因此不会再被本地桩的历史记账误触发熔断。
    """
    summary = await accumulate(project_id, store=store, limit_usd=limit_usd, quota_usd=quota_usd)
    projected = _round(summary.used_usd + max(0.0, float(estimated_usd)))
    ok = projected <= summary.limit_usd
    check = CostCheck(
        ok=ok,
        used_usd=summary.used_usd,
        limit_usd=summary.limit_usd,
        estimated_usd=round(float(estimated_usd), 6),
        remaining_usd=_round(max(0.0, summary.limit_usd - summary.used_usd)),
        quota_usd=summary.quota_usd,
        quota_exceeded=summary.used_usd > summary.quota_usd,
        limit_exceeded=projected > summary.limit_usd,
        cost_complete=summary.cost_complete,
        stub_saved_usd=summary.stub_saved_usd,
        stub_calls=summary.stub_calls,
        replay_saved_usd=summary.replay_saved_usd,
        replay_calls=summary.replay_calls,
        notes=summary.notes,
    )
    excluded = _round(summary.stub_saved_usd + summary.replay_saved_usd)
    if not ok:
        check.reason = (
            f"成本护栏熔断：真实累计 {summary.used_usd} + 预估 {check.estimated_usd} "
            f"= {projected} USD 超过护栏值 {summary.limit_usd} USD"
        )
    elif check.quota_exceeded:
        check.reason = (
            f"演示配额告警：真实累计 {summary.used_usd} USD 已超过配额 {summary.quota_usd} USD（不熔断）"
        )
    else:
        check.reason = (
            f"成本护栏通过：真实累计 {summary.used_usd} + 预估 {check.estimated_usd} "
            f"= {projected} USD <= 护栏值 {summary.limit_usd} USD；"
            f"非真实调用（桩 {summary.stub_calls} 次 / 回放 {summary.replay_calls} 次，"
            f"合计 {excluded} USD）按口径不计入"
        )
    if not summary.cost_complete:
        check.reason = (check.reason or "") + (
            f"；注意：{summary.unknown_price_calls} 次真实调用缺单价，实际花费可能更高"
        )
    return check


async def guardrail_snapshot(project_id: int | None = None, *, store: LLMStore | None = None) -> dict[str, Any]:
    """SSE ``guardrail`` 事件载荷（``{type,used_usd,limit_usd,quota_usd,ok}``）。"""
    summary = await accumulate(project_id, store=store)
    return {
        "type": "cost",
        "used_usd": summary.used_usd,
        "limit_usd": summary.limit_usd,
        "quota_usd": summary.quota_usd,
        "quota_exceeded": summary.quota_exceeded,
        "stub_saved_usd": summary.stub_saved_usd,
        "stub_calls": summary.stub_calls,
        "replay_saved_usd": summary.replay_saved_usd,
        "ok": not summary.limit_exceeded,
    }


__all__ = [
    "DEFAULT_LIMIT_USD",
    "DEFAULT_QUOTA_USD",
    "CostCheck",
    "CostSummary",
    "accumulate",
    "check_cost",
    "default_limits",
    "get_used_usd",
    "guardrail_snapshot",
]
