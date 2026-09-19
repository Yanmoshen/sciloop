# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``model_configs`` / ``stage_model_routing`` 的存取层。

与 :mod:`app.llm.store` 同样的取舍：用原生 SQL 直连附录 A.6 已冻结的表结构，
不依赖 WP01 的 ORM 定义，保证 WP02 的读写链路可独立验证。

**API Key 只以密文或 ``env:VAR`` 引用形态进出本层**，解密统一由 :mod:`app.llm.secret` 完成。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: 六环节 + 前置环节（附录 A.6 llm_call_logs.stage 注释口径）
STAGES: tuple[str, ...] = (
    "parse",
    "aggregate",
    "ideate",
    "feasibility",
    "survey",
    "plan",
    "plan_review",
    "experiment",
    "writing",
    "review",
    "decide",
)


@dataclass
class ModelConfigRecord:
    """一行 ``model_configs``。"""

    id: int
    name: str
    base_url: str
    api_key_enc: str = ""
    models: list[dict[str, Any]] = field(default_factory=list)
    is_default: bool = False
    last_tested_at: datetime | None = None
    test_ok: bool | None = None
    created_at: datetime | None = None

    def find_model(self, model_id: str) -> dict[str, Any] | None:
        for entry in self.models or []:
            if str(entry.get("model_id")) == str(model_id):
                return entry
        return None


@dataclass
class RoutingRecord:
    """一行 ``stage_model_routing``（``project_id`` 为 None 表示全局默认）。"""

    id: int
    stage: str
    model_config_id: int
    model_id: str
    project_id: int | None = None
    purpose: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    created_at: datetime | None = None


@runtime_checkable
class ModelRegistry(Protocol):
    async def list_configs(self) -> list[ModelConfigRecord]: ...
    async def get_config(self, config_id: int) -> ModelConfigRecord | None: ...
    async def create_config(
        self, *, name: str, base_url: str, api_key_enc: str, models: list[dict[str, Any]], is_default: bool
    ) -> int: ...
    async def update_config(self, config_id: int, fields: dict[str, Any]) -> None: ...
    async def delete_config(self, config_id: int) -> None: ...
    async def mark_tested(self, config_id: int, ok: bool) -> None: ...
    async def list_routing(self, project_id: int | None = None) -> list[RoutingRecord]: ...
    async def list_all_routing(self) -> list[RoutingRecord]: ...
    async def get_routing(self, stage: str, project_id: int | None = None) -> RoutingRecord | None: ...
    async def replace_routing(self, entries: list[dict[str, Any]], project_id: int | None = None) -> None: ...


# --------------------------------------------------------------------------- #
# 内存实现
# --------------------------------------------------------------------------- #
class InMemoryModelRegistry:
    """测试用实现（不落库）。"""

    def __init__(self) -> None:
        self.configs: dict[int, ModelConfigRecord] = {}
        self.routing: list[RoutingRecord] = []
        self._config_seq = 0
        self._routing_seq = 0

    async def list_configs(self) -> list[ModelConfigRecord]:
        return [self.configs[key] for key in sorted(self.configs)]

    async def get_config(self, config_id: int) -> ModelConfigRecord | None:
        return self.configs.get(int(config_id))

    async def create_config(
        self, *, name: str, base_url: str, api_key_enc: str, models: list[dict[str, Any]], is_default: bool
    ) -> int:
        self._config_seq += 1
        config_id = self._config_seq
        self.configs[config_id] = ModelConfigRecord(
            id=config_id,
            name=name,
            base_url=base_url,
            api_key_enc=api_key_enc,
            models=list(models),
            is_default=is_default,
            created_at=datetime.now(),
        )
        return config_id

    async def update_config(self, config_id: int, fields: dict[str, Any]) -> None:
        record = self.configs.get(int(config_id))
        if record is None:
            raise KeyError(config_id)
        for key, value in fields.items():
            setattr(record, key, value)

    async def delete_config(self, config_id: int) -> None:
        self.configs.pop(int(config_id), None)

    async def mark_tested(self, config_id: int, ok: bool) -> None:
        record = self.configs.get(int(config_id))
        if record is not None:
            record.test_ok = ok
            record.last_tested_at = datetime.now()

    async def list_routing(self, project_id: int | None = None) -> list[RoutingRecord]:
        return [
            row
            for row in self.routing
            if (row.project_id == project_id) or (project_id is None and row.project_id is None)
        ]

    async def list_all_routing(self) -> list[RoutingRecord]:
        return list(self.routing)

    async def get_routing(self, stage: str, project_id: int | None = None) -> RoutingRecord | None:
        candidates = [
            row
            for row in self.routing
            if row.stage == stage and row.project_id in {project_id, None}
        ]
        if not candidates:
            return None
        # 项目级优先于全局；同层级内 purpose 精确匹配优先
        candidates.sort(key=lambda r: (r.project_id is None, r.purpose is None, -r.id))
        return candidates[0]

    async def replace_routing(self, entries: list[dict[str, Any]], project_id: int | None = None) -> None:
        self.routing = [
            row for row in self.routing if row.project_id != project_id
        ]
        for entry in entries:
            self._routing_seq += 1
            self.routing.append(
                RoutingRecord(
                    id=self._routing_seq,
                    stage=str(entry["stage"]),
                    model_config_id=int(entry["model_config_id"]),
                    model_id=str(entry["model_id"]),
                    project_id=project_id,
                    purpose=entry.get("purpose"),
                    temperature=entry.get("temperature"),
                    max_tokens=entry.get("max_tokens"),
                    created_at=datetime.now(),
                )
            )


# --------------------------------------------------------------------------- #
# SQL 实现
# --------------------------------------------------------------------------- #
_CONFIG_COLUMNS = "id, name, base_url, api_key_enc, models, is_default, last_tested_at, test_ok, created_at"
_ROUTING_COLUMNS = (
    "id, project_id, stage, purpose, model_config_id, model_id, temperature, max_tokens, created_at"
)


class SqlModelRegistry:
    """PostgreSQL 实现。"""

    def __init__(self, engine: Any | None = None, dsn: str | None = None) -> None:
        self._engine = engine
        self._dsn = dsn

    def _resolve_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        from app.llm.db import get_async_engine

        self._engine = get_async_engine(self._dsn)
        return self._engine

    async def list_configs(self) -> list[ModelConfigRecord]:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT {_CONFIG_COLUMNS} FROM model_configs ORDER BY id")
            )
            rows = result.mappings().all()
        return [_row_to_config(dict(row)) for row in rows]

    async def get_config(self, config_id: int) -> ModelConfigRecord | None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT {_CONFIG_COLUMNS} FROM model_configs WHERE id = :id"),
                {"id": int(config_id)},
            )
            row = result.mappings().first()
        return _row_to_config(dict(row)) if row else None

    async def create_config(
        self, *, name: str, base_url: str, api_key_enc: str, models: list[dict[str, Any]], is_default: bool
    ) -> int:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.begin() as conn:
            result = await conn.execute(
                text(
                    "INSERT INTO model_configs (name, base_url, api_key_enc, models, is_default) "
                    "VALUES (:name, :base_url, :api_key_enc, CAST(:models AS JSONB), :is_default) "
                    "RETURNING id"
                ),
                {
                    "name": name,
                    "base_url": base_url,
                    "api_key_enc": api_key_enc,
                    "models": json.dumps(models, ensure_ascii=False),
                    "is_default": is_default,
                },
            )
            row = result.first()
        return int(row[0]) if row else 0

    async def update_config(self, config_id: int, fields: dict[str, Any]) -> None:
        from sqlalchemy import text

        if not fields:
            return
        assignments: list[str] = []
        params: dict[str, Any] = {"id": int(config_id)}
        for key, value in fields.items():
            if key == "models":
                assignments.append("models = CAST(:models AS JSONB)")
                params["models"] = json.dumps(value, ensure_ascii=False)
            else:
                assignments.append(f"{key} = :{key}")
                params[key] = value
        engine = self._resolve_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(f"UPDATE model_configs SET {', '.join(assignments)} WHERE id = :id"), params
            )

    async def delete_config(self, config_id: int) -> None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM model_configs WHERE id = :id"), {"id": int(config_id)})

    async def mark_tested(self, config_id: int, ok: bool) -> None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE model_configs SET test_ok = :ok, last_tested_at = now() WHERE id = :id"
                ),
                {"ok": bool(ok), "id": int(config_id)},
            )

    async def list_routing(self, project_id: int | None = None) -> list[RoutingRecord]:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT {_ROUTING_COLUMNS} FROM stage_model_routing "
                    "WHERE (CAST(:project_id AS BIGINT) IS NULL AND project_id IS NULL) "
                    "   OR (CAST(:project_id AS BIGINT) IS NOT NULL AND project_id = CAST(:project_id AS BIGINT)) "
                    "ORDER BY stage, purpose NULLS FIRST"
                ),
                {"project_id": project_id},
            )
            rows = result.mappings().all()
        return [_row_to_routing(dict(row)) for row in rows]

    async def list_all_routing(self) -> list[RoutingRecord]:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(f"SELECT {_ROUTING_COLUMNS} FROM stage_model_routing ORDER BY id")
            )
            rows = result.mappings().all()
        return [_row_to_routing(dict(row)) for row in rows]

    async def get_routing(self, stage: str, project_id: int | None = None) -> RoutingRecord | None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    f"SELECT {_ROUTING_COLUMNS} FROM stage_model_routing "
                    "WHERE stage = :stage "
                    "  AND (project_id = CAST(:project_id AS BIGINT) OR project_id IS NULL) "
                    "ORDER BY (project_id IS NULL), (purpose IS NULL), id DESC "
                    "LIMIT 1"
                ),
                {"stage": stage, "project_id": project_id},
            )
            row = result.mappings().first()
        return _row_to_routing(dict(row)) if row else None

    async def replace_routing(self, entries: list[dict[str, Any]], project_id: int | None = None) -> None:
        from sqlalchemy import text

        engine = self._resolve_engine()
        async with engine.begin() as conn:
            if project_id is None:
                await conn.execute(text("DELETE FROM stage_model_routing WHERE project_id IS NULL"))
            else:
                await conn.execute(
                    text("DELETE FROM stage_model_routing WHERE project_id = :project_id"),
                    {"project_id": project_id},
                )
            for entry in entries:
                await conn.execute(
                    text(
                        "INSERT INTO stage_model_routing "
                        "(project_id, stage, purpose, model_config_id, model_id, temperature, max_tokens) "
                        "VALUES (:project_id, :stage, :purpose, :model_config_id, :model_id, :temperature, :max_tokens)"
                    ),
                    {
                        "project_id": project_id,
                        "stage": entry["stage"],
                        "purpose": entry.get("purpose"),
                        "model_config_id": int(entry["model_config_id"]),
                        "model_id": entry["model_id"],
                        "temperature": entry.get("temperature"),
                        "max_tokens": entry.get("max_tokens"),
                    },
                )


# --------------------------------------------------------------------------- #
# 行映射
# --------------------------------------------------------------------------- #
def _row_to_config(row: dict[str, Any]) -> ModelConfigRecord:
    models = row.get("models")
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except json.JSONDecodeError:
            models = []
    return ModelConfigRecord(
        id=int(row["id"]),
        name=row["name"],
        base_url=row["base_url"],
        api_key_enc=row.get("api_key_enc") or "",
        models=list(models or []),
        is_default=bool(row.get("is_default")),
        last_tested_at=row.get("last_tested_at"),
        test_ok=row.get("test_ok"),
        created_at=row.get("created_at"),
    )


def _row_to_routing(row: dict[str, Any]) -> RoutingRecord:
    temperature = row.get("temperature")
    return RoutingRecord(
        id=int(row["id"]),
        project_id=row.get("project_id"),
        stage=row["stage"],
        purpose=row.get("purpose"),
        model_config_id=int(row["model_config_id"]),
        model_id=row["model_id"],
        temperature=float(temperature) if temperature is not None else None,
        max_tokens=row.get("max_tokens"),
        created_at=row.get("created_at"),
    )


# --------------------------------------------------------------------------- #
# 默认单例
# --------------------------------------------------------------------------- #
_default_registry: ModelRegistry | None = None


def get_registry() -> ModelRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = SqlModelRegistry()
    return _default_registry


def set_registry(registry: ModelRegistry | None) -> None:
    global _default_registry
    _default_registry = registry


__all__ = [
    "STAGES",
    "InMemoryModelRegistry",
    "ModelConfigRecord",
    "ModelRegistry",
    "RoutingRecord",
    "SqlModelRegistry",
    "get_registry",
    "set_registry",
]
