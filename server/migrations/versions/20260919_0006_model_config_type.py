# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""供应商加 ``type``（端点类型），并说明定价口径的变更。

为什么加 ``type``
------------------
Cherry Studio 的供应商对象带 ``type``（``openai`` / ``anthropic`` / …），
添加服务商时以它决定默认端点与适配族。SciLoop 此前只有 ``name + base_url``，
无法区分「OpenAI 兼容端点」与「Anthropic Messages 端点」——同一家供应商可能两者都有
（例如 DeepSeek 同时提供 ``/v1`` 与 ``/anthropic``）。

本次**只加 ``type`` 一列**（用户决策）：``base_url`` 仍为单值，不做多端点
（``endpointConfigs``）。这样改动可控，且覆盖绝大多数场景。
``type`` 允许为空（旧行为：按 OpenAI 兼容处理），不设 NOT NULL，避免影响既有写入路径。

定价口径（**由本次一并切换，代码侧见 ``app/llm/pricing.py``**）
--------------------------------------------------------------
``model_configs.models[]`` 的每一项由

    {"model_id": "...", "input_price": 0.27, "output_price": 1.1, "price_unit": 1000}

改为 Cherry Studio 口径

    {"model_id": "...",
     "pricing": {"input":  {"currency": "USD", "perMillionTokens": 0.27},
                 "output": {"currency": "USD", "perMillionTokens": 1.1}}}

``pricing`` 是 JSONB 内的嵌套结构，**不需要 DDL**；本迁移只加 ``type`` 列。
切换原因：预置目录（Cherry 的 61 家 / 914 模型 / 1094 条带定价）本身就是这个口径，
沿用旧口径要做一次有损换算；且新口径**自带 currency**，能把「人民币定价」如实表达出来
（护栏是 USD，非 USD 的行不参与比较，见 ``pricing.py`` 的 ``non_usd`` 分支）。

历史数据
--------
本迁移**不做数据回填**：执行时点库里 15 个供应商全部是验收桩，已由运维动作清空
（``DELETE FROM stage_model_routing; DELETE FROM model_configs;``），不存在需要转换的旧行。
因此这里只加列；若未来仍有旧口径行，``pricing.py`` 保留了旧键的兼容解析（按 ``price_unit``
精确换算到每百万 token，属算术换算而非估算）。

回滚
----
``downgrade()`` 删列。JSONB 内的 pricing 结构不受 DDL 影响（回滚后旧代码会读到 null 单价，
表现为 ``cost_usd=null`` + ``unit_price_missing`` 告警，属如实降级，不会产生错数）。
"""

from __future__ import annotations

from alembic import op

revision = "0006_model_config_type"
down_revision = "0005_app_settings"
branch_labels = None
depends_on = None

TABLE = "model_configs"
COLUMN = "type"

_UP = f"""
ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} VARCHAR(32);
COMMENT ON COLUMN {TABLE}.{COLUMN} IS
    'Endpoint type: openai | anthropic | ... ; NULL = 按 OpenAI 兼容处理'
"""

_DOWN = f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {COLUMN}"


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
