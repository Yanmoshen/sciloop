# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""ORM 模型汇总入口。

**导入本模块即注册全部 30 张表**（附录 A.0 顺序），alembic 的 ``env.py``
与任何需要完整 ``Base.metadata`` 的场景都必须先 ``import db.models``。

补充（全文阅读器，迁移 ``0003_reader_library``）
------------------------------------------------
``reader_documents`` / ``reader_versions`` / ``reader_states`` / ``reader_annotations``
四张表由 ``app/db/models/reader.py`` 定义，**不属于附录 A.0 的 30 张表**，因此
``TABLE_BUILD_ORDER`` 保持原样（该常量是附录 A.0 的权威顺序，已被
``scripts/verify/empty_db_migration.sh`` 与既有工作包依赖，不随新模块变动）。
新表由 alembic 迁移独立建/删，导入顺序与外键依赖为
``reader_documents → （papers）``、``reader_versions → reader_documents``、
``reader_annotations → reader_documents, reader_versions``。

补充（全局设置，迁移 ``0005_app_settings``）
--------------------------------------------
``app_settings``（``scope`` 主键 + ``value`` JSONB）由 ``app/db/models/settings.py`` 定义，
同样**不属于附录 A.0 的 30 张表**，``TABLE_BUILD_ORDER`` 保持原样不变。
该表**没有任何外键**，建/删顺序无依赖。

补充（研究节点编排层，迁移 ``0008_research_nodes``）
---------------------------------------------------
``research_node_runs`` / ``research_node_transitions`` 由
``app/db/models/research.py`` 定义，同样**不属于附录 A.0 的 30 张表**，
``TABLE_BUILD_ORDER`` 保持原样不变。两表都只依赖 ``projects``，
建表顺序为 ``research_node_runs`` → ``research_node_transitions``（互不依赖）。
本层**不新增业务实体表**：证据落 ``evidences``、假设落 ``ideas``、
可行性落 ``feasibilities``、实验协议落 ``taskbooks``。
"""

from __future__ import annotations

from db.base import Base
from db.models.aggregation import Aggregation, Evidence, Gap, Idea
from db.models.feasibility import Feasibility, Taskbook
from db.models.paper import (
    Paper,
    PaperCard,
    PaperDocument,
    PaperFeedSnapshot,
    PaperIdentity,
    PaperSourceRecord,
    PaperSpan,
)
from db.models.pipeline import (
    Experiment,
    ExperimentMetric,
    ExperimentRun,
    PipelineRun,
    StageOutput,
)
from db.models.project import Project
from db.models.reader import (
    ReaderAnnotation,
    ReaderDocument,
    ReaderState,
    ReaderVersion,
)
from db.models.research import (
    IMPLEMENTED_NODES,
    NODE_STATUSES,
    RESEARCH_NODES,
    TRANSITION_KINDS,
    TRANSITION_TRIGGERS,
    ResearchNodeRun,
    ResearchNodeTransition,
)
from db.models.review import (
    DecisionLog,
    DemoFixture,
    DraftClaim,
    ExperimentPassport,
    Intervention,
    LlmCallLog,
    ModelConfig,
    PaperDraft,
    ReviewCalibration,
    ReviewScore,
    StageModelRouting,
)
from db.models.settings import AppSetting

# 附录 A.0 权威建表顺序（projects 首表且先不含 taskbook_id）
TABLE_BUILD_ORDER: tuple[str, ...] = (
    "projects",
    "papers",
    "paper_identities",
    "paper_documents",
    "paper_source_records",
    "paper_cards",
    "paper_spans",
    "paper_feed_snapshots",
    "aggregations",
    "gaps",
    "ideas",
    "feasibilities",
    "taskbooks",
    "pipeline_runs",
    "stage_outputs",
    "experiments",
    "experiment_runs",
    "experiment_metrics",
    "review_scores",
    "decision_logs",
    "interventions",
    "evidences",
    "llm_call_logs",
    "model_configs",
    "stage_model_routing",
    "paper_drafts",
    "draft_claims",
    "experiment_passports",
    "review_calibrations",
    "demo_fixtures",
)

# 迁移末段用 ALTER TABLE 补齐的外键（循环外键 + 后建表外键）
DEFERRED_FOREIGN_KEYS: tuple[str, ...] = (
    "fk_projects_taskbook",
    "fk_projects_idea",
    "fk_paper_cards_llm_call",
    "fk_evidences_passport",
    "fk_spans_document",
)

__all__ = [
    "DEFERRED_FOREIGN_KEYS",
    "IMPLEMENTED_NODES",
    "NODE_STATUSES",
    "RESEARCH_NODES",
    "TABLE_BUILD_ORDER",
    "TRANSITION_KINDS",
    "TRANSITION_TRIGGERS",
    "Aggregation",
    "AppSetting",
    "Base",
    "DecisionLog",
    "DemoFixture",
    "DraftClaim",
    "Evidence",
    "Experiment",
    "ExperimentMetric",
    "ExperimentPassport",
    "ExperimentRun",
    "Feasibility",
    "Gap",
    "Idea",
    "Intervention",
    "LlmCallLog",
    "ModelConfig",
    "Paper",
    "PaperCard",
    "PaperDocument",
    "PaperDraft",
    "PaperFeedSnapshot",
    "PaperIdentity",
    "PaperSourceRecord",
    "PaperSpan",
    "PipelineRun",
    "Project",
    "ReaderAnnotation",
    "ReaderDocument",
    "ReaderState",
    "ReaderVersion",
    "ResearchNodeRun",
    "ResearchNodeTransition",
    "ReviewCalibration",
    "ReviewScore",
    "StageModelRouting",
    "StageOutput",
    "Taskbook",
]
