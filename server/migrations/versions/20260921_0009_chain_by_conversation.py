# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""研究链改挂**对话**：两表加 ``conversation_id``，``project_id`` 降为可空元信息。

为什么改
--------
0008 把研究链挂在 ``project_id`` 上（「一个项目一条链」）。实测使用者要的是
**「一个对话一条链」**，而且：

- 对话可以没有项目（用户是在对话里说「开始文献调研」，不是在项目里点按钮）；
  原表 ``project_id NOT NULL`` 会让「无项目对话」根本跑不了节点；
- 反过来，在对话里替用户自动建一个项目等于**替他造数据** —— 明确不做。

所以：
1. ``conversation_id`` 成为链的身份（会话 id 来自 ``.data/conversations/`` 的文件名，
   形如 ``6f8fc39d673d4341``，定长 16，这里给 64 留余量）；
2. ``project_id`` 改可空，**只当元信息**（有就记，便于以后按项目汇总）；
3. 唯一键从 ``(project_id, node, entry_index)`` 改为
   ``(conversation_id, node, entry_index)``，且用**部分唯一索引**（``WHERE conversation_id
   IS NOT NULL``）：0008/0009 之前的历史行没有对话归属，不能因此互相冲突。

回滚
----
``downgrade()`` 把唯一键改回 ``(project_id, node, entry_index)`` 并删掉新列。
注意：回滚会丢掉对话归属信息，且**若存在无项目的新数据会因 NOT NULL 失败** ——
所以 downgrade 前会先把 project_id 为空的行删掉（如实记录在下面的 SQL 注释里）。
"""

from __future__ import annotations

from alembic import op

#: ⚠️ 长度上限 32：``alembic_version.version_num`` 是 ``varchar(32)``。
#: 原来的 `0009_research_chain_by_conversation`（35 字符）会让迁移在
#: **最后写版本号时**抛 StringDataRightTruncation 并整条回滚——DDL 白写了，
#: 版本号还停在 0008。测试 test_migration_revision_ids_fit_column 锁死这条。
revision = "0009_chain_by_conversation"
down_revision = "0008_research_nodes"
branch_labels = None
depends_on = None

_UP = """
-- 1) 两表加对话归属（可空：历史行没有对话）
ALTER TABLE research_node_runs
    ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(64);
ALTER TABLE research_node_transitions
    ADD COLUMN IF NOT EXISTS conversation_id VARCHAR(64);

-- 2) project_id 降为可空元信息
ALTER TABLE research_node_runs
    ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE research_node_transitions
    ALTER COLUMN project_id DROP NOT NULL;

-- 3) 唯一键换成「对话 + 节点 + 第几次进入」
--    0008 用 UniqueConstraint 建的是**约束**（不是裸索引），必须 DROP CONSTRAINT
ALTER TABLE research_node_runs
    DROP CONSTRAINT IF EXISTS uq_node_run_once;
DROP INDEX IF EXISTS uq_node_run_once;
--    部分唯一索引：只约束有对话归属的行，历史行（conversation_id IS NULL）不受影响
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_run_once
    ON research_node_runs(conversation_id, node, entry_index)
    WHERE conversation_id IS NOT NULL;

-- 4) 链查询索引（同时清掉 0008 按 project 建的旧索引，避免 schema 与 ORM 漂移）
DROP INDEX IF EXISTS idx_node_runs_project;
DROP INDEX IF EXISTS idx_transitions_project;
CREATE INDEX IF NOT EXISTS idx_node_runs_chain
    ON research_node_runs(conversation_id, node);
CREATE INDEX IF NOT EXISTS idx_transitions_chain
    ON research_node_transitions(conversation_id, created_at);

COMMENT ON COLUMN research_node_runs.conversation_id IS
    'Owning conversation id; the chain identity is (conversation_id, node, entry_index)';
COMMENT ON COLUMN research_node_runs.project_id IS
    'Optional metadata only; a conversation may have no project at all';
COMMENT ON COLUMN research_node_transitions.conversation_id IS
    'Owning conversation id; NULL only for pre-0009 historical rows';
"""

# 回滚：project_id 要恢复 NOT NULL，因此先把无项目的行删掉（属于不可逆的数据损失，如实说明）
_DOWN = """
DELETE FROM research_node_transitions WHERE project_id IS NULL;
DELETE FROM research_node_runs WHERE project_id IS NULL;

DROP INDEX IF EXISTS idx_transitions_chain;
DROP INDEX IF EXISTS idx_node_runs_chain;
DROP INDEX IF EXISTS uq_node_run_once;
CREATE UNIQUE INDEX IF NOT EXISTS uq_node_run_once
    ON research_node_runs(project_id, node, entry_index);

ALTER TABLE research_node_runs DROP COLUMN IF EXISTS conversation_id;
ALTER TABLE research_node_transitions DROP COLUMN IF EXISTS conversation_id;

ALTER TABLE research_node_runs ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE research_node_transitions ALTER COLUMN project_id SET NOT NULL;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
