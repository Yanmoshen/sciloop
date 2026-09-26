# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""阅读器只留**一种**中文译本：``chinese | simple | bilingual`` → ``chinese``。

为什么改
--------
研究者 2026-09-26 口径（全文阅读界面）：「译文版本」选择与「登记中文译本 /
通俗简化 / 双语对照」三个按钮全部下掉，界面上只保留「翻译该论文」与
「双语对照模式」两个动作；后者是**显示开关**（左英文原文、右中文译文，两栏各自滚动），
不是另一种译本。

于是「通俗简化」「双语对照」作为**独立译本类型**一并下线：一个阅读文档只可能有
``original``（原文，证据源）与 ``chinese``（中文译本）两种版本。

改什么
------
1. **先清数据**（顺序重要：先删存量行再收紧约束，否则 ``ADD CONSTRAINT`` 必然失败）：
   - ``reader_versions`` 中 ``kind IN ('simple','bilingual')`` 的行 → ``DELETE``
     （研究者明确选择"清掉历史数据"）。批注不会跟着消失：
     ``reader_annotations.version_id`` 是 ``ON DELETE SET NULL``，
     锚点（``block_id`` / ``quote_sha256``）与摘录正文都还在；
   - ``reader_states.mode`` 落在下线的两个值上的行 → 归一为 ``chinese``；
   - ``app_settings`` 的 ``reading.default_kind`` 同理。
2. 收紧两个取值约束为 ``('original','chinese')``。

不改什么
--------
- **翻译引擎照旧产出** ``mono.pdf`` 与 ``dual.pdf``（``manifest.files`` 是与翻译模块的
  冻结契约，不因阅读器收窄而变）—— 只是阅读器不再把 ``dual`` 登记成版本，产物留痕不受影响；
- ``reader_versions`` 的 ``BEFORE UPDATE`` 不可变触发器不动（本迁移只 ``DELETE``，
  不 ``UPDATE`` 版本行）。

回滚
----
``downgrade()`` 把两个约束放宽回四值。**已删除的版本行不会回来** —— 回滚只恢复
"允许登记三类"的能力，不伪造历史（如实记录，不静默补齐）。
"""

from __future__ import annotations

from alembic import op

#: ⚠️ 长度上限 32：``alembic_version.version_num`` 是 ``varchar(32)``。
revision = "0010_reader_single_translation"
down_revision = "0009_chain_by_conversation"
branch_labels = None
depends_on = None

VERSIONS_KIND_CONSTRAINT = "ck_reader_versions_kind"
STATES_MODE_CONSTRAINT = "ck_reader_states_mode"

#: 0003 建表时的取值（回滚用）
_OLD_VALUES = "'original','chinese','simple','bilingual'"
#: 收敛后的取值
_NEW_VALUES = "'original','chinese'"


def _swap_constraint(table: str, name: str, column: str, values: str) -> None:
    """重建 CHECK 约束（先 DROP 再 ADD，沿用 0003 的显式约束名）。"""
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({values}))")


def upgrade() -> None:
    # 1) 清掉已下线的两类译本（必须先于收紧约束）
    op.execute("DELETE FROM reader_versions WHERE kind IN ('simple','bilingual')")
    op.execute("UPDATE reader_states SET mode = 'chinese' WHERE mode IN ('simple','bilingual')")
    op.execute(
        "UPDATE app_settings SET value = jsonb_set(value, '{default_kind}', '\"chinese\"') "
        "WHERE scope = 'reading' AND value ->> 'default_kind' IN ('simple','bilingual')"
    )
    # 2) 收紧取值约束
    _swap_constraint("reader_versions", VERSIONS_KIND_CONSTRAINT, "kind", _NEW_VALUES)
    _swap_constraint("reader_states", STATES_MODE_CONSTRAINT, "mode", _NEW_VALUES)


def downgrade() -> None:
    _swap_constraint("reader_versions", VERSIONS_KIND_CONSTRAINT, "kind", _OLD_VALUES)
    _swap_constraint("reader_states", STATES_MODE_CONSTRAINT, "mode", _OLD_VALUES)
