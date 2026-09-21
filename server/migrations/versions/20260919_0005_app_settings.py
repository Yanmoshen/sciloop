# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""新增全局设置表 ``app_settings``（当前用于「阅读设置」）。

为什么需要这张表
----------------
「阅读设置」要求**全站生效**，而现有存储里没有全局偏好：

- ``reader_states`` 是**逐文档**的（``document_id`` 唯一），只能表达"这一篇读到哪、字号多大"；
- ``model_configs`` 是供应商配置，语义不符。

所以新增一张极小的 **scope → 一个 JSONB** 的表：一个 scope 一行，整块偏好存在
``value`` 里。这样**新增一个设置项不需要迁移**，加新的 scope 也不用改表结构。

为什么不做成 ``key/value`` 多行
--------------------------------
接口形状就是 ``GET/PUT /api/v1/settings/{scope}``（读写"一个 scope 的整块设置"），
一 scope 一行与之严格对应；多行 key/value 会让读取变成 N 行拼装、更新变成多行 upsert，
在设置体量（几十个字段）下没有任何收益。

口径与护栏
----------
- 表**不含任何密钥**：``OWNER_TOKEN`` 只允许存在于服务端环境变量（见 ``.env.example`` 说明），
  这里只存界面偏好这类非机密数据。
- 合法的 ``scope`` 与字段白名单由 ``app/services/settings/store.py`` 校验，
  **不写进数据库约束** —— 这样新增设置项只需要改一行代码，不必再发一个迁移。
- ``updated_at`` 由服务层显式写入（upsert 时 ``now()``）。

回滚
----
``downgrade()`` 直接 ``DROP TABLE``。该表只存界面偏好，**丢失不影响任何产出物**：
翻译产物、阅读器不可变版本、批注、Passport 都不依赖它。
"""

from __future__ import annotations

from alembic import op

revision = "0005_app_settings"
down_revision = "0004_reader_version_kind_history"
branch_labels = None
depends_on = None

TABLE = "app_settings"

_CREATE = f"""
CREATE TABLE {TABLE} (
    scope       VARCHAR(32) PRIMARY KEY,
    value       JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def upgrade() -> None:
    op.execute(_CREATE)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
