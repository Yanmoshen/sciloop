# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""可行性加三个维度列：规则层算不出的「落地风险 / 应用价值 / 伦理与合规」。

为什么改
--------
研究者 2026-09-26 定稿：可行性口径从**四维**扩到**七维**（技能
「论文创新Idea生成与专业可行性分析」）。前四维（技术成熟度 / 数据可行性 /
算力与工程成本 / 创新增量）与既有四列同键、分数仍由规则层给（可复算、可审计）；
新增的三维规则层**没有信号**，改由模型评审给分。

改什么
------
``feasibilities`` 加三列 JSONB（``landing_risk`` / ``application_value`` /
``ethics_compliance``），``SERVER DEFAULT '{}'::jsonb`` ——
**存量行留空对象**：那是"这一维当时没有评"，与"评了 0 分"**必须区分开**
（0 分是明确结论，空对象是缺数据，混起来就是把不确定说成确定）。

不改什么
--------
- 既有四列不动：存量记录可原样读取，规则分仍可复算；
- ``total_score`` 列类型不变（含义从"四维加权和"变成"七维平均"，由服务层写入）。

回滚
----
``downgrade()`` 直接删三列。**已写入的评审结果会丢**（重跑可行性可补回），如实记录。
"""

from __future__ import annotations

from alembic import op

#: ⚠️ 长度上限 32：``alembic_version.version_num`` 是 ``varchar(32)``。
revision = "0011_feasibility_seven_dims"
down_revision = "0010_reader_single_translation"
branch_labels = None
depends_on = None

TABLE = "feasibilities"
NEW_COLUMNS: tuple[str, ...] = ("landing_risk", "application_value", "ethics_compliance")


def upgrade() -> None:
    for column in NEW_COLUMNS:
        op.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {column} JSONB "
            "NOT NULL DEFAULT '{}'::jsonb"
        )


def downgrade() -> None:
    for column in reversed(NEW_COLUMNS):
        op.execute(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {column}")
