# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""研究节点编排层 ORM 模型（迁移 ``0008_research_nodes``）。

两张表都是**纯状态表**，不承载业务实体：

- 证据落 ``evidences``、假设落 ``ideas``、可行性落 ``feasibilities``、
  实验协议落 ``taskbooks`` —— 全部复用既有表（见迁移 0008 的说明）。
- 本层只记录「哪个节点、第几次进入、什么状态、校验结果、花了多少」，
  以及「从哪个节点迁到哪个节点、为什么、带了什么信息」。

``uq_node_run_once`` = ``(project_id, node, entry_index)`` 是幂等基石：
回退只让 ``entry_index`` 递增，**永不新建链**；重复提交走
``ON CONFLICT DO UPDATE``，不会出现两条 ``running``，也不会重复计成本。
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

BIGINT = BigInteger

#: 七个研究节点（顺序即推荐推进顺序；回退边见 services/research/graph.py）
RESEARCH_NODES: tuple[str, ...] = (
    "literature_review",
    "idea_and_feasibility",
    "experiment_and_data_preparation",
    "experiment_execution_and_retries",
    "results_analysis",
    "paper_writing",
    "paper_review",
)

#: 首版实装校验的三个节点（其余为占位，允许进入但校验直接放行）
IMPLEMENTED_NODES: tuple[str, ...] = (
    "literature_review",
    "idea_and_feasibility",
    "experiment_and_data_preparation",
)

#: 节点状态（程序内部六态；文档五态在展示层映射，见 services/research/graph.py）
NODE_STATUSES: tuple[str, ...] = (
    "pending",
    "running",
    "waiting_human",
    "done",
    "failed",
    "blocked",
)

#: 迁移种类与触发方
TRANSITION_KINDS: tuple[str, ...] = ("advance", "revert", "retry", "stop")
TRANSITION_TRIGGERS: tuple[str, ...] = ("program", "model", "researcher")


class ResearchNodeRun(Base):
    """节点实例（一次「进入」一行）。"""

    __tablename__ = "research_node_runs"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    node: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    #: 第几次进入本节点（回退后再进 +1；不新建链）
    entry_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    #: 本次进入内的修复重试次数（0..2）
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: 结构化交接块（契约输出，已通过校验）
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="'{}'::jsonb"
    )
    #: 模型原始输出（排查用；未通过校验时也要留）
    raw_output: Mapped[str | None] = mapped_column(Text)
    #: 最近一次校验结果：{"ok": bool, "level": "L1|L2", "items": [...], "rules": [...]}
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: 本次产出的 evidences.id 列表（可追溯）
    evidence_refs: Mapped[list[Any] | None] = mapped_column(JSONB)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, server_default="0")
    llm_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "node", "entry_index", name="uq_node_run_once"),
        Index("idx_node_runs_project", "project_id", "node"),
    )


class ResearchNodeTransition(Base):
    """节点迁移留痕（每次 advance / revert / retry / stop 一行）。"""

    __tablename__ = "research_node_transitions"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    from_node: Mapped[str | None] = mapped_column(String(48))
    to_node: Mapped[str] = mapped_column(String(48), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: 回退必带信息（闸门 G1 校验通过后才写）：重合证据 / 仍存差异 / 是否值得继续
    required_carried: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: 当时的重试 / 回退 / 成本快照（闸门 G2 的依据）
    budget_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(16), nullable=False, server_default="system")
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_transitions_project", "project_id", "created_at"),)
