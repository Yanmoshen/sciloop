# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""数据库会话工厂（WP01 公共设施）。

对外契约（其他工作包直接引用，勿改名）
----------------------------------------
===================  ==================================================
``engine``           异步引擎（``AsyncEngine``）—— 异步端点 / LLM 记账复用
``AsyncSessionLocal`` 异步会话工厂（``async_sessionmaker``）
``sync_engine``      同步引擎（``Engine``）
``SessionLocal``     **同步会话工厂**（``sessionmaker``）—— WP04 已按此名引用
``get_db``           同步会话 FastAPI 依赖
``get_async_db``     异步会话 FastAPI 依赖
``ping_database``    健康检查用连通性探测
===================  ==================================================

驱动口径：异步走 ``asyncpg``，同步走 ``psycopg``（alembic 同步迁移与批量任务共用）。
两个引擎都是**惰性连接**的，模块导入阶段不会真正连库；连接串不可解析时降级为
``None`` 并记录错误日志，保证 ``import db.session`` 永不炸掉整个进程。
"""

from __future__ import annotations

import logging
from pathlib import Path
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

logger = logging.getLogger("sciloop.db")

settings = get_settings()

# 连接池参数：演示环境并发极低，保持小池 + 预检，避免连接泄漏拖垮 postgres
_POOL_KWARGS: dict[str, Any] = {
    "pool_pre_ping": True,
    "pool_size": 5,
    "max_overflow": 5,
    "pool_recycle": 1800,
    "future": True,
}


def _async_dsn() -> str:
    """归一为异步 DSN；宿主机默认使用 SQLite。"""
    url = (settings.database_url or "").strip()
    if url.startswith("sqlite:///"):
        url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if url.startswith("sqlite+aiosqlite:///"):
        _ensure_sqlite_parent(url)
        return url
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _sync_dsn() -> str:
    """同步 DSN（SQLite 或 ``postgresql+psycopg://``）。"""
    url = settings.sync_database_url
    if url.startswith("sqlite:///"):
        _ensure_sqlite_parent(url)
    return url


def _ensure_sqlite_parent(url: str) -> None:
    """创建 SQLite 文件的父目录，支持宿主机直接启动。"""
    raw = url.removeprefix("sqlite+aiosqlite:///").removeprefix("sqlite:///")
    if raw == ":memory:":
        return
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _build_async_engine() -> AsyncEngine | None:
    try:
        dsn = _async_dsn()
        kwargs = {} if dsn.startswith("sqlite") else _POOL_KWARGS
        return create_async_engine(
            dsn,
            echo=False,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动缺失 / DSN 非法都不应阻断导入
        logger.error("异步引擎创建失败（driver=asyncpg）: %s", exc)
        return None


def _build_sync_engine() -> Engine | None:
    try:
        dsn = _sync_dsn()
        kwargs = {} if dsn.startswith("sqlite") else _POOL_KWARGS
        return create_engine(dsn, echo=False, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.error("同步引擎创建失败（driver=psycopg）: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# 模块级单例（其他工作包按名字引用）
# --------------------------------------------------------------------------- #
engine: AsyncEngine | None = _build_async_engine()
sync_engine: Engine | None = _build_sync_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = (
    async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    if engine is not None
    else None
)

SessionLocal: sessionmaker[Session] | None = (
    sessionmaker(bind=sync_engine, autoflush=False, expire_on_commit=False, future=True)
    if sync_engine is not None
    else None
)


# --------------------------------------------------------------------------- #
# 依赖注入
# --------------------------------------------------------------------------- #
def get_db() -> Iterator[Session]:
    """FastAPI 同步依赖：``def`` 端点用（由 FastAPI 放线程池执行）。"""
    if SessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise RuntimeError("同步数据库会话工厂不可用（DATABASE_URL / psycopg 未就绪）")
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 异步依赖：``async def`` 端点用。"""
    if AsyncSessionLocal is None:  # pragma: no cover
        raise RuntimeError("异步数据库会话工厂不可用（DATABASE_URL / asyncpg 未就绪）")
    async with AsyncSessionLocal() as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """脚本 / 批量任务用的同步事务上下文：异常回滚，正常提交。"""
    if SessionLocal is None:  # pragma: no cover
        raise RuntimeError("同步数据库会话工厂不可用")
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# 健康检查与生命周期
# --------------------------------------------------------------------------- #
async def ping_database(timeout_seconds: float = 3.0) -> dict[str, Any]:
    """探测数据库连通性，返回 ``{ok, detail, version?}``（不抛异常）。"""
    if engine is None:
        return {"ok": False, "detail": "async_engine_unavailable"}
    try:
        async with engine.connect() as conn:
            if conn.dialect.name == "sqlite":
                version = await conn.execute(text("SELECT sqlite_version()"))
                version_value = version.scalar_one()
                database = str(settings.database_url)
            else:
                result = await conn.execute(text("SELECT version()"))
                version_value = result.scalar_one()
                result = await conn.execute(text("SELECT current_database()"))
                database = result.scalar_one()
        return {
            "ok": True,
            "detail": "connected",
            "database": database,
            "server_version": str(version_value).split(" ")[0] if version_value else None,
        }
    except SQLAlchemyError as exc:
        logger.warning("数据库健康检查失败: %s", exc)
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - 驱动层异常同样视为不可用
        logger.warning("数据库健康检查异常: %s", exc)
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def create_schema() -> None:
    """创建宿主机 SQLite 的初始 schema；PostgreSQL 仍由 Alembic 管理。"""
    if sync_engine is None or sync_engine.dialect.name != "sqlite":
        return
    from db.base import Base
    import db.models  # noqa: F401 - 注册所有模型

    # PostgreSQL 迁移中的 ``NULLS LAST`` 索引语法在 SQLite 不存在；普通业务查询
    # 不依赖这些索引，因此宿主机初始化时跳过它们，PostgreSQL 仍由 Alembic 创建。
    skipped_indexes = []
    for table in Base.metadata.tables.values():
        for index in list(table.indexes):
            if any("NULLS LAST" in str(getattr(expr, "text", expr)).upper() for expr in index.expressions):
                table.indexes.remove(index)
                skipped_indexes.append((table, index))
    try:
        Base.metadata.create_all(sync_engine)
        # SQLite has no Alembic process in the one-line host deployment. Keep
        # existing local databases usable when a nullable application column is
        # added after the first start (CREATE TABLE alone does not alter tables).
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(sync_engine)
        with sync_engine.begin() as connection:
            for table in Base.metadata.tables.values():
                if not inspector.has_table(table.name):
                    continue
                present = {item["name"] for item in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in present or column.primary_key:
                        continue
                    type_sql = column.type.compile(dialect=sync_engine.dialect)
                    nullable_sql = "" if column.nullable else " NOT NULL"
                    default_sql = ""
                    if column.server_default is not None:
                        default_sql = f" DEFAULT {column.server_default.arg}"
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                            f"{type_sql}{default_sql}{nullable_sql}"
                        )
                    )
    finally:
        for table, index in skipped_indexes:
            table.indexes.add(index)
async def dispose_engines() -> None:
    """应用关闭时释放两个连接池。"""
    if engine is not None:
        await engine.dispose()
    if sync_engine is not None:
        sync_engine.dispose()


__all__ = [
    "AsyncSessionLocal",
    "SessionLocal",
    "dispose_engines",
    "engine",
    "get_async_db",
    "get_db",
    "ping_database",
    "session_scope",
    "sync_engine",
]
