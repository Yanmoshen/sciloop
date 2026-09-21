# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""LLM 记账所用的数据库连接。

优先复用 WP01 在 ``db.session`` 中建立的引擎（避免进程内出现两个连接池）；
若该模块尚未就绪（并行开发期间），则按 ``DATABASE_URL`` 自建一个仅本模块使用的引擎。
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_engine: Any | None = None

_DEFAULT_DSN = "postgresql+asyncpg://sciloop:sciloop@localhost:5432/sciloop"


def normalize_async_dsn(dsn: str | None) -> str:
    """把 DSN 归一为 SQLAlchemy 异步驱动形式。"""
    url = (dsn or "").strip()
    if not url:
        url = _read_dsn_from_settings()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _read_dsn_from_settings() -> str:
    try:
        from core.config import get_settings

        return get_settings().database_url
    except Exception:  # pragma: no cover - 单独跑脚本 / 配置未就绪
        return os.environ.get("DATABASE_URL", _DEFAULT_DSN)


def _engine_from_core() -> Any | None:
    """若 WP01 已提供 db.session.engine，直接复用。"""
    try:
        from db import session as db_session  # type: ignore
    except Exception:
        return None
    return getattr(db_session, "engine", None)


def get_async_engine(dsn: str | None = None) -> Any:
    """获取（并缓存）异步引擎。"""
    global _engine
    if dsn is None:
        shared = _engine_from_core()
        if shared is not None:
            return shared
    if _engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        url = normalize_async_dsn(dsn)
        _engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        logger.debug("LLM 记账引擎已创建 driver=%s", url.split("://", 1)[0])
    return _engine


async def dispose_engine() -> None:
    """释放自建引擎（应用关闭 / 测试收尾用）。"""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


__all__ = ["dispose_engine", "get_async_engine", "normalize_async_dsn"]
