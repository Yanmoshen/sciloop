# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""论文域契约：``papers`` / ``paper_identities`` / ``paper_documents`` /
``paper_spans`` / ``paper_source_records`` / ``paper_cards`` / ``paper_feed_snapshots``。

字段来源：附录 A.2（建表 DDL）+ 附录 B.1（``/papers/*`` 响应示例）。

两个必须保留的口径
------------------

1. ``institution_score`` 与 ``llm_novelty`` **仅展示**，不进入任何排序／评分计算
   （contracts.ranking_and_influence.excluded_from_any_score）；本包保留字段是为了
   文档与前端能显示并标注，而不是让它们参与计算。
2. 排序分项一律带 ``source`` 与 ``confidence``（contracts.ranking_and_influence.
   confidence_labels），缺失分项必须 ``null`` 并披露 ``score_coverage``，禁止编造。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field

from schemas.base import Paginated, SciLoopModel
from schemas.enums import IdType, ParseStatus, SpanVerdict

__all__ = [
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
]


# --------------------------------------------------------------------------- #
# 分值分项
# --------------------------------------------------------------------------- #
class ScoreFactor(SciLoopModel):
    """单个分项分：**值 + 来源 + 置信度**三元组（缺一即视为不完整）。"""

    value: float | None = Field(default=None, description="归一后 0-100；缺失必须为 null")
    source: str | None = Field(default=None, description="如 bm25 / openalex / fulltext_parser")
    confidence: float | None = Field(default=None, ge=0, le=1, description="0-1")


class RankBreakdown(SciLoopModel):
    """主排序四维（weights: 0.40/0.25/0.20/0.15）。"""

    relevance: ScoreFactor | None = None
    recency: ScoreFactor | None = None
    citation_trend: ScoreFactor | None = None
    evidence_completeness: ScoreFactor | None = None


class InfluenceBreakdown(SciLoopModel):
    """影响力辅助三分项（weights: 0.40/0.40/0.20），仅展示。"""

    venue: ScoreFactor | None = None
    citation_velocity: ScoreFactor | None = None
    code_heat: ScoreFactor | None = None


class LlmNoveltyTag(SciLoopModel):
    """LLM 新颖性标签：**辅助标签**，必须带区间与依据，不得伪装成客观分。"""

    value: float | None = Field(default=None, ge=0, le=100)
    low: float | None = Field(default=None, description="不确定性下界")
    high: float | None = Field(default=None, description="不确定性上界")
    stable: bool | None = Field(
        default=None,
        description="两次调用差 ≤ LLM_NOVELTY_STABILITY_TOLERANCE 为 true；false 时只展示区间",
    )
    note: str | None = Field(default=None, description="一句话依据（必填口径，缺则视为不完整）")
    model: str | None = Field(default=None, description="产出该标签的模型 id")


class FulltextSummary(SciLoopModel):
    """论文库里内嵌的全文可用性摘要（前端 ``CoverageTag`` 的数据源）。"""

    parse_status: ParseStatus | None = None
    coverage: float | None = Field(default=None, ge=0, le=1)
    document_version: str | None = None


class Author(SciLoopModel):
    """``papers.authors[]``（JSONB）。"""

    name: str
    affiliation: str | None = None
    institution_id: str | None = None


# --------------------------------------------------------------------------- #
# 论文主表与身份
# --------------------------------------------------------------------------- #
class Paper(SciLoopModel):
    """``papers`` 全字段（附录 A.2）。"""

    id: int | None = None
    source: str = Field(description="arxiv | semantic_scholar | openalex")
    external_id: str = Field(description="arXiv id / S2 paperId / OpenAlex Work ID")
    doi: str | None = None

    title: str
    abstract: str | None = None
    authors: list[Author] | None = None
    published_at: date | None = None
    updated_at_src: date | None = None

    venue: str | None = None
    venue_source: str | None = Field(
        default=None, description="s2 | openalex | arxiv_comment | whitelist"
    )
    venue_level: int | None = Field(
        default=None, ge=0, le=4, description="顶会等级 0-4；NULL = 未知"
    )

    citation_count: int | None = 0
    citation_velocity: float | None = Field(default=None, description="引用数 / 月")
    code_url: str | None = None
    code_heat: float | None = None

    # ---- 仅展示，禁止进入任何排序/评分计算 ----
    institution_score: float | None = Field(default=None, description="【仅展示】不进任何分数")
    llm_novelty: float | None = Field(default=None, description="【仅展示标签】不进任何分数")
    llm_novelty_low: float | None = None
    llm_novelty_high: float | None = None
    llm_novelty_note: str | None = None
    llm_novelty_stable: bool | None = None

    # ---- 排序与影响力 ----
    rank_score: float | None = None
    rank_breakdown: RankBreakdown | None = None
    influence_score: float | None = Field(default=None, description="辅助展示分，不用于默认排序")
    score_breakdown: InfluenceBreakdown | None = None
    score_coverage: float | None = Field(
        default=None, ge=0, le=1, description="取数完整度（可用分项权重和）"
    )

    pdf_url: str | None = None
    is_parsed: bool = False
    raw: dict[str, Any] | None = Field(default=None, description="多源合并前的原始响应留档")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaperIdentity(SciLoopModel):
    """``paper_identities``（v1.2：防同文多源重复入库）。"""

    id: int | None = None
    paper_id: int
    id_type: IdType
    id_value: str
    is_primary: bool = False
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 全文解析与定位
# --------------------------------------------------------------------------- #
class SourceRecord(SciLoopModel):
    """``paper_source_records``：每个字段一条留痕，支撑「可追溯 / 可解释」。"""

    id: int | None = None
    paper_id: int
    source: str = Field(description="arxiv | semantic_scholar | openalex | github | llm")
    field_name: str = Field(
        description="venue | citation_count | citation_velocity | code_heat | institution | llm_novelty"
    )
    raw_value: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    request_url: str | None = None
    http_status: int | None = None
    fetched_at: datetime | None = None
    created_at: datetime | None = None


class PaperDocument(SciLoopModel):
    """``paper_documents``：证据定位的地基（``document_version`` 是唯一定位前提）。"""

    id: int | None = None
    paper_id: int
    document_version: str = Field(description="源 URL + 内容 SHA-256 前 12 位")
    source_type: str = Field(description="html | pdf | abstract_only")
    source_url: str
    parser: str = Field(description="ar5iv_html | arxiv_html | pymupdf | pdfminer")
    parser_version: str
    page_count: int | None = None
    text_sha256: str
    char_count: int | None = None
    locatable_chars: int | None = None
    coverage: float | None = Field(default=None, description="locatable_chars / char_count")
    parse_status: ParseStatus
    parse_error: str | None = None
    parsed_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def fulltext_usable(self) -> bool:
        """全文闸门：``parse_status='ok'`` 且 ``coverage>=0.60`` 才可生成 paper_span 证据。"""
        return self.parse_status == ParseStatus.OK and (self.coverage or 0) >= 0.60


class BBox(SciLoopModel):
    """``paper_spans.bbox``；无可靠坐标时为 ``None``（HTML 版本无分页坐标）。"""

    x0: float
    y0: float
    x1: float
    y1: float


class EvidenceVerification(SciLoopModel):
    """``services.fulltext.locator.verify_span`` 的返回口径。

    ``verdict`` 三态语义见 contracts.evidence_rules.verification_verdicts：
    先比 ``quote_sha256``，哈希命中但偏移不可校验时为 ``valid_by_hash``
    （**不得伪报 ``valid``**）。
    """

    verdict: SpanVerdict
    hash_match: bool
    offset_match: bool | None = Field(default=None, description="null = 无全文缓存，无法比对偏移")
    expected_quote_sha256: str | None = None
    stored_quote_sha256: str | None = None
    reason: str | None = None


class PaperSpan(SciLoopModel):
    """``paper_spans``：引用定位片段（前端高亮与证据校验的唯一依据）。"""

    id: int | None = None
    paper_id: int
    document_version: str = Field(description="**必填**：裸偏移不得作为唯一定位依据")
    section_name: str | None = Field(
        default=None, description="abstract | method | experiment | conclusion"
    )
    page_number: int | None = Field(
        default=None, description="HTML 版本恒为 1（HTML 渲染不分页），前端不得当作物理页码"
    )
    bbox: BBox | None = None
    char_start: int
    char_end: int
    quote_text: str
    quote_sha256: str
    created_at: datetime | None = None


class CardEvidenceSpan(SciLoopModel):
    """卡片条目内嵌的 ``evidence_span``（WP06 ``locator.to_evidence_span`` 的落库口径）。"""

    evidence_type: str = "paper_span"
    paper_id: int | None = None
    document_version: str | None = None
    span_id: int | None = None
    section_name: str | None = None
    page_number: int | None = None
    bbox: BBox | None = None
    char_start: int | None = None
    char_end: int | None = None
    quote_text: str | None = None
    quote_sha256: str | None = None
    source_span_char_span: list[int] | None = None
    source_span_verdict: SpanVerdict | None = None
    verification: EvidenceVerification | None = None
    locator: dict[str, Any] | None = Field(
        default=None, description="定位审计信息（field/index/match_*）"
    )


# --------------------------------------------------------------------------- #
# 解析卡片（8 字段）
# --------------------------------------------------------------------------- #
class KeyInnovationEntry(SciLoopModel):
    point: str
    evidence_span: CardEvidenceSpan | None = Field(
        default=None, description="核对不上必须为 null，禁止编造引用"
    )


class TechnicalRouteStep(SciLoopModel):
    step: str | int
    description: str


class ExperimentalSetup(SciLoopModel):
    """``paper_cards.experimental_setup`` 的契约三键；额外键（如 ``available_scope``）原样保留。"""

    model_config = SciLoopModel.model_config | {"extra": "allow"}

    datasets: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    available_scope: str | None = Field(
        default=None, description="fulltext | abstract_only（降级必须披露）"
    )


class MainConclusionEntry(SciLoopModel):
    conclusion: str
    evidence_span: CardEvidenceSpan | None = None


class TransferableEntry(SciLoopModel):
    point: str
    target_problem: str | None = None


class PaperCard(SciLoopModel):
    """``paper_cards``（8 字段 + 版本）；``force`` 重解析生成新 ``version``。"""

    id: int | None = None
    paper_id: int
    version: int = 1
    research_problem: str
    core_method: str
    key_innovation: list[KeyInnovationEntry] = Field(default_factory=list)
    technical_route: list[TechnicalRouteStep] = Field(default_factory=list)
    experimental_setup: ExperimentalSetup
    main_conclusions: list[MainConclusionEntry] = Field(default_factory=list)
    limitations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="[{limitation, evidence_span}]（条目可为字符串，故用宽松 dict）",
    )
    transferable: list[TransferableEntry] = Field(default_factory=list)
    llm_call_log_id: int | None = Field(
        default=None, description="取证用：最后一次成功 LLM 调用 id"
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PaperFeedSnapshot(SciLoopModel):
    """``paper_feed_snapshots``：离线演示首屏与三视图可复现。"""

    id: int | None = None
    view_type: str = Field(description="recommended | influence | latest")
    filters: dict[str, Any] | None = None
    paper_ids: list[int] = Field(default_factory=list, description="有序 id 数组")
    is_demo: bool = False
    created_at: datetime | None = None


# --------------------------------------------------------------------------- #
# 论文库（附录 B.1 响应示例）
# --------------------------------------------------------------------------- #
class PaperFeedItem(SciLoopModel):
    """``GET /papers/feed`` 的单条 item。"""

    id: int
    title: str
    published_at: date | None = None
    venue: str | None = None
    venue_source: str | None = None
    venue_level: int | None = None
    rank_score: float | None = None
    influence_score: float | None = None
    score_coverage: float | None = None
    rank_breakdown: RankBreakdown | None = None
    score_breakdown: InfluenceBreakdown | None = None
    llm_novelty_tag: LlmNoveltyTag | None = None
    fulltext: FulltextSummary | None = None
    is_parsed: bool = False


class PaperFeedResponse(Paginated[PaperFeedItem]):
    """``GET /papers/feed`` 响应：分页体 + ``data_source``（前端据此显示 DemoBadge）。"""

    data_source: str = Field(default="live", description="live | snapshot | replay")


# --------------------------------------------------------------------------- #
# 请求体（附录 B.1）
# --------------------------------------------------------------------------- #
class PaperFetchRequest(SciLoopModel):
    """``POST /papers/fetch``。"""

    fields: list[str] = Field(default_factory=list, description="arXiv 分类，如 cs.AI")
    date_from: date | None = None
    limit: int = Field(default=50, ge=1, le=1000)
    sources: list[str] = Field(
        default_factory=list, description="arxiv | semantic_scholar | openalex"
    )


class PaperParseRequest(SciLoopModel):
    """``POST /papers/{id}/parse``（长任务，返回 ``task_id``）。"""

    force: bool = Field(default=False, description="true = 忽略已有卡片，生成新 version")
