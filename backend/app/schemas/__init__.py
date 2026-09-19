# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``app.schemas`` —— SciLoop 对外契约与文档可引用层（Pydantic v2）。

模块划分（与附录 A 的域一一对应）
--------------------------------

================  =====================================================================
模块              内容
================  =====================================================================
``base``          基类 ``SciLoopModel``、分页 ``Paginated[T]`` / ``PageParams``、
                  统一错误体 ``ErrorBody``、长任务受理 ``TaskAccepted``
``enums``         受控值域（``contracts.json`` → ``enums`` 的 ``StrEnum`` 镜像）
``paper``         ``papers`` / ``paper_identities`` / ``paper_documents`` /
                  ``paper_spans`` / ``paper_source_records`` / ``paper_cards`` /
                  ``paper_feed_snapshots`` + 论文库响应
``aggregation``   ``aggregations`` / ``gaps`` / ``ideas`` / ``evidences``
``feasibility``   ``feasibilities`` / ``taskbooks``
``pipeline``      ``pipeline_runs`` / ``stage_outputs`` / ``experiments`` /
                  ``experiment_runs`` / ``experiment_metrics`` / ``decision_logs`` /
                  ``interventions`` / ``experiment_passports``
``review``        ``review_scores`` / ``review_calibrations`` / ``paper_drafts`` /
                  ``draft_claims``
``routing``       ``stage_model_routing``（``/models/routing``）+ 脱敏模型视图
================  =====================================================================

刻意**不**做的事
----------------

- **不改造既有端点**：各 WP 的 router 继续直出 ``dict`` / ORM。本包只在「需要类型化
  JSON 口径」的地方使用（新端点、SSE 载荷、测试断言、前端 ``src/api/*.ts`` 对照），
  避免并行开发期为统一 schema 而引入大范围回归。
- **不做业务校验**：除少量契约红线（如 evidence 必须带锚点、idea 必须带证据）外，
  分项归一、权重计算、闸门判定等一律留在各自 ``app.services.*``，本包不重复实现。

用法::

    from app.schemas import Paginated, Paper, ErrorBody

    page = Paginated[Paper].create(items=[], total=0)
    err = ErrorBody(code="validation_error", message="请求参数校验失败", detail=[])
"""

from __future__ import annotations

from app.schemas.aggregation import (
    Aggregation,
    AggregationCreateRequest,
    ComparisonMatrix,
    Evidence,
    Gap,
    Idea,
    IdeaCreateRequest,
    IdeaGenerateRequest,
    IdeaWithEvidence,
    MethodEvolutionStep,
)
from app.schemas.base import (
    DataSource,
    ErrorBody,
    PageParams,
    Paginated,
    SciLoopModel,
    TaskAccepted,
)

# 受控值域（同名 StrEnum 全量再导出，供 router / 测试 / 前端类型对照使用）
from app.schemas.enums import (
    AccessMode,
    AgreementMetric,
    CalibrationStatus,
    ClaimStatus,
    DecisionPoint,
    EvidenceType,
    FailureLevel,
    IdType,
    InterventionAction,
    InterventionNode,
    ParseStatus,
    PassportStatus,
    PipelineStage,
    PolicyAction,
    ProjectMode,
    ProjectStatus,
    SpanVerdict,
    StageStatus,
    StopReason,
    TemplateId,
    Verdict,
)
from app.schemas.feasibility import (
    ComputeBudget,
    DimensionScore,
    Feasibility,
    FeasibilityRequest,
    RiskItem,
    Taskbook,
    TaskbookCreateRequest,
    TaskbookUpdateRequest,
)
from app.schemas.paper import (
    Author,
    BBox,
    CardEvidenceSpan,
    EvidenceVerification,
    ExperimentalSetup,
    FulltextSummary,
    InfluenceBreakdown,
    KeyInnovationEntry,
    LlmNoveltyTag,
    MainConclusionEntry,
    Paper,
    PaperCard,
    PaperDocument,
    PaperFeedItem,
    PaperFeedResponse,
    PaperFeedSnapshot,
    PaperFetchRequest,
    PaperIdentity,
    PaperParseRequest,
    PaperSpan,
    RankBreakdown,
    ScoreFactor,
    SourceRecord,
    TechnicalRouteStep,
    TransferableEntry,
)
from app.schemas.pipeline import (
    DecisionLog,
    Experiment,
    ExperimentMetric,
    ExperimentPassport,
    ExperimentRun,
    GuardrailChecks,
    InterveneRequest,
    Intervention,
    PipelineCreateRequest,
    PipelineRun,
    PipelineRunRequest,
    StageOutput,
)
from app.schemas.review import (
    CalibrationCommitRequest,
    Claim,
    PaperDraft,
    ReviewCalibration,
    ReviewScore,
    VerifyEvidenceRequest,
)
from app.schemas.routing import ModelConfigBrief, Routing, RoutingUpdateRequest

__all__ = [
    # base
    "DataSource",
    "ErrorBody",
    "PageParams",
    "Paginated",
    "SciLoopModel",
    "TaskAccepted",
    # paper
    "Author",
    "BBox",
    "CardEvidenceSpan",
    "EvidenceVerification",
    "ExperimentalSetup",
    "FulltextSummary",
    "InfluenceBreakdown",
    "KeyInnovationEntry",
    "LlmNoveltyTag",
    "MainConclusionEntry",
    "Paper",
    "PaperCard",
    "PaperDocument",
    "PaperFetchRequest",
    "PaperFeedItem",
    "PaperFeedResponse",
    "PaperFeedSnapshot",
    "PaperIdentity",
    "PaperParseRequest",
    "PaperSpan",
    "RankBreakdown",
    "ScoreFactor",
    "SourceRecord",
    "TechnicalRouteStep",
    "TransferableEntry",
    # aggregation
    "Aggregation",
    "AggregationCreateRequest",
    "ComparisonMatrix",
    "Evidence",
    "Gap",
    "Idea",
    "IdeaCreateRequest",
    "IdeaGenerateRequest",
    "IdeaWithEvidence",
    "MethodEvolutionStep",
    # feasibility
    "ComputeBudget",
    "DimensionScore",
    "Feasibility",
    "FeasibilityRequest",
    "RiskItem",
    "Taskbook",
    "TaskbookCreateRequest",
    "TaskbookUpdateRequest",
    # pipeline
    "DecisionLog",
    "Experiment",
    "ExperimentMetric",
    "ExperimentPassport",
    "ExperimentRun",
    "GuardrailChecks",
    "InterveneRequest",
    "Intervention",
    "PipelineCreateRequest",
    "PipelineRun",
    "PipelineRunRequest",
    "StageOutput",
    # review
    "CalibrationCommitRequest",
    "Claim",
    "PaperDraft",
    "ReviewCalibration",
    "ReviewScore",
    "VerifyEvidenceRequest",
    # routing
    "ModelConfigBrief",
    "Routing",
    "RoutingUpdateRequest",
    # enums
    "AccessMode",
    "AgreementMetric",
    "CalibrationStatus",
    "ClaimStatus",
    "DecisionPoint",
    "EvidenceType",
    "FailureLevel",
    "IdType",
    "InterventionAction",
    "InterventionNode",
    "ParseStatus",
    "PassportStatus",
    "PipelineStage",
    "PolicyAction",
    "ProjectMode",
    "ProjectStatus",
    "SpanVerdict",
    "StageStatus",
    "StopReason",
    "TemplateId",
    "Verdict",
]
