# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""导入本模块注册全部 ORM 模型，供 SQLite 启动初始化使用。"""

from __future__ import annotations

from db.base import Base
from db.models.aggregation import Aggregation, Evidence, Gap, Idea
from db.models.feasibility import Feasibility, Taskbook
from db.models.idea_pipeline import IdeaPipelineCandidate, IdeaPipelineExpertRun, IdeaPipelineRun
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
    "idea_pipeline_runs",
    "idea_pipeline_experts",
    "idea_pipeline_candidates",
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
    "IdeaPipelineCandidate",
    "IdeaPipelineExpertRun",
    "IdeaPipelineRun",
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
