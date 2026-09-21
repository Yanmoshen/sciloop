# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``llm_call_logs`` 与 ``demo_fixtures`` 的存取。

本模块**刻意不依赖 WP01 的 ORM 模型类**，只用 SQLAlchemy Core 原生 SQL 访问附录 A.6 已冻结的表结构，
这样 WP02 的记账链路可以在迁移脚本落地前独立验证，也避免与并行开发的模型定义互相阻塞。

代价是这里出现手写 SQL；收益是「一次 LLM 调用 → 一行 llm_call_logs」这条红线不依赖任何第三方解析层。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from llm.errors import LLMError
from llm.types import CallLogEntry

logger = logging.getLogger(__name__)

FIXTURE_TYPE_LLM_RESPONSE = "llm_response"


@dataclass
class StageCostRow:
    """按环节聚合的成本行（cost 服务据此拼 CostSummary）。"""

    stage: str | None
    real_cost_usd: float | None
    replay_cost_usd: float | None
    calls: int
    replay_calls: int
    failed_calls: int
    unknown_price_calls: int


@runtime_checkable
class LLMStore(Protocol):
    """LLM 记账与 fixture 存取接口。"""

    async def insert_call_log(self, entry: CallLogEntry) -> int | None: ...

    async def find_fixture(self, fixture_type: str, fixture_key: str) -> dict[str, Any] | None: ...

    async def upsert_fixture(
        self, fixture_type: str, fixture_key: str, payload: dict[str, Any], note: str | None = None
    ) -> None: ...

    async def aggregate_costs(self, project_id: int | None = None) -> list[StageCostRow]: ...

    async def recent_call_logs(
        self, *, project_id: int | None = None, stage: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- #
# 内存实现（单元测试 / 无 DB 的本地验证）
# --------------------------------------------------------------------------- #
class InMemoryLLMStore:
    """不落盘实现，仅供测试与本包自检脚本使用。"""

    def __init__(self) -> None:
        self.logs: list[dict[str, Any]] = []
        self.fixtures: dict[tuple[str, str], dict[str, Any]] = {}
        self._seq = 0

    async def insert_call_log(self, entry: CallLogEntry) -> int | None:
        self._seq += 1
        row = entry.to_row()
        row["id"] = self._seq
        row["model_ref"] = entry.model_ref
        self.logs.append(row)
        return self._seq

    async def find_fixture(self, fixture_type: str, fixture_key: str) -> dict[str, Any] | None:
        item = self.fixtures.get((fixture_type, fixture_key))
        return dict(item["payload"]) if item else None

    async def upsert_fixture(
        self, fixture_type: str, fixture_key: str, payload: dict[str, Any], note: str | None = None
    ) -> None:
        self.fixtures[(fixture_type, fixture_key)] = {"payload": dict(payload), "note": note}

    async def aggregate_costs(self, project_id: int | None = None) -> list[StageCostRow]:
        buckets: dict[str | None, StageCostRow] = {}
        for row in self.logs:
            if project_id is not None and row.get("project_id") != project_id:
                continue
            key = row.get("stage")
            bucket = buckets.get(key)
            if bucket is None:
                bucket = StageCostRow(key, 0.0, 0.0, 0, 0, 0, 0)
                buckets[key] = bucket
            bucket.calls += 1
            if row.get("is_replay"):
                bucket.replay_calls += 1
                if row.get("cost_usd") is not None:
                    bucket.replay_cost_usd = (bucket.replay_cost_usd or 0.0) + float(row["cost_usd"])
            else:
                if not row.get("success"):
                    bucket.failed_calls += 1
                if row.get("cost_usd") is None:
                    if row.get("success"):
                        bucket.unknown_price_calls += 1
                else:
                    bucket.real_cost_usd = (bucket.real_cost_usd or 0.0) + float(row["cost_usd"])
        return list(buckets.values())

    async def recent_call_logs(
        self, *, project_id: int | None = None, stage: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.logs
            if (project_id is None or row.get("project_id") == project_id)
            and (stage is None or row.get("stage") == stage)
        ]
        return rows[-limit:]


# --------------------------------------------------------------------------- #
# SQL 实现（PostgreSQL / 附录 A.6 表结构）
# --------------------------------------------------------------------------- #
_INSERT_LOG_SQL = """
INSERT INTO llm_call_logs
    (project_id, stage, provider, model, purpose, prompt_tokens, completion_tokens,
     cost_usd, duration_ms, success, is_replay, error)
VALUES
    (:project_id, :stage, :provider, :model, :purpose, :prompt_tokens, :completion_tokens,
     :cost_usd, :duration_ms, :success, :is_replay, :error)
RETURNING id
"""

_AGGREGATE_SQL = """
SELECT
    stage,
    COALESCE(SUM(cost_usd) FILTER (WHERE is_replay = FALSE), 0) AS real_cost_usd,
    COALESCE(SUM(cost_usd) FILTER (WHERE is_replay = TRUE), 0)  AS replay_cost_usd,
    COUNT(*)                                                    AS calls,
    COUNT(*) FILTER (WHERE is_replay = TRUE)                    AS replay_calls,
    COUNT(*) FILTER (WHERE is_replay = FALSE AND success = FALSE) AS failed_calls,
    COUNT(*) FILTER (WHERE is_replay = FALSE AND success = TRUE AND cost_usd IS NULL)
                                                                AS unknown_price_calls
FROM llm_call_logs
WHERE (CAST(:project_id AS BIGINT) IS NULL OR project_id = CAST(:project_id AS BIGINT))
GROUP BY stage
ORDER BY stage NULLS FIRST
"""

# 注意：可空参数必须显式 CAST，否则 asyncpg 无法推断 `$1 IS NULL` 的类型
_RECENT_LOG_SQL = """
SELECT id, project_id, stage, provider, model, purpose, prompt_tokens,
       completion_tokens, cost_usd, duration_ms, success, is_replay, error, created_at
FROM llm_call_logs
WHERE (CAST(:project_id AS BIGINT) IS NULL OR project_id = CAST(:project_id AS BIGINT))
  AND (CAST(:stage AS VARCHAR) IS NULL OR stage = CAST(:stage AS VARCHAR))
ORDER BY id DESC
LIMIT :limit
"""


class SqlLLMStore:
    """基于 SQLAlchemy 2.x 原生 SQL 的 PostgreSQL 实现。"""

    def __init__(self, engine: Any | None = None, dsn: str | None = None) -> None:
        self._engine = engine
        self._dsn = dsn
        self._created_engine = False

    # -- engine 管理 ------------------------------------------------------- #
    def _resolve_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from llm.db import get_async_engine

        self._engine = get_async_engine(self._dsn)
        self._created_engine = True
        return self._engine

    async def dispose(self) -> None:
        if self._engine is not None and self._created_engine:
            await self._engine.dispose()
            self._engine = None

    async def ping(self) -> bool:
        """连通性自检（供 /costs/summary 与设置页展示数据层状态）。"""
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    # -- 记账 -------------------------------------------------------------- #
    async def insert_call_log(self, entry: CallLogEntry) -> int | None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        params = entry.to_row()
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(_INSERT_LOG_SQL), params)
                row = result.first()
                return int(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001 - 统一转成资产可读的 LLMError
            raise LLMError(
                f"写入 llm_call_logs 失败：{exc}",
                provider=entry.provider,
                model=entry.model,
                detail={"stage": entry.stage, "purpose": entry.purpose},
            ) from exc

    # -- fixture ----------------------------------------------------------- #
    async def find_fixture(self, fixture_type: str, fixture_key: str) -> dict[str, Any] | None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT payload FROM demo_fixtures "
                    "WHERE fixture_type = :fixture_type AND fixture_key = :fixture_key"
                ),
                {"fixture_type": fixture_type, "fixture_key": fixture_key},
            )
            row = result.first()
        if not row:
            return None
        payload = row[0]
        if isinstance(payload, str):  # 极端情况：JSONB 以文本返回
            import json

            payload = json.loads(payload)
        return dict(payload or {})

    async def upsert_fixture(
        self, fixture_type: str, fixture_key: str, payload: dict[str, Any], note: str | None = None
    ) -> None:
        import json

        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO demo_fixtures (fixture_type, fixture_key, payload, note) "
                    "VALUES (:fixture_type, :fixture_key, CAST(:payload AS JSONB), :note) "
                    "ON CONFLICT (fixture_type, fixture_key) "
                    "DO UPDATE SET payload = EXCLUDED.payload, note = EXCLUDED.note"
                ),
                {
                    "fixture_type": fixture_type,
                    "fixture_key": fixture_key,
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "note": note,
                },
            )

    # -- 查询 -------------------------------------------------------------- #
    async def aggregate_costs(self, project_id: int | None = None) -> list[StageCostRow]:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text(_AGGREGATE_SQL), {"project_id": project_id})
            rows = result.mappings().all()
        return [
            StageCostRow(
                stage=row["stage"],
                real_cost_usd=float(row["real_cost_usd"]) if row["real_cost_usd"] is not None else 0.0,
                replay_cost_usd=float(row["replay_cost_usd"]) if row["replay_cost_usd"] is not None else 0.0,
                calls=int(row["calls"]),
                replay_calls=int(row["replay_calls"]),
                failed_calls=int(row["failed_calls"]),
                unknown_price_calls=int(row["unknown_price_calls"]),
            )
            for row in rows
        ]

    async def recent_call_logs(
        self, *, project_id: int | None = None, stage: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(_RECENT_LOG_SQL),
                {"project_id": project_id, "stage": stage, "limit": limit},
            )
            rows = result.mappings().all()
        return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# 默认单例
# --------------------------------------------------------------------------- #
_default_store: LLMStore | None = None


def get_store() -> LLMStore:
    """获取默认记账存储（生产为 PostgreSQL）。

    刻意**不做**「DB 不可用就退回内存」的降级：那会让调用变成不留痕的黑洞，
    直接违反「每一次调用都必须写 llm_call_logs」。
    """
    global _default_store
    if _default_store is None:
        _default_store = SqlLLMStore()
    return _default_store


def set_store(store: LLMStore | None) -> None:
    """覆盖默认存储（单元测试 / 依赖注入用）。"""
    global _default_store
    _default_store = store


__all__ = [
    "FIXTURE_TYPE_LLM_RESPONSE",
    "InMemoryLLMStore",
    "LLMStore",
    "SqlLLMStore",
    "StageCostRow",
    "get_store",
    "set_store",
]
