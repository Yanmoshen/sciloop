# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""受控值域（枚举）：与 ``contracts.json`` → ``enums`` 一一对应。

为什么单独成模块
----------------

附录 A 约定「枚举用 ``VARCHAR + CHECK``」，因此库里存的是字符串；把值域做成
``StrEnum`` 后，router / 服务 / 前端类型 / 测试断言可以共用同一份常量，
避免各处硬编码字面量后出现拼写漂移（例如 ``valid_by_hash`` 写成 ``validByHash``）。
``StrEnum`` 继承 ``str``，可直接与数据库返回值比较、可 JSON 序列化。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
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


class ProjectStatus(StrEnum):
    """``projects.status``（附录 A.1）。"""

    DRAFT = "DRAFT"
    TASKBOOK_LOCKED = "TASKBOOK_LOCKED"
    RUNNING = "RUNNING"
    RISK_EVALUATING = "RISK_EVALUATING"
    WAIT_HUMAN = "WAIT_HUMAN"
    CIRCUIT_BREAK = "CIRCUIT_BREAK"
    REVIEWING = "REVIEWING"
    DONE = "DONE"
    ABORTED = "ABORTED"


class ProjectMode(StrEnum):
    """``projects.mode`` / ``pipeline_runs.mode``。"""

    MANUAL = "manual"
    AUTO = "auto"


class PipelineStage(StrEnum):
    """六环节（附录 A.5 ``stage_outputs.stage``）。"""

    SURVEY = "survey"
    PLAN = "plan"
    PLAN_REVIEW = "plan_review"
    EXPERIMENT = "experiment"
    WRITING = "writing"
    REVIEW = "review"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    DONE = "done"
    FAILED = "failed"


class DecisionPoint(StrEnum):
    """D1–D6（contracts.decision_points）。"""

    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"
    D5 = "D5"
    D6 = "D6"


class PolicyAction(StrEnum):
    """风险策略三种动作（contracts.risk_policy_rules.action_rules）。"""

    AUTO_EXECUTE = "auto_execute"
    NEED_HUMAN = "need_human"
    CIRCUIT_BREAK = "circuit_break"


class InterventionNode(StrEnum):
    """N1–N4（contracts.intervention_mapping）。"""

    N1 = "N1"
    N2 = "N2"
    N3 = "N3"
    N4 = "N4"


class InterventionAction(StrEnum):
    APPROVE = "approve"
    MODIFY = "modify"
    REJECT = "reject"
    RERUN = "rerun"
    DOWNGRADE = "downgrade"
    ABORT = "abort"
    SWITCH_MODE = "switch_mode"


class ClaimStatus(StrEnum):
    """``draft_claims.support_status``（三态，禁留空）。"""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"


class ParseStatus(StrEnum):
    """``paper_documents.parse_status``。"""

    OK = "ok"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class SpanVerdict(StrEnum):
    """``quote_sha256`` 优先、偏移次之（contracts.evidence_rules.verification_verdicts）。"""

    VALID = "valid"
    VALID_BY_HASH = "valid_by_hash"
    INVALID = "invalid"


class IdType(StrEnum):
    """``paper_identities.id_type``（v1.2 统一身份）。"""

    DOI = "doi"
    ARXIV = "arxiv"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENALEX = "openalex"
    TITLE_HASH = "title_hash"


class EvidenceType(StrEnum):
    """``evidences.evidence_type``（contracts.evidence_rules.evidence_types）。"""

    PAPER_SPAN = "paper_span"
    CARD_FIELD = "card_field"
    EXPERIMENT_RUN = "experiment_run"
    EXPERIMENT_PASSPORT = "experiment_passport"
    DECISION = "decision"


class Verdict(StrEnum):
    """盲评结论（``plan_review``）。"""

    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"


class FailureLevel(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class StopReason(StrEnum):
    SCORE_THRESHOLD = "score_threshold"
    MARGINAL_STAGNATION = "marginal_stagnation"
    MAX_ITERATIONS = "max_iterations"
    MANUAL = "manual"


class TemplateId(StrEnum):
    """实验模板 T1–T5（P0 只要求 T1 / T3）。"""

    T1_PROMPT_VARIANT = "T1_prompt_variant"
    T2_FEWSHOT_ABLATION = "T2_fewshot_ablation"
    T3_MODEL_COMPARE = "T3_model_compare"
    T4_LLM_AS_JUDGE = "T4_llm_as_judge"
    T5_RAG_ABLATION = "T5_rag_ablation"


class PassportStatus(StrEnum):
    """任一关键字段缺失必须 ``incomplete``，禁止宣称可复现。"""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class AgreementMetric(StrEnum):
    COHEN_KAPPA = "cohen_kappa"
    MAE = "mae"


class CalibrationStatus(StrEnum):
    PENDING = "pending"
    CALIBRATED = "calibrated"


class AccessMode(StrEnum):
    PUBLIC_DEMO = "public_demo"
    OWNER_MODE = "owner_mode"
