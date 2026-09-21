# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""研究节点编排层：``research_node_runs`` + ``research_node_transitions``。

为什么加
--------
现有流水线是**固定六阶段顺序**（``contracts.enums.pipeline_stage``；
``app/services/pipeline/stages/base.py:STAGE_ORDER``），无图结构，
唯一的回退是 ``plan_review → plan`` 写死的一种。

研究节点编排层需要的是**有条件的流程图**：七个研究节点、文献与 idea 双向进入、
失败回退、以及每次回退可审计。这无法用固定顺序表达，因此**另开两张表**，
而不是改 ``stage_outputs``（改它等于动已有 51 条测试与既有迁移语义）。

与既有表的分工
--------------
本层**不新增业务实体表**——证据、idea、可行性、实验协议全部复用既有表：

- 证据 → ``evidences``（多态 ``owner_type``/``owner_id``，含 ``card_field`` 与
  ``paper_span_id``，天然满足「证据必须带可定位来源」）
- 假设 → ``ideas``；可行性 → ``feasibilities``；实验协议 → ``taskbooks``

本迁移只加两张**纯状态表**：

``research_node_runs``
    节点实例。``uq_node_run_once`` 唯一键为 ``(project_id, node, entry_index)``，
    是幂等的基石：同一节点同一次进入只有一行，重复提交走 ``ON CONFLICT DO UPDATE``，
    不会出现两条 ``running``，也不会重复扣预算（与 ``stage_outputs.uq_stage_once`` 同款做法）。
    ``entry_index`` 是「第几次进入本节点」（回退后再进 +1，不新建链）；
    ``retry_count`` 是「本次进入内的修复重试次数」（0..2）。

``research_node_transitions``
    迁移留痕。每次 advance / revert / retry / stop 一行，记录触发方
    （``program`` / ``model`` / ``researcher``）、原因、**回退必带信息**
    （``required_carried``，经闸门校验通过后才写）与当时的预算快照。
    这是「程序不拦要不要退、只拦退得规不规范」这条口径的唯一证据来源。

回滚
----
``downgrade()`` 删两张表。编排层状态丢失属于元数据损失，业务数据
（论文、证据、idea、任务书）完整，属可接受回滚。
"""

from __future__ import annotations

from alembic import op

revision = "0008_research_nodes"
down_revision = "0007_project_archived"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS research_node_runs (
    id              BIGSERIAL PRIMARY KEY,
    project_id      BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node            VARCHAR(48) NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'pending',
    entry_index     INTEGER NOT NULL DEFAULT 1,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_output      TEXT,
    validation      JSONB,
    evidence_refs   JSONB,
    cost_usd        NUMERIC(10,6) NOT NULL DEFAULT 0,
    llm_call_count  INTEGER NOT NULL DEFAULT 0,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_node_run_once
    ON research_node_runs(project_id, node, entry_index);
CREATE INDEX IF NOT EXISTS idx_node_runs_project
    ON research_node_runs(project_id, node);

CREATE TABLE IF NOT EXISTS research_node_transitions (
    id               BIGSERIAL PRIMARY KEY,
    project_id       BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_node        VARCHAR(48),
    to_node          VARCHAR(48) NOT NULL,
    kind             VARCHAR(16) NOT NULL,
    trigger          VARCHAR(16) NOT NULL,
    reason           TEXT NOT NULL,
    required_carried JSONB,
    budget_snapshot  JSONB,
    actor            VARCHAR(16) NOT NULL DEFAULT 'system',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transitions_project
    ON research_node_transitions(project_id, created_at);

COMMENT ON TABLE research_node_runs IS
    'Research node instance: uq(project_id,node,entry_index) guarantees idempotency';
COMMENT ON COLUMN research_node_runs.node IS
    'literature_review | idea_and_feasibility | experiment_and_data_preparation | ...';
COMMENT ON COLUMN research_node_runs.status IS
    'pending | running | waiting_human | done | failed | blocked';
COMMENT ON COLUMN research_node_runs.entry_index IS
    'Times this node was entered (revert re-entry +1); never creates a new chain';
COMMENT ON COLUMN research_node_runs.retry_count IS
    'Repair retries within the current entry, 0..2; exhausted -> waiting_human';
COMMENT ON COLUMN research_node_runs.validation IS
    'Last validation result: failed rule ids + human readable items';
COMMENT ON COLUMN research_node_transitions.trigger IS
    'program | model | researcher';
COMMENT ON COLUMN research_node_transitions.kind IS
    'advance | revert | retry | stop';
COMMENT ON COLUMN research_node_transitions.required_carried IS
    'Information carried by a revert; written only after the gate passes';
"""

_DOWN = """
DROP INDEX IF EXISTS idx_transitions_project;
DROP TABLE IF EXISTS research_node_transitions;
DROP INDEX IF EXISTS idx_node_runs_project;
DROP INDEX IF EXISTS uq_node_run_once;
DROP TABLE IF EXISTS research_node_runs;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
