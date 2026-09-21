# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""``projects`` 加 ``archived``（左栏「项目」分组的归档位）。

为什么加
--------
首页对话重构后，左栏「项目」分组按最近活动倒序列出项目，行上提供「归档」；
归档后从主列表收起、进入「已归档」折叠区（可取消归档）。
此前 ``projects`` 没有任何归档/软删除位，只能靠 ``status`` 近似表达，语义不对
（``status`` 是**流水线状态机**的状态，写它等于绕过状态机）。

所以这里**只加一列** ``archived BOOLEAN NOT NULL DEFAULT false``：
- 服务端默认 ``false`` → 既有行全部保持可见，旧代码（不读该列）行为不变；
- 不做数据回填（默认值就是正确值）；
- 不加索引：项目量级在百行内，全表扫描即可。

回滚
----
``downgrade()`` 删列。删除后归档信息丢失（属于"不可逆的元数据损失"），
但业务数据本身完整，属可接受回滚。
"""

from __future__ import annotations

from alembic import op

revision = "0007_project_archived"
down_revision = "0006_model_config_type"
branch_labels = None
depends_on = None

TABLE = "projects"
COLUMN = "archived"

_UP = f"""
ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} BOOLEAN NOT NULL DEFAULT false;
COMMENT ON COLUMN {TABLE}.{COLUMN} IS
    'Archived: true = 从左栏「项目」主列表收起，进「已归档」折叠区'
"""

_DOWN = f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {COLUMN}"


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
