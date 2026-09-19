# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""数据库级 Passport 不可变护栏（缺陷 B / contracts.database.conventions）。

背景
----
``experiment_passports`` 创建后**不可 UPDATE**；重跑须生成新记录并以
``parent_passport_id`` 关联（``contracts.passport_rules.immutability``）。
该约束原先只在 ORM 层由 ``app/services/experiment/passport.py`` 的 ``before_update``
事件拦截（``code=passport_immutable``）；绕过 ORM 的**裸 SQL UPDATE 仍可原地改写历史凭证**。
本迁移把不可变性下沉为**数据库不变量**：``BEFORE UPDATE ... FOR EACH ROW`` 触发器直接抛异常。

为什么不会误伤合法流程
----------------------
- 子 Passport 通过 ``INSERT`` 写入（带 ``parent_passport_id`` 外键），不触发本触发器；
- ``parent_passport_id`` 的 ``ON DELETE SET NULL`` 属外键内部动作，不经过行级 UPDATE 触发器；
- 既有 41+ 条 Passport 只被读取（replay/rerun 生成新行），本迁移不触碰任何数据行。

回滚
----
``downgrade()`` 依次 ``DROP TRIGGER`` + ``DROP FUNCTION``，与 ``upgrade()`` 严格互逆，
保证 ``alembic downgrade base`` 可回滚（被 ``scripts/verify/empty_db_migration.sh`` 覆盖）。
"""

from __future__ import annotations

from alembic import op

revision = "0002_passport_immutable_guard"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

TABLE_NAME = "experiment_passports"
FUNCTION_NAME = "sciloop_experiment_passports_immutable"
TRIGGER_NAME = "trg_experiment_passports_immutable"

#: 报错信息必须含 ``passport_immutable`` 字面量（与 ORM 层 PassportImmutableError.code 同名），
#: 使「API 层 / ORM 层 / 数据库层」三处拒绝口径可被同一断言识别。
_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION_NAME}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'passport_immutable: experiment_passports 创建后不可 UPDATE'
        '（contracts.database.conventions / contracts.passport_rules.immutability）；'
        '重跑请 INSERT 新 Passport 并以 parent_passport_id 关联，禁止原地改写历史凭证'
        USING ERRCODE = 'raise_exception',
              HINT = '如需修订，请生成子 Passport（parent_passport_id 指向被修订的凭证）';
END;
$$;
"""

_CREATE_TRIGGER = f"""
CREATE TRIGGER {TRIGGER_NAME}
BEFORE UPDATE ON {TABLE_NAME}
FOR EACH ROW
EXECUTE FUNCTION {FUNCTION_NAME}();
"""

_DROP_TRIGGER = f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON {TABLE_NAME}"
_DROP_FUNCTION = f"DROP FUNCTION IF EXISTS {FUNCTION_NAME}()"


def _exec(sql: str) -> None:
    """执行原文 DDL（``exec_driver_sql`` 不走 text() 解析，避免 ``$$`` 被误判为绑定参数）。"""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _exec(_CREATE_FUNCTION)
    _exec(_DROP_TRIGGER)  # 幂等：重复执行 upgrade head 时先清掉同名触发器
    _exec(_CREATE_TRIGGER)


def downgrade() -> None:
    _exec(_DROP_TRIGGER)
    _exec(_DROP_FUNCTION)
