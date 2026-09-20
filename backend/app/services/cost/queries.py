# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``llm_call_logs`` 分类分桶查询（WP02 成本口径）。

原 ``app.llm.store.aggregate_costs`` 只按 ``stage`` 聚合、只区分 ``is_replay``，
无法按 ``provider/model`` 剔除桩调用（表结构无 ``is_stub`` 列，见 WP17 findings）。
本模块**只读**地按 ``(stage, provider, model, is_replay)`` 分桶，把 provider/model
原样带出，交给 :mod:`app.services.cost.classification` 判定口径。

- 生产路径：SQL（复用 :func:`app.llm.db.get_async_engine` 的连接池）
- 测试路径：``InMemoryLLMStore`` 等带 ``.logs`` 的实现，内存内分桶

本模块不执行任何 INSERT / UPDATE / DELETE。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.llm.store import LLMStore, get_store

#: 按 (stage, provider, model, is_replay) 分桶的只读聚合
#:
#: ``non_usd_*`` 两列识别「定价币种不是 USD」的行：这类行由 ``pricing.py`` 写成
#: ``cost_usd = NULL`` + ``error LIKE '%non_usd_currency:%'``，因此**本来就不会**
#: 进入 ``SUM(cost_usd)``（NULL 不参与求和），USD 总额天然不受影响。
#: ``unknown_price_calls`` 显式排除它们，避免把「非 USD 定价」误报成「缺单价」。
_BUCKET_SQL = """
SELECT
    stage,
    provider,
    model,
    is_replay,
    COALESCE(SUM(cost_usd), 0)                                      AS cost_usd,
    COUNT(*)                                                        AS calls,
    COUNT(*) FILTER (WHERE success IS FALSE)                        AS failed_calls,
    COUNT(*) FILTER (
        WHERE success IS TRUE
          AND cost_usd IS NULL
          AND error NOT LIKE '%non_usd_currency:%'
    )                                                               AS unknown_price_calls,
    COUNT(*) FILTER (WHERE error LIKE '%non_usd_currency:%')        AS non_usd_calls,
    string_agg(DISTINCT substring(error from 'non_usd_currency:([A-Za-z]+)'), ',')
        FILTER (WHERE error LIKE '%non_usd_currency:%')             AS non_usd_currencies
FROM llm_call_logs
WHERE (CAST(:project_id AS BIGINT) IS NULL OR project_id = CAST(:project_id AS BIGINT))
GROUP BY stage, provider, model, is_replay
ORDER BY stage NULLS FIRST, provider NULLS FIRST, model NULLS FIRST, is_replay
"""


class _HasLogs(Protocol):
    """内存实现的最小契约（``InMemoryLLMStore.logs``）。"""

    logs: list[dict[str, Any]]


@dataclass
class CostBucket:
    """一个 (stage, provider, model, is_replay) 组合的原始成本聚合。"""

    stage: str | None
    provider: str | None
    model: str | None
    is_replay: bool
    cost_usd: float
    calls: int
    failed_calls: int
    unknown_price_calls: int
    #: 该桶中「定价币种非 USD」的调用数（cost_usd 为 NULL，未计入 USD 汇总）
    non_usd_calls: int = 0
    #: 该桶出现过的非 USD 币种（去重）
    non_usd_currencies: list[str] = field(default_factory=list)


_NON_USD_MARK = "non_usd_currency:"


def _non_usd_currencies(text: Any) -> list[str]:
    """从 ``error`` 文本里抠出 ``non_usd_currency:<币种>`` 的币种。"""
    content = str(text or "")
    if _NON_USD_MARK not in content:
        return []
    found: list[str] = []
    for chunk in content.split(_NON_USD_MARK)[1:]:
        token = ""
        for char in chunk:
            if char.isalpha():
                token += char
            else:
                break
        if token and token not in found:
            found.append(token)
    return found


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _buckets_from_logs(logs: list[dict[str, Any]], project_id: int | None) -> list[CostBucket]:
    """内存分桶（口径与 SQL 完全一致，供单元测试与无 DB 环境使用）。"""
    buckets: dict[tuple[Any, Any, Any, bool], CostBucket] = {}
    for row in logs:
        if project_id is not None and row.get("project_id") != project_id:
            continue
        key = (
            row.get("stage"),
            row.get("provider"),
            row.get("model"),
            bool(row.get("is_replay")),
        )
        bucket = buckets.get(key)
        if bucket is None:
            bucket = CostBucket(key[0], key[1], key[2], key[3], 0.0, 0, 0, 0)
            buckets[key] = bucket
        bucket.calls += 1
        if row.get("cost_usd") is not None:
            bucket.cost_usd += _as_float(row.get("cost_usd"))
        currencies = _non_usd_currencies(row.get("error"))
        if currencies:
            bucket.non_usd_calls += 1
            for currency in currencies:
                if currency not in bucket.non_usd_currencies:
                    bucket.non_usd_currencies.append(currency)
        if not row.get("success"):
            bucket.failed_calls += 1
        elif row.get("cost_usd") is None and not currencies:
            bucket.unknown_price_calls += 1
    return list(buckets.values())


async def _buckets_from_sql(project_id: int | None) -> list[CostBucket]:
    from sqlalchemy import text

    from app.llm.db import get_async_engine

    engine = get_async_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text(_BUCKET_SQL), {"project_id": project_id})
        rows = result.mappings().all()
    return [
        CostBucket(
            stage=row["stage"],
            provider=row["provider"],
            model=row["model"],
            is_replay=bool(row["is_replay"]),
            cost_usd=_as_float(row["cost_usd"]),
            calls=int(row["calls"]),
            failed_calls=int(row["failed_calls"]),
            unknown_price_calls=int(row["unknown_price_calls"]),
            non_usd_calls=int(row["non_usd_calls"] or 0),
            non_usd_currencies=[
                item for item in str(row["non_usd_currencies"] or "").split(",") if item
            ],
        )
        for row in rows
    ]


async def fetch_cost_buckets(
    project_id: int | None = None,
    *,
    store: LLMStore | None = None,
) -> list[CostBucket]:
    """取出分类所需的成本分桶（只读）。"""
    active = store if store is not None else get_store()
    logs = getattr(active, "logs", None)
    if isinstance(logs, list):
        return _buckets_from_logs(logs, project_id)
    return await _buckets_from_sql(project_id)


async def audit_cost_rows(
    project_id: int | None = None,
    *,
    store: LLMStore | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """一次性重算/校验：逐桶给出「原始 cost_usd 之和」与「口径归类」的对照。

    输出足以解释 ``used_usd`` 的每一个来源，且不依赖任何写入操作。
    相同 project_id 下 ``sum(bucket.cost_usd)`` 恒等于库内该项目的
    ``llm_call_logs.cost_usd`` 之和（口径切换不影响原始行）。
    """
    from app.services.cost.classification import classification_rules, classify_call

    buckets = await fetch_cost_buckets(project_id, store=store)
    items: list[dict[str, Any]] = []
    totals = {"raw_usd": 0.0, "used_usd": 0.0, "stub_saved_usd": 0.0, "replay_saved_usd": 0.0}
    calls = {"total": 0, "real": 0, "stub": 0, "replay": 0}
    for bucket in buckets:
        verdict = classify_call(
            provider=bucket.provider,
            model=bucket.model,
            is_replay=bucket.is_replay,
        )
        totals["raw_usd"] += bucket.cost_usd
        calls["total"] += bucket.calls
        if verdict.classification == "real":
            totals["used_usd"] += bucket.cost_usd
            calls["real"] += bucket.calls
        elif verdict.classification == "replay":
            totals["replay_saved_usd"] += bucket.cost_usd
            calls["replay"] += bucket.calls
        else:
            totals["stub_saved_usd"] += bucket.cost_usd
            calls["stub"] += bucket.calls
        items.append(
            {
                "stage": bucket.stage,
                "provider": bucket.provider,
                "model": bucket.model,
                "is_replay": bucket.is_replay,
                "calls": bucket.calls,
                "raw_cost_usd": round(bucket.cost_usd, 6),
                "non_usd_calls": bucket.non_usd_calls,
                "non_usd_currencies": list(bucket.non_usd_currencies),
                "classification": verdict.classification,
                "matched": verdict.reason,
                "counts_toward_used_usd": verdict.billable,
            }
        )
    items.sort(key=lambda item: (-item["raw_cost_usd"], item["provider"] or "", item["model"] or ""))
    return {
        "project_id": project_id,
        "rules": classification_rules(),
        "buckets": len(items),
        "calls": calls,
        "totals_usd": {key: round(value, 6) for key, value in totals.items()},
        "items": items[: max(1, limit)],
        "truncated": len(items) > max(1, limit),
    }


__all__ = ["CostBucket", "audit_cost_rows", "fetch_cost_buckets"]
