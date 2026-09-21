# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""Alembic 运行环境（同步引擎 + psycopg）。

- 连接串统一来自 ``core.config.settings``（可用 ``-x dsn=...`` 覆盖，便于测试）
- 先 ``import db.models`` 保证 ``Base.metadata`` 含全部 30 张表
"""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# 保证 `alembic` 在 server/ 目录下执行时能 import 顶层包（api / core / db / services …）
SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from core.config import get_settings  # noqa: E402
from db.base import Base  # noqa: E402
import db.models  # noqa: E402,F401  注册全部模型

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

logger = logging.getLogger("alembic.env")

target_metadata = Base.metadata


def _database_url() -> str:
    """优先取 ``-x dsn=...``，否则用 Settings 的同步 DSN。"""
    override = context.get_x_argument(as_dictionary=True).get("dsn")
    if override:
        return override
    return get_settings().sync_database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：走同步引擎执行迁移。"""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
