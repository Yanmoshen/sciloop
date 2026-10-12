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

驱动口径：异步走 ``aiosqlite``，同步走 Python 内置 ``sqlite3``。
两个引擎都是**惰性连接**的，模块导入阶段不会真正连库；连接串不可解析时降级为
``None`` 并记录错误日志，保证 ``import db.session`` 永不炸掉整个进程。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings
from core.database import database_url

logger = logging.getLogger("sciloop.db")

settings = get_settings()

def _async_dsn() -> str:
    url = database_url(settings.database_url)
    _ensure_sqlite_parent(url)
    return url


def _sync_dsn() -> str:
    url = database_url(settings.database_url, asynchronous=False)
    _ensure_sqlite_parent(url)
    return url


def _ensure_sqlite_parent(url: str) -> None:
    """创建 SQLite 文件的父目录，支持宿主机直接启动。"""
    raw = make_url(url).database
    if not raw or raw == ":memory:":
        return
    Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _build_async_engine() -> AsyncEngine | None:
    try:
        dsn = _async_dsn()
        return create_async_engine(
            dsn,
            echo=False,
        )
    except Exception as exc:  # noqa: BLE001 - 驱动缺失 / DSN 非法都不应阻断导入
        logger.error("异步引擎创建失败（driver=aiosqlite）: %s", exc)
        return None


def _build_sync_engine() -> Engine | None:
    try:
        dsn = _sync_dsn()
        return create_engine(dsn, echo=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("同步引擎创建失败（driver=sqlite3）: %s", exc)
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
        raise RuntimeError("同步数据库会话工厂不可用（DATABASE_URL / sqlite3 未就绪）")
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


async def get_async_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 异步依赖：``async def`` 端点用。"""
    if AsyncSessionLocal is None:  # pragma: no cover
        raise RuntimeError("异步数据库会话工厂不可用（DATABASE_URL / aiosqlite 未就绪）")
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
            version = await conn.execute(text("SELECT sqlite_version()"))
            version_value = version.scalar_one()
            database = str(settings.database_url)
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
    """创建及补齐 SQLite schema。"""
    if sync_engine is None or sync_engine.dialect.name != "sqlite":
        return
    import db.models  # noqa: F401 - 注册所有模型
    from db.base import Base

    # SQLite 索引不支持 NULLS LAST，跳过这些不影响业务查询的索引。
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
        _repair_legacy_sqlite(sync_engine)
    finally:
        for table, index in skipped_indexes:
            table.indexes.add(index)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _split_definitions(body: str) -> list[str]:
    """Split CREATE TABLE definitions without splitting CHECK/default expressions."""
    parts, start, depth, quote = [], 0, 0, None
    i = 0
    while i < len(body):
        char = body[i]
        if quote:
            if char == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in ('"', "'", '`'):
            quote = char
        elif char == '[':
            quote = ']'
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(body[start:i])
            start = i + 1
        i += 1
    parts.append(body[start:])
    return parts


def _repair_legacy_sqlite(target_engine: Engine) -> None:
    """Repair PostgreSQL-era SQLite defaults/PKs, preserving original table DDL.

    Unknown columns, CHECK/UNIQUE/FK constraints, indexes and triggers are retained.
    SQLite's documented create-copy-drop-rename procedure runs as one transaction;
    changing FK enforcement happens before BEGIN, never within a transaction.
    """
    raw = target_engine.raw_connection()
    connection = raw.driver_connection
    original_foreign_keys = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    original_legacy_alter = connection.execute('PRAGMA legacy_alter_table').fetchone()[0]
    try:
        connection.commit()
        connection.execute('PRAGMA foreign_keys=OFF')
        # Views/external triggers must keep referring to the original table name
        # during the documented drop/rename window.
        connection.execute('PRAGMA legacy_alter_table=ON')
        plans = []
        for name, sql in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall():
            columns = connection.execute(f'PRAGMA table_info({_quote_identifier(name)})').fetchall()
            boolean_names = {row[1] for row in columns if row[2].upper() == 'BOOLEAN'}
            pk = [row for row in columns if row[5]]
            bigint_pk = pk[0][1] if len(pk) == 1 and pk[0][2].upper() == 'BIGINT' else None
            start, end = sql.find('('), sql.rfind(')')
            definitions = _split_definitions(sql[start + 1:end])
            repaired = []
            for definition in definitions:
                for column in columns:
                    colname = column[1]
                    pattern = r'^(\s*)(?:"' + re.escape(colname) + r'"|`' + re.escape(colname) + r'`|\[' + re.escape(colname) + r'\]|' + re.escape(colname) + r')\s+'
                    match = re.match(pattern, definition, re.IGNORECASE)
                    if not match:
                        continue
                    prefix, rest = definition[:match.end()], definition[match.end():]
                    if colname == bigint_pk:
                        rest = re.sub(r'^BIGINT\b', 'INTEGER', rest, flags=re.IGNORECASE)
                    if colname in boolean_names:
                        rest = re.sub(r"\bDEFAULT\s+'(false|true)'", lambda m: 'DEFAULT ' + ('0' if m[1].lower() == 'false' else '1'), rest, flags=re.IGNORECASE)
                    definition = prefix + rest
                    break
                repaired.append(definition)
            fixed_sql = sql[:start + 1] + ','.join(repaired) + sql[end:]
            plans.append((name, sql, fixed_sql, columns, boolean_names))

        needs_repair = any(old != new for _, old, new, _, _ in plans)
        if needs_repair and target_engine.url.database not in (None, ':memory:'):
            database_path = Path(target_engine.url.database).resolve()
            backup_dir = database_path.parent.parent / '.data' / 'database-backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f'{database_path.stem}-before-sqlite-repair-{datetime.now():%Y%m%d%H%M%S%f}.sqlite3'
            with sqlite3.connect(backup_path) as backup:
                connection.backup(backup)
            logger.info('SQLite migration recovery copy: %s', backup_path)

        connection.execute('BEGIN IMMEDIATE')
        previous_violations = set(connection.execute('PRAGMA foreign_key_check').fetchall())
        for name, old, new, columns, boolean_names in plans:
            quoted = _quote_identifier(name)
            if old != new:
                temp_name = '__sciloop_repair_' + name
                temp = _quote_identifier(temp_name)
                # Do not rename the old table: SQLite may rewrite external FKs.
                create_sql = re.sub(r'^CREATE\s+TABLE\s+(?:"[^"]+"|`[^`]+`|\[[^]]+\]|[^\s(]+)', 'CREATE TABLE ' + temp, new, count=1, flags=re.IGNORECASE)
                objects = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (name,)).fetchall()
                sequence = None
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                    sequence = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (name,)).fetchone()
                fields = ','.join(_quote_identifier(row[1]) for row in columns)
                before = connection.execute(f'SELECT count(*) FROM {quoted}').fetchone()[0]
                connection.execute(create_sql)
                connection.execute(f'INSERT INTO {temp} ({fields}) SELECT {fields} FROM {quoted}')
                if connection.execute(f'SELECT count(*) FROM {temp}').fetchone()[0] != before:
                    raise RuntimeError(f'SQLite migration row count mismatch: {name}')
                connection.execute(f'DROP TABLE {quoted}')
                connection.execute(f'ALTER TABLE {temp} RENAME TO {quoted}')
                for (object_sql,) in objects:
                    connection.execute(object_sql)
                if sequence is not None:
                    connection.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (sequence[0], name))
            for colname in boolean_names:
                col = _quote_identifier(colname)
                connection.execute(f"UPDATE {quoted} SET {col}=CASE lower(trim({col})) WHEN 'false' THEN 0 WHEN 'true' THEN 1 END WHERE typeof({col})='text' AND lower(trim({col})) IN ('false','true')")
        new_violations = set(connection.execute('PRAGMA foreign_key_check').fetchall())
        if new_violations - previous_violations:
            raise RuntimeError('SQLite migration introduced foreign key violations')
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite migration integrity check failed')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute(f'PRAGMA foreign_keys={int(original_foreign_keys)}')
        connection.execute(f'PRAGMA legacy_alter_table={int(original_legacy_alter)}')
        raw.close()
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
