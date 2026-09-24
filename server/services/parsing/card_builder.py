# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""单篇论文的 8 字段结构化解析卡片（WP06-T1/T3/T4/T5，附录 A.2 ``paper_cards``）。

交付物
------
``build_card(paper_id, force=False)``   生成（或复用）卡片并落库，返回审计结果
``get_card(paper_id, version=None)``    读取卡片（默认最新版本）
``list_card_versions(paper_id)``        版本列表（force 重解析后旧版本仍可查）
``submit_card_build(...)``              长任务（``POST /papers/{id}/card`` 用，返回 task_id）

硬口径（不可协商）
------------------
- **8 字段全部必填且非空**：``research_problem`` / ``core_method`` / ``key_innovation[]``
  / ``technical_route[]`` / ``experimental_setup{datasets,baselines,metrics}`` /
  ``main_conclusions[]`` / ``limitations[]`` / ``transferable[]``；数组字段非空数组，
  论文未报告的信息写 ``"未提及"``（如实标注，禁止编造）。
- **结论性条目必须带 evidence**：模型的 ``evidence_quote`` 只是"候选引用"，由
  :mod:`services.parsing.locator` 回原文核对；核对不上就 ``evidence_span=null``。
- **全文闸门**：只有 ``parse_status='ok'`` 且 ``coverage>=0.60`` 才带正文章节，
  否则走摘要级降级（``available_scope='abstract_only'``，定位字段全为 null）。
- **版本管理**：``version = 同 paper 最大值 + 1``；``force=True`` 生成新版本，
  旧版本保留；``papers.is_parsed`` 随成功建卡刷新。
- **调用记账**：每次 LLM 调用写 ``llm_call_logs``（``stage='parse'``、
  ``purpose='card_build'``），并把最后一次成功调用的 id 写入
  ``paper_cards.llm_call_log_id``（双向可追溯）。

审计字段落点
------------
``paper_cards`` 表（附录 A.2）没有审计列。卡片级审计（``available_scope`` /
``unlocated_count`` / 闸门 / 模型 / 调用日志 id）以 ``evidence_meta`` 键**附加**在
``experimental_setup`` JSONB 内（``datasets``/``baselines``/``metrics`` 三个契约键保持不变），
既满足"记录 available_scope"的要求，又不改动任何他人拥有的表结构。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services.fulltext import (
    PaperSpanRecord,
    SqlDocumentRepository,
    summarize_documents,
)
from services.fulltext.text_cache import default_text_cache
from services.parsing.locator import (
    FIELD_TEXT_KEYS,
    FULLTEXT_GATE_COVERAGE,
    UNKNOWN_VALUES,
    LocateReport,
    SpanIndex,
    gate_state,
    locate_card,
    placeholder_entries,
)
from services.parsing.summary import generate_summary

logger = logging.getLogger("sciloop.wp06.card_builder")

#: 「原文没提」的占位值（用户 2026-09-24 口径：卡片一律中文，不再用英文 ``unknown``）。
#: 它同时承担两个职责，改名字必须两边一起改：
#: ① 给研究者看的「这条字段没有原文支撑」；② 让 ``locator.is_usable_quote`` 判定
#: **这条引用不可用** —— 所以它必须同时出现在 ``locator.UNKNOWN_VALUES`` 里。
UNKNOWN_PLACEHOLDER = "未提及"

CARD_BUILDER_VERSION = "1.0.0"

#: 摘要级卡片的 ``available_scope``
SCOPE_ABSTRACT_ONLY = "abstract_only"
#: 全文级卡片的 ``available_scope``
SCOPE_FULLTEXT = "fulltext"

#: 送进模型的段落字符预算。**``None`` = 不设预算**：全量纳入，不因预算丢章节。
#: 原先写 12000，代价是"预算不足时从低优先级章节丢弃" —— 那是**悄悄丢内容**，
#: 卡片却看不出来（用户口径：输入不设限）。
MAX_CONTEXT_CHARS: int | None = None
#: 单个段落在提示词里的最大长度。**``None`` = 不截断**（截断后的前缀虽与原文逐字一致，
#: 但会丢掉段落后半的内容，同样属于"悄悄丢内容"）。
MAX_BLOCK_CHARS: int | None = None
#: 正文章节优先顺序
SECTION_PRIORITY: tuple[str, ...] = (
    "abstract",
    "method",
    "experiment",
    "conclusion",
    "introduction",
    "related_work",
    "other",
)

#: 禁止出现在卡片里的"指向投稿"表述（附录 D system 模板硬约束）
SUBMISSION_PATTERNS: tuple[str, ...] = (
    "投稿",
    "建议投稿",
    "投递",
    "投稿期刊",
    "投稿会议",
    "submission",
    "submit to",
    "target venue",
    "journal recommendation",
    "camera-ready",
    "影响因子",
    "中科院分区",
)

SYSTEM_PROMPT = (
    "你是科研辅助系统的「论文解析（parse）」模块。你的输出将作为结构化数据被程序消费。\n"
    "必须严格遵守：1) 只输出合法 JSON，不要任何解释性文字；2) 所有结论性字段必须附带 evidence"
    "（evidence_quote 必须是用户给出的 title/abstract/full_text_sections 中**逐字连续**的一段原文，"
    '不要改写、不要拼接、**不要翻译**）；3) 不确定时输出 "未提及" 而不是编造；'
    "禁止生成任何指向投稿的表述（禁止出现投稿/期刊推荐/影响因子等内容）。\n"
    "补充要求：4) 8 个字段全部必填，数组字段至少 1 条；"
    "5) limitations 必须引用论文原文中真实出现的表述（如作者自述的失败场景/局限），"
    '原文没有提到时写 "未提及"；'
    # ⚠️ 2026-09-24 改：原为"语言与论文原文一致（英文论文用英文）"，导致英文论文出英文卡片。
    # 用户口径：**卡片一律中文**，但术语/方法名/模型名/数据集名保留英文标准名称；
    # evidence_quote 仍然必须逐字照抄原文（英文论文就是英文原文）—— 它是证据，不能被翻译。
    "6) **所有字段的值用中文**（含数组项里的 point/conclusion/limitation/step 等）；"
    "专业术语、方法名、模型名、数据集名保留英文标准名称，不做口语化改写；"
    "evidence_quote 例外：必须逐字照抄原文，英文论文保留英文原文，**不得翻译**；"
    '7) 论文未报告的要素统一写 "未提及"，不要留空、不要猜测。'
)

#: 8 字段的 JSON Schema（结构化输出契约 + 本地校验第一道闸门）
#: 说明：``evidence_quote`` 允许占位值 ``"未提及"``（长度下限 1），表示"原文无支撑内容"；
#: 能否作为证据由 ``locator.is_usable_quote``（>=8 字符且非占位值）+ 原文逐字核对共同决定，
#: 因此放宽长度不会让编造的短引用变成证据。
CARD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "research_problem": {"type": "string", "minLength": 3},
        "core_method": {"type": "string", "minLength": 3},
        "key_innovation": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "point": {"type": "string", "minLength": 3},
                    "evidence_quote": {"type": "string", "minLength": 1},
                },
                "required": ["point", "evidence_quote"],
            },
        },
        "technical_route": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "string", "minLength": 3},
                    "description": {"type": "string", "minLength": 3},
                },
                "required": ["step", "description"],
            },
        },
        "experimental_setup": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "datasets": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "baselines": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "metrics": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
            },
            "required": ["datasets", "baselines", "metrics"],
        },
        "main_conclusions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "conclusion": {"type": "string", "minLength": 3},
                    "evidence_quote": {"type": "string", "minLength": 1},
                },
                "required": ["conclusion", "evidence_quote"],
            },
        },
        "limitations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "limitation": {"type": "string", "minLength": 3},
                    "evidence_quote": {"type": "string", "minLength": 1},
                },
                "required": ["limitation", "evidence_quote"],
            },
        },
        "transferable": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "point": {"type": "string", "minLength": 3},
                    "target_problem": {"type": "string", "minLength": 3},
                },
                "required": ["point", "target_problem"],
            },
        },
    },
    "required": [
        "research_problem",
        "core_method",
        "key_innovation",
        "technical_route",
        "experimental_setup",
        "main_conclusions",
        "limitations",
        "transferable",
    ],
}

#: 卡片 8 字段（顺序即界面展示顺序）
CARD_FIELDS: tuple[str, ...] = (
    "research_problem",
    "core_method",
    "key_innovation",
    "technical_route",
    "experimental_setup",
    "main_conclusions",
    "limitations",
    "transferable",
)

#: 需要"非空字符串"的标量字段
SCALAR_FIELDS: tuple[str, ...] = ("research_problem", "core_method")

#: 结论性字段
CONCLUDING_FIELDS: tuple[str, ...] = ("key_innovation", "main_conclusions", "limitations")


class PaperNotFoundError(Exception):
    """``papers`` 表中不存在该 paper_id（API 层转 404）。"""


# --------------------------------------------------------------------------- #
# Pydantic 严格校验
# --------------------------------------------------------------------------- #
class CardValidationError(Exception):
    """卡片结构校验失败（会按 ``LLM_JSON_RETRY`` 触发重试）。"""

    def __init__(self, message: str, *, issues: Sequence[str] = (), raw_head: str = "") -> None:
        super().__init__(message)
        self.issues = list(issues)
        self.raw_head = raw_head

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "issues": self.issues, "raw_head": self.raw_head}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _no_blank(cls, value: Any) -> Any:
        # 空串补成占位值（如实标注"不确定"，而不是留空或编造）
        if isinstance(value, str):
            text = value.strip()
            return text if text else UNKNOWN_PLACEHOLDER
        return value


class InnovationItem(_StrictModel):
    point: str = Field(min_length=1)
    evidence_quote: str = Field(default=UNKNOWN_PLACEHOLDER, min_length=1)


class RouteStep(_StrictModel):
    step: str = Field(min_length=1)
    description: str = Field(min_length=1)


class ConclusionItem(_StrictModel):
    conclusion: str = Field(min_length=1)
    evidence_quote: str = Field(default=UNKNOWN_PLACEHOLDER, min_length=1)


class LimitationItem(_StrictModel):
    limitation: str = Field(min_length=1)
    evidence_quote: str = Field(default=UNKNOWN_PLACEHOLDER, min_length=1)


class TransferableItem(_StrictModel):
    point: str = Field(min_length=1)
    target_problem: str = Field(min_length=1)


class ExperimentalSetup(_StrictModel):
    datasets: list[str] = Field(min_length=1)
    baselines: list[str] = Field(min_length=1)
    metrics: list[str] = Field(min_length=1)


class CardPayload(_StrictModel):
    """8 字段卡片（结构与附录 A.2 ``paper_cards`` 一一对应）。"""

    research_problem: str = Field(min_length=1)
    core_method: str = Field(min_length=1)
    key_innovation: list[InnovationItem] = Field(min_length=1, max_length=6)
    technical_route: list[RouteStep] = Field(min_length=1, max_length=10)
    experimental_setup: ExperimentalSetup
    main_conclusions: list[ConclusionItem] = Field(min_length=1, max_length=8)
    limitations: list[LimitationItem] = Field(min_length=1, max_length=8)
    transferable: list[TransferableItem] = Field(min_length=1, max_length=6)

    def to_card_dict(self) -> dict[str, Any]:
        """转成落库前的 8 字段字典（保留 ``evidence_quote`` 供定位阶段消费）。"""
        return {
            "research_problem": self.research_problem,
            "core_method": self.core_method,
            "key_innovation": [item.model_dump() for item in self.key_innovation],
            "technical_route": [item.model_dump() for item in self.technical_route],
            "experimental_setup": self.experimental_setup.model_dump(),
            "main_conclusions": [item.model_dump() for item in self.main_conclusions],
            "limitations": [item.model_dump() for item in self.limitations],
            "transferable": [item.model_dump() for item in self.transferable],
        }


def validate_card_payload(payload: Any) -> CardPayload:
    """严格校验 8 字段结构；失败抛 :class:`CardValidationError`。"""
    if not isinstance(payload, dict):
        raise CardValidationError(
            f"卡片输出不是 JSON 对象（实际类型 {type(payload).__name__}）",
            issues=["payload_not_object"],
        )
    missing = [name for name in CARD_FIELDS if name not in payload]
    if missing:
        raise CardValidationError(
            "卡片缺少必填字段", issues=[f"missing:{name}" for name in missing]
        )
    try:
        model = CardPayload.model_validate(payload)
    except ValidationError as exc:
        issues = [
            f"{'.'.join(str(part) for part in error['loc'])}:{error['type']}"
            for error in exc.errors()[:20]
        ]
        raise CardValidationError("卡片结构校验失败（Pydantic 严格模式）", issues=issues) from exc
    return model


def unknown_fields(card: dict[str, Any]) -> list[str]:
    """如实统计"模型自述不确定"的字段（前端可据此提示证据不足）。"""
    flagged: list[str] = []
    for name in SCALAR_FIELDS:
        if str(card.get(name) or "").strip().lower() in UNKNOWN_VALUES:
            flagged.append(name)
    setup = card.get("experimental_setup")
    if isinstance(setup, dict):
        for key in ("datasets", "baselines", "metrics"):
            values = [str(v).strip().lower() for v in (setup.get(key) or [])]
            if values and all(v in UNKNOWN_VALUES for v in values):
                flagged.append(f"experimental_setup.{key}")
    return flagged


def _submission_language_hits(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [pattern for pattern in SUBMISSION_PATTERNS if pattern.lower() in lowered]


def guard_submission_language(card: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """剔除任何"指向投稿"的表述（附录 D system 模板硬约束）。

    命中时：同字段还有其它条目则丢弃命中条目；只剩这一条时把文本置为占位值 ``未提及``。
    所有处置留痕在 ``evidence_meta.guardrail``，不做静默修改。
    """
    flags: list[dict[str, Any]] = []
    cleaned = dict(card)

    for name in SCALAR_FIELDS:
        hits = _submission_language_hits(str(cleaned.get(name) or ""))
        if hits:
            flags.append(
                {"field": name, "entry_index": None, "patterns": hits, "action": "set_unknown"}
            )
            cleaned[name] = UNKNOWN_PLACEHOLDER

    for name, key in FIELD_TEXT_KEYS.items():
        entries = cleaned.get(name)
        if not isinstance(entries, list):
            continue
        kept: list[dict[str, Any]] = []
        dropped = 0
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            hits = _submission_language_hits(json.dumps(entry, ensure_ascii=False))
            if not hits:
                kept.append(entry)
                continue
            dropped += 1
            flags.append(
                {
                    "field": name,
                    "entry_index": position,
                    "patterns": hits,
                    "action": "drop" if len(entries) > 1 else "set_unknown",
                }
            )
        if not kept and entries:
            fallback = dict(entries[0]) if isinstance(entries[0], dict) else {}
            fallback[key] = UNKNOWN_PLACEHOLDER
            fallback["evidence_quote"] = UNKNOWN_PLACEHOLDER
            kept = [fallback]
        if kept:
            cleaned[name] = kept
        if dropped:
            logger.warning("字段 %s 命中投稿类表述，已按红线处置 %d 条", name, dropped)

    for name in ("technical_route", "transferable"):
        entries = cleaned.get(name)
        if not isinstance(entries, list):
            continue
        kept = [
            entry
            for entry in entries
            if not isinstance(entry, dict)
            or not _submission_language_hits(json.dumps(entry, ensure_ascii=False))
        ]
        if not kept:
            kept = entries[:1] if entries else []
        cleaned[name] = kept

    setup = cleaned.get("experimental_setup")
    if isinstance(setup, dict):
        for key in ("datasets", "baselines", "metrics"):
            values = setup.get(key)
            if not isinstance(values, list):
                continue
            filtered = [item for item in values if not _submission_language_hits(str(item))]
            setup[key] = filtered or [UNKNOWN_PLACEHOLDER]
    return cleaned, flags


# --------------------------------------------------------------------------- #
# 卡片上下文（论文元数据 + 可用正文章节）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class CardContext:
    paper_id: int
    title: str
    abstract: str | None
    document_version: str | None
    parse_status: str | None
    coverage: float | None
    parser: str | None
    gate: dict[str, Any]
    index: SpanIndex | None = None
    blocks: list[dict[str, Any]] = field(default_factory=list)
    documents_seen: int = 0
    section_chars: dict[str, int] = field(default_factory=dict)
    dropped_sections: list[str] = field(default_factory=list)

    @property
    def available_scope(self) -> str:
        return SCOPE_FULLTEXT if self.gate.get("allowed") else SCOPE_ABSTRACT_ONLY

    def to_audit(self) -> dict[str, Any]:
        return {
            "available_scope": self.available_scope,
            "parse_status": self.parse_status,
            "coverage": self.coverage,
            "parser": self.parser,
            "document_version": self.document_version,
            "documents_seen": self.documents_seen,
            "fulltext_gate": self.gate,
            "context_blocks": len(self.blocks),
            "section_chars": dict(self.section_chars),
            "dropped_sections": list(self.dropped_sections),
        }


def _truncate_block(text: str) -> tuple[str, bool]:
    if MAX_BLOCK_CHARS is None or len(text) <= MAX_BLOCK_CHARS:
        return text, False
    return text[:MAX_BLOCK_CHARS], True


def build_prompt_blocks(
    spans: Sequence[PaperSpanRecord],
    *,
    max_chars: int | None = MAX_CONTEXT_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """按 ``SECTION_PRIORITY`` 挑选正文章节（**默认不设字符预算**）。

    选择策略（对应 WP06-T1「先 abstract + method，必要时追加 experiment」）：

    1. 章节按 ``SECTION_PRIORITY`` 依次纳入（abstract → method → experiment → conclusion
       → introduction → related_work → other），即"必要时追加"的落地形式；
    2. 同一章节内按 ``char_start`` 升序纳入，保持段落连续可读；
    3. ``max_chars`` 为 ``None``（默认）时**全量纳入、不丢任何章节**；
       只有当调用方显式给了预算时，才在超预算时停止纳入该章节剩余段落并把章节名记入
       ``dropped_sections``（如实记录"有内容没进上下文"）；
    4. 单段超长按 ``MAX_BLOCK_CHARS`` 截断（默认 ``None`` = 不截断）。

    返回 ``(blocks, section_chars, dropped_sections)``；``blocks`` 按章节优先级排序。
    """
    by_section: dict[str, list[PaperSpanRecord]] = {}
    for span in spans:
        section = span.section_name or "other"
        if section not in SECTION_PRIORITY:
            continue
        by_section.setdefault(section, []).append(span)
    for rows in by_section.values():
        rows.sort(key=lambda span: (int(span.char_start), span.id or 0))

    blocks: list[dict[str, Any]] = []
    section_chars: dict[str, int] = {}
    dropped: list[str] = []
    char_total = 0
    for section in SECTION_PRIORITY:
        rows = by_section.get(section)
        if not rows:
            continue
        section_used = 0
        section_dropped = False
        for span in rows:
            text, _ = _truncate_block(span.quote_text or "")
            if not text:
                continue
            if max_chars is not None and char_total + len(text) > max_chars:
                section_dropped = True
                continue
            blocks.append(
                {
                    "section": section,
                    "char_start": int(span.char_start),
                    "char_end": int(span.char_end),
                    "text": text,
                }
            )
            char_total += len(text)
            section_used += len(text)
        if section_used:
            section_chars[section] = section_used
        if section_dropped:
            dropped.append(section)
    return blocks, section_chars, dropped


async def collect_card_context(repository: SqlDocumentRepository, paper_id: int) -> CardContext:
    """读取论文元数据 + 可用正文章节，并判定全文闸门。"""
    paper = await repository.get_paper(paper_id)
    if paper is None:
        raise PaperNotFoundError(f"papers 表中不存在 paper_id={paper_id}")

    documents = await repository.list_documents(paper_id)
    summary = summarize_documents(documents)
    document_version = summary.get("document_version")
    parse_status = summary.get("parse_status")
    coverage = summary.get("coverage")
    gate = gate_state(
        parse_status=parse_status,
        coverage=float(coverage) if coverage is not None else None,
        document_version=str(document_version) if document_version else None,
    )

    context = CardContext(
        paper_id=int(paper.id),
        title=paper.title or "",
        abstract=paper.abstract,
        document_version=str(document_version) if document_version else None,
        parse_status=parse_status,
        coverage=float(coverage) if coverage is not None else None,
        parser=summary.get("parser"),
        gate=gate,
        documents_seen=len(documents),
    )

    if not gate["allowed"] or not document_version:
        logger.info(
            "paper %s 走摘要级降级（available_scope=abstract_only, reason=%s）",
            paper_id,
            gate.get("reason"),
        )
        return context

    spans = await repository.list_spans(paper_id, document_version=str(document_version))
    full_text = default_text_cache().get(int(paper_id), str(document_version))
    context.index = SpanIndex(int(paper.id), str(document_version), spans, full_text=full_text)
    blocks, section_chars, dropped = build_prompt_blocks(spans)
    context.blocks = blocks
    context.section_chars = section_chars
    context.dropped_sections = dropped
    logger.info(
        "paper %s 正文章节可用：spans=%d blocks=%d sections=%s 丢弃章节=%s",
        paper_id,
        len(spans),
        len(blocks),
        sorted(section_chars),
        dropped,
    )
    return context


# --------------------------------------------------------------------------- #
# 提示词与 LLM 调用
# --------------------------------------------------------------------------- #
def build_messages(context: CardContext) -> list[dict[str, Any]]:
    """构造 user 消息（title + abstract + 可用正文章节）。"""
    payload: dict[str, Any] = {
        "paper": {
            "paper_id": context.paper_id,
            "title": context.title,
            "abstract": context.abstract,
        },
        "available_scope": context.available_scope,
        "full_text_sections": context.blocks,
        "instruction": (
            "请输出 JSON，且**只输出 JSON**，包含以下 8 个字段：\n"
            "1) research_problem: 字符串，研究问题；\n"
            "2) core_method: 字符串，核心方法；\n"
            '3) key_innovation: 数组，每条 {"point": 创新点, "evidence_quote": 支撑该点的原文片段}；\n'
            '4) technical_route: 数组，每条 {"step": 步骤名, "description": 步骤说明}；\n'
            '5) experimental_setup: 对象 {"datasets": [...], "baselines": [...], "metrics": [...]}；\n'
            '6) main_conclusions: 数组，每条 {"conclusion": 结论, "evidence_quote": 支撑原文片段}；\n'
            '7) limitations: 数组，每条 {"limitation": 局限, "evidence_quote": 作者自述局限的原文片段}；\n'
            '8) transferable: 数组，每条 {"point": 可迁移做法, "target_problem": 可迁移到的问题}。\n'
            "evidence_quote 必须从上面的 title/abstract/full_text_sections.text 中逐字复制一段连续原文"
            '（不要改写、不要跨段拼接、不要翻译）；原文确实没有支撑内容时写 "未提及"。\n'
            '论文未报告的实验要素（数据集/基线/指标）用 "未提及" 占位，禁止猜测。\n'
            "字段的值一律用中文（术语/方法名/模型名/数据集名保留英文标准名称）；"
            "evidence_quote 保持原文语言，英文论文就是英文原文，不得翻译。\n"
            "禁止出现任何与投稿、期刊/会议推荐、影响因子相关的内容。"
        ),
    }
    if not context.blocks:
        payload["scope_note"] = (
            "本论文仅摘要可用（全文未能解析或覆盖率不足）：请只依据 title/abstract 作答，"
            'experimental_setup 等未在摘要中出现的要素写 "未提及"。'
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]


def _corrective_message(issues: Sequence[str]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "上一次输出不符合契约，请严格修正后重新输出完整 JSON（不要任何解释）："
            f"{'; '.join(issues[:10]) or '结构与 required 字段不匹配'}。"
            "注意：8 个字段全部必填，数组字段至少 1 条，字段值用中文（术语保留英文），"
            'evidence_quote 必须是被引用原文里的逐字片段或 "未提及"。'
        ),
    }


async def call_card_llm(
    messages: list[dict[str, Any]],
    *,
    paper_id: int,
    document_version: str | None,
    project_id: int | None,
    json_retry_limit: int | None = None,
) -> tuple[CardPayload, dict[str, Any], dict[str, Any]]:
    """调用 ``llm.chat``（``stage='parse'``/``purpose='card_build'``）并按 ``LLM_JSON_RETRY`` 重试。

    返回 ``(卡片, 调用审计, {'linked_id': ...})``；``linked_id`` 为最后一次成功调用写入
    ``llm_call_logs`` 的行 id，用于 ``paper_cards.llm_call_log_id`` 关联。
    """
    from llm import chat
    from llm.replay import json_retry_limit as _json_retry_limit

    limit = int(json_retry_limit) if json_retry_limit is not None else int(_json_retry_limit())
    max_attempts = max(1, min(limit + 1, 3))
    conversation = list(messages)
    last_issues: list[str] = []
    log_ids: dict[str, Any] = {"before_id": None, "after_id": None, "linked_id": None}

    for attempt in range(1, max_attempts + 1):
        log_ids["before_id"] = await max_call_log_id()
        result = await chat(
            conversation,
            json_schema=CARD_SCHEMA,
            temperature=0.2,
            # 不设输出上限：推理类模型的推理与正文共用这份预算，
            # 限死会让正文被截断（实测 max_tokens=2048 时 content 为空 → 解析失败）
            max_tokens=None,
            project_id=project_id,
            stage="parse",
            purpose="card_build",
            metadata={
                "paper_id": paper_id,
                "document_version": document_version,
                "card_builder": CARD_BUILDER_VERSION,
                "attempt": attempt,
            },
        )
        log_ids["after_id"] = await max_call_log_id()
        log_ids["linked_id"] = log_ids["after_id"]

        try:
            payload = validate_card_payload(result.parsed)
        except CardValidationError as exc:
            last_issues = exc.issues
            logger.warning(
                "卡片结构校验失败（第 %s/%s 次）paper_id=%s issues=%s",
                attempt,
                max_attempts,
                paper_id,
                exc.issues,
            )
            if attempt >= max_attempts:
                raise
            conversation = [*messages, _corrective_message(exc.issues)]
            continue

        audit = {
            "model_ref": result.model_ref,
            "provider": result.provider,
            "prompt_hash": result.prompt_hash,
            "is_replay": bool(result.is_replay),
            "attempts": int(result.attempts),
            "http_calls": int(result.http_calls),
            "cost_usd": result.cost_usd,
            "cost_unknown_reason": result.cost_unknown_reason,
            "resolved_from": result.resolved_from,
            "fallback_from": list(result.fallback_from),
            "validation_attempt": attempt,
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
            },
        }
        return payload, audit, log_ids

    raise CardValidationError("卡片结构校验连续失败", issues=last_issues)


async def max_call_log_id() -> int | None:
    """``llm_call_logs`` 当前最大 id（用于把卡片关联到最后一次成功调用）。"""
    try:
        from sqlalchemy import text

        from llm.db import get_async_engine

        engine = get_async_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM llm_call_logs"))
            row = result.first()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001 - 记账探测失败不应阻断建卡（关联字段如实置 null）
        logger.warning("读取 llm_call_logs 最大 id 失败，卡片将不关联调用日志", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# 落库（异步 / 同步 session 兼容）
# --------------------------------------------------------------------------- #
async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _load_json(value: Any, default: Any) -> Any:
    """JSONB 列在不同驱动下可能返回 str（asyncpg）或已解析对象（psycopg）。"""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


@dataclass(slots=True)
class CardRow:
    """``paper_cards`` 一行的纯数据视图。"""

    paper_id: int
    version: int
    card: dict[str, Any]
    llm_call_log_id: int | None = None
    id: int | None = None
    created_at: Any | None = None
    updated_at: Any | None = None

    @property
    def evidence_meta(self) -> dict[str, Any]:
        setup = self.card.get("experimental_setup")
        if not isinstance(setup, dict):
            return {}
        meta = setup.get("evidence_meta")
        return dict(meta) if isinstance(meta, dict) else {}

    @property
    def available_scope(self) -> str:
        return str(self.evidence_meta.get("available_scope") or SCOPE_ABSTRACT_ONLY)

    @property
    def document_version(self) -> str | None:
        value = self.evidence_meta.get("document_version")
        return str(value) if value else None

    def to_api_dict(self) -> dict[str, Any]:
        meta = self.evidence_meta
        return {
            "paper_id": self.paper_id,
            "version": self.version,
            "card": self.card,
            "llm_call_log_id": self.llm_call_log_id,
            "available_scope": self.available_scope,
            "evidence_scope": meta.get("evidence_scope"),
            "parse_status": meta.get("parse_status"),
            "coverage": meta.get("coverage"),
            "document_version": self.document_version,
            "located_count": meta.get("located_count"),
            "unlocated_count": meta.get("unlocated_count"),
            "by_field": meta.get("by_field"),
            "unknown_fields": meta.get("unknown_fields"),
            "generated_at": meta.get("generated_at"),
            "card_builder_version": meta.get("card_builder_version"),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


class SqlCardRepository(SqlDocumentRepository):
    """在 WP05 的文档仓储之上，增加 ``paper_cards`` 读写与版本管理。"""

    _CARD_COLUMNS = (
        "id",
        "paper_id",
        "version",
        "research_problem",
        "core_method",
        "key_innovation",
        "technical_route",
        "experimental_setup",
        "main_conclusions",
        "limitations",
        "transferable",
        "llm_call_log_id",
        "created_at",
        "updated_at",
    )

    async def _run(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        return await _maybe_await(self.session.execute(statement, params or {}))

    async def next_version(self, paper_id: int) -> int:
        from sqlalchemy import text

        result = await self._run(
            text("SELECT COALESCE(MAX(version), 0) + 1 FROM paper_cards WHERE paper_id = :pid"),
            {"pid": int(paper_id)},
        )
        row = result.first()
        return int(row[0]) if row and row[0] is not None else 1

    async def insert_card(self, row: CardRow) -> CardRow:
        from sqlalchemy import text

        statement = text(
            """
            INSERT INTO paper_cards
                (paper_id, version, research_problem, core_method, key_innovation,
                 technical_route, experimental_setup, main_conclusions, limitations,
                 transferable, llm_call_log_id)
            VALUES
                (:paper_id, :version, :research_problem, :core_method,
                 CAST(:key_innovation AS JSONB), CAST(:technical_route AS JSONB),
                 CAST(:experimental_setup AS JSONB), CAST(:main_conclusions AS JSONB),
                 CAST(:limitations AS JSONB), CAST(:transferable AS JSONB), :llm_call_log_id)
            RETURNING id, created_at, updated_at
            """
        )
        params = {
            "paper_id": int(row.paper_id),
            "version": int(row.version),
            "research_problem": str(row.card["research_problem"]),
            "core_method": str(row.card["core_method"]),
            "key_innovation": _dump(row.card["key_innovation"]),
            "technical_route": _dump(row.card["technical_route"]),
            "experimental_setup": _dump(row.card["experimental_setup"]),
            "main_conclusions": _dump(row.card["main_conclusions"]),
            "limitations": _dump(row.card["limitations"]),
            "transferable": _dump(row.card["transferable"]),
            "llm_call_log_id": row.llm_call_log_id,
        }
        result = await self._run(statement, params)
        record = result.first()
        if record is not None:
            row.id = int(record[0])
            row.created_at = record[1]
            row.updated_at = record[2]
        await self._commit()
        return row

    async def get_card(self, paper_id: int, version: int | None = None) -> CardRow | None:
        from sqlalchemy import text

        columns = ", ".join(self._CARD_COLUMNS)
        if version is None:
            statement = text(
                f"SELECT {columns} FROM paper_cards WHERE paper_id = :pid "
                "ORDER BY version DESC LIMIT 1"
            )
            params: dict[str, Any] = {"pid": int(paper_id)}
        else:
            statement = text(
                f"SELECT {columns} FROM paper_cards WHERE paper_id = :pid AND version = :version LIMIT 1"
            )
            params = {"pid": int(paper_id), "version": int(version)}
        result = await self._run(statement, params)
        record = result.mappings().first()
        return _card_from_row(dict(record)) if record else None

    async def list_card_versions(self, paper_id: int) -> list[dict[str, Any]]:
        from sqlalchemy import text

        result = await self._run(
            text(
                "SELECT version, llm_call_log_id, created_at FROM paper_cards "
                "WHERE paper_id = :pid ORDER BY version DESC"
            ),
            {"pid": int(paper_id)},
        )
        return [
            {
                "version": int(row["version"]),
                "llm_call_log_id": row["llm_call_log_id"],
                "created_at": _iso(row["created_at"]),
            }
            for row in result.mappings().all()
        ]

    async def set_is_parsed(self, paper_id: int, value: bool = True) -> None:
        from sqlalchemy import text

        await self._run(
            text("UPDATE papers SET is_parsed = :value, updated_at = now() WHERE id = :pid"),
            {"value": bool(value), "pid": int(paper_id)},
        )
        await self._commit()

    async def get_llm_call_log(self, log_id: int) -> dict[str, Any] | None:
        """按 id 反查 ``llm_call_logs``（A6：卡片 ↔ 调用日志双向可追溯）。"""
        from sqlalchemy import text

        result = await self._run(
            text(
                "SELECT id, project_id, stage, provider, model, purpose, prompt_tokens, "
                "completion_tokens, cost_usd, duration_ms, success, is_replay, error, created_at "
                "FROM llm_call_logs WHERE id = :id"
            ),
            {"id": int(log_id)},
        )
        record = result.mappings().first()
        if record is None:
            return None
        row = dict(record)
        row["created_at"] = _iso(row["created_at"])
        row["cost_usd"] = float(row["cost_usd"]) if row["cost_usd"] is not None else None
        return row


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _card_from_row(row: dict[str, Any]) -> CardRow:
    card = {
        "research_problem": row["research_problem"],
        "core_method": row["core_method"],
        "key_innovation": _load_json(row["key_innovation"], []),
        "technical_route": _load_json(row["technical_route"], []),
        "experimental_setup": _load_json(row["experimental_setup"], {}),
        "main_conclusions": _load_json(row["main_conclusions"], []),
        "limitations": _load_json(row["limitations"], []),
        "transferable": _load_json(row["transferable"], []),
    }
    return CardRow(
        id=int(row["id"]),
        paper_id=int(row["paper_id"]),
        version=int(row["version"]),
        card=card,
        llm_call_log_id=row["llm_call_log_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --------------------------------------------------------------------------- #
# 建卡主流程
# --------------------------------------------------------------------------- #
async def build_card(
    paper_id: int,
    *,
    force: bool = False,
    session: Any | None = None,
    project_id: int | None = None,
) -> dict[str, Any]:
    """生成（或复用）单篇论文的 8 字段卡片并落库。

    :param force: ``True`` 时无视已有版本，直接生成 ``version = MAX+1`` 的新卡片
        （旧版本保留，符合 ``UNIQUE(paper_id, version)``）
    :param session: 注入的 SQLAlchemy 会话（``AsyncSession`` 或同步 ``Session``）；
        不传则自建 ``AsyncSessionLocal`` 会话并在结束时关闭
    :raises PaperNotFoundError: ``papers`` 表中不存在该 paper_id
    :raises CardValidationError: 模型输出连续不符合契约
    """
    if session is not None:
        return await _build_card_impl(paper_id, force=force, session=session, project_id=project_id)

    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise RuntimeError("异步数据库会话工厂不可用（DATABASE_URL / asyncpg 未就绪）")
    async with AsyncSessionLocal() as owned:
        return await _build_card_impl(paper_id, force=force, session=owned, project_id=project_id)


async def _build_card_impl(
    paper_id: int,
    *,
    force: bool,
    session: Any,
    project_id: int | None,
) -> dict[str, Any]:
    from llm.replay import json_retry_limit as _json_retry_limit

    repository = SqlCardRepository(session)
    context = await collect_card_context(repository, paper_id)

    latest = await repository.get_card(paper_id)
    if latest is not None and not force and latest.document_version == context.document_version:
        logger.info(
            "paper %s 已有 version=%s 且 document_version 未变，复用（force=False）",
            paper_id,
            latest.version,
        )
        return {
            "paper_id": int(paper_id),
            "status": "reused",
            "version": latest.version,
            "document_version": latest.document_version,
            "available_scope": latest.available_scope,
            "llm_call_log_id": latest.llm_call_log_id,
            "card": latest.card,
            "evidence_meta": latest.evidence_meta,
            "reused": True,
        }

    messages = build_messages(context)
    payload, llm_audit, log_ids = await call_card_llm(
        messages,
        paper_id=int(paper_id),
        document_version=context.document_version,
        project_id=project_id,
        json_retry_limit=_json_retry_limit(),
    )
    card = payload.to_card_dict()
    card, guardrail_flags = guard_submission_language(card)

    if context.gate.get("allowed") and context.index is not None and len(context.index) > 0:
        located_card, locate_report = locate_card(card, index=context.index, gate=context.gate)
    else:
        locate_report = LocateReport(gate=dict(context.gate))
        located_card = dict(card)
        for name in CONCLUDING_FIELDS:
            entries = [item for item in (card.get(name) or []) if isinstance(item, dict)]
            located_card[name] = placeholder_entries(
                entries,
                text_key=FIELD_TEXT_KEYS.get(name, "point"),
                note="abstract_only_scope" if not context.gate.get("allowed") else "no_paper_spans",
            )
        locate_report.rejects.append(
            {
                "field": "*",
                "entry_index": None,
                "reason": (
                    "fulltext_gate_not_satisfied"
                    if not context.gate.get("allowed")
                    else "no_paper_spans"
                ),
                "detail": context.gate.get("reason"),
            }
        )
        locate_report.unlocated_count = sum(
            len(located_card.get(name) or []) for name in CONCLUDING_FIELDS
        )
        locate_report.by_field = {
            name: {
                "total": len(located_card.get(name) or []),
                "located": 0,
                "unlocated": len(located_card.get(name) or []),
            }
            for name in CONCLUDING_FIELDS
        }

    setup = dict(located_card.get("experimental_setup") or {})

    # 全文总结速览：**随卡片一起生成**（用户口径），失败不拖累卡片 —— generate_summary 从不抛异常，
    # 失败只落 status='failed'，页面如实显示「生成失败」。
    summary = await generate_summary(
        paper_id=int(paper_id),
        document_version=context.document_version,
        title=context.title,
        abstract=context.abstract,
        blocks=context.blocks,
        project_id=project_id,
    )

    meta = {
        "available_scope": context.available_scope,
        "evidence_scope": context.available_scope,
        "parse_status": context.parse_status,
        "coverage": context.coverage,
        "document_version": context.document_version,
        "parser": context.parser,
        "fulltext_gate": context.gate,
        "fulltext_gate_threshold": FULLTEXT_GATE_COVERAGE,
        "located_count": locate_report.located_count,
        "unlocated_count": locate_report.unlocated_count,
        "by_field": locate_report.by_field,
        "rejects": locate_report.rejects[:40],
        "unknown_fields": unknown_fields(located_card),
        "guardrail": {"submission_language_flags": guardrail_flags[:20]},
        "llm": llm_audit,
        "llm_call_log_id": log_ids.get("linked_id"),
        "context": context.to_audit(),
        "generated_at": _utc_now(),
        "card_builder_version": CARD_BUILDER_VERSION,
        "summary": summary,
        "locate_rule": (
            "quote_text 为落库 span 的真实切片，quote_sha256 由服务端重新计算；"
            "定位不到一律 evidence_span=null，禁止编造引用"
        ),
    }
    setup["evidence_meta"] = meta
    located_card["experimental_setup"] = setup

    row, created_version = await _insert_with_version_retry(
        repository, paper_id, located_card, log_ids.get("linked_id")
    )
    await repository.set_is_parsed(paper_id, True)

    logger.info(
        "card_built paper_id=%s version=%s scope=%s located=%s unlocated=%s llm_call_log_id=%s",
        paper_id,
        created_version,
        context.available_scope,
        locate_report.located_count,
        locate_report.unlocated_count,
        log_ids.get("linked_id"),
    )
    return {
        "paper_id": int(paper_id),
        "status": "created",
        "version": created_version,
        "document_version": context.document_version,
        "available_scope": context.available_scope,
        "llm_call_log_id": log_ids.get("linked_id"),
        "card_record_id": row.id,
        "card": located_card,
        "evidence_meta": meta,
        "reused": False,
    }


async def _insert_with_version_retry(
    repository: SqlCardRepository,
    paper_id: int,
    card: dict[str, Any],
    llm_call_log_id: int | None,
) -> tuple[CardRow, int]:
    """``version = MAX+1`` 写入；并发撞唯一键时重算一次（绝不覆盖他人版本）。"""
    last_error: Exception | None = None
    for _attempt in range(2):
        version = await repository.next_version(paper_id)
        row = CardRow(
            paper_id=int(paper_id),
            version=version,
            card=card,
            llm_call_log_id=llm_call_log_id,
        )
        try:
            saved = await repository.insert_card(row)
            return saved, version
        except Exception as exc:  # noqa: BLE001 - 唯一键冲突时重算版本
            last_error = exc
            logger.warning(
                "写入 paper_cards 失败（paper_id=%s version=%s）：%s", paper_id, version, exc
            )
            await _rollback(repository.session)
    raise RuntimeError(f"写入 paper_cards 失败：{last_error}")


async def _rollback(session: Any) -> None:
    try:
        await _maybe_await(session.rollback())
    except Exception:  # noqa: BLE001 - 回滚失败只记日志
        logger.debug("回滚会话失败", exc_info=True)


async def get_card(
    paper_id: int, version: int | None = None, *, session: Any | None = None
) -> CardRow | None:
    """读取卡片（``version=None`` 时返回最新版本）。"""
    if session is not None:
        return await SqlCardRepository(session).get_card(paper_id, version)
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover
        raise RuntimeError("异步数据库会话工厂不可用")
    async with AsyncSessionLocal() as owned:
        return await SqlCardRepository(owned).get_card(paper_id, version)


async def list_card_versions(paper_id: int, *, session: Any | None = None) -> list[dict[str, Any]]:
    """版本列表（倒序）。"""
    if session is not None:
        return await SqlCardRepository(session).list_card_versions(paper_id)
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover
        raise RuntimeError("异步数据库会话工厂不可用")
    async with AsyncSessionLocal() as owned:
        return await SqlCardRepository(owned).list_card_versions(paper_id)


async def get_llm_call_log(log_id: int, *, session: Any | None = None) -> dict[str, Any] | None:
    """按 id 反查 LLM 调用详情（A6 追溯用）。"""
    if session is not None:
        return await SqlCardRepository(session).get_llm_call_log(log_id)
    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover
        raise RuntimeError("异步数据库会话工厂不可用")
    async with AsyncSessionLocal() as owned:
        return await SqlCardRepository(owned).get_llm_call_log(log_id)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# 长任务（POST /papers/{id}/card）：返回 task_id，结果查 card / card-jobs
# --------------------------------------------------------------------------- #
CARD_JOB_NAME = "card_build"
_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
_MAX_TASKS = 200


def _register_task(task_id: str, payload: dict[str, Any]) -> None:
    with _TASKS_LOCK:
        _TASKS[task_id] = payload
        if len(_TASKS) > _MAX_TASKS:
            for key in sorted(_TASKS, key=lambda item: str(_TASKS[item].get("started_at")))[:50]:
                _TASKS.pop(key, None)


def _update_task(task_id: str, **changes: Any) -> None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        if task is not None:
            task.update(changes)


def get_card_task(task_id: str) -> dict[str, Any] | None:
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task is not None else None


def list_card_tasks(limit: int = 20) -> list[dict[str, Any]]:
    with _TASKS_LOCK:
        items = sorted(_TASKS.values(), key=lambda item: str(item.get("started_at")), reverse=True)
        return [dict(item) for item in items[:limit]]


def publish_card_progress(paper_id: int, percent: int, message: str) -> None:
    """广播 ``stage_progress``（SSE 传输归 WP09 的 /stream 端点）。"""
    try:
        from services.pipeline.sse import publish

        publish(
            "stage_progress",
            {"stage": "parse", "percent": int(percent), "message": message},
            project_id=None,
        )
    except Exception:  # noqa: BLE001 - 事件通道不可用不影响建卡
        logger.debug("SSE 事件通道不可用，跳过 stage_progress 广播", exc_info=True)


def _task_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    meta = result.get("evidence_meta") or {}
    return {
        "paper_id": result.get("paper_id"),
        "version": result.get("version"),
        "status": result.get("status"),
        "reused": result.get("reused"),
        "available_scope": result.get("available_scope"),
        "llm_call_log_id": result.get("llm_call_log_id"),
        "located_count": meta.get("located_count"),
        "unlocated_count": meta.get("unlocated_count"),
        "card_url": f"/api/v1/papers/{result.get('paper_id')}/card?version={result.get('version')}",
    }


def run_card_build_sync(
    paper_id: int, *, force: bool = False, project_id: int | None = None
) -> dict[str, Any]:
    """同步上下文（CLI / 后台线程）里的建卡入口：自建事件循环。"""
    publish_card_progress(paper_id, 5, "开始生成解析卡片")
    result = asyncio.run(build_card(paper_id, force=force, project_id=project_id))
    publish_card_progress(paper_id, 100, "卡片生成完成")
    return result


def submit_card_build(
    *,
    paper_id: int,
    force: bool = False,
    project_id: int | None = None,
) -> dict[str, Any]:
    """提交建卡长任务，立即返回 ``task_id``。

    在事件循环内（FastAPI 端点）用 ``asyncio.create_task`` 就地调度，
    避免后台线程与主循环争用同一个异步连接池；
    在无事件循环的同步上下文（CLI）里退化为后台线程 + 自建循环。
    """
    task_id = uuid.uuid4().hex[:12]
    _register_task(
        task_id,
        {
            "task_id": task_id,
            "job": CARD_JOB_NAME,
            "status": "accepted",
            "paper_id": int(paper_id),
            "force": bool(force),
            "started_at": _utc_now(),
            "params": {"paper_id": int(paper_id), "force": bool(force), "project_id": project_id},
        },
    )

    async def _worker() -> None:
        _update_task(task_id, status="running")
        publish_card_progress(paper_id, 20, "读取论文与全文片段")
        try:
            result = await build_card(paper_id, force=bool(force), project_id=project_id)
            _update_task(
                task_id,
                status="done",
                finished_at=_utc_now(),
                result=_task_result_payload(result),
            )
            publish_card_progress(paper_id, 100, "卡片生成完成")
        except Exception as exc:  # noqa: BLE001 - 后台失败必须能被查询到
            logger.exception("card_build_failed task_id=%s paper_id=%s", task_id, paper_id)
            _update_task(
                task_id,
                status="failed",
                finished_at=_utc_now(),
                error={"type": type(exc).__name__, "message": str(exc)[:600]},
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(_worker(), name=f"card-build-{task_id}")
        _update_task(task_id, executor="asyncio_task")
    else:

        def _thread_worker() -> None:
            _update_task(task_id, status="running")
            try:
                result = run_card_build_sync(
                    int(paper_id), force=bool(force), project_id=project_id
                )
                _update_task(
                    task_id,
                    status="done",
                    finished_at=_utc_now(),
                    result=_task_result_payload(result),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("card_build_failed task_id=%s paper_id=%s", task_id, paper_id)
                _update_task(
                    task_id,
                    status="failed",
                    finished_at=_utc_now(),
                    error={"type": type(exc).__name__, "message": str(exc)[:600]},
                )

        thread = threading.Thread(target=_thread_worker, name=f"card-build-{task_id}", daemon=True)
        thread.start()
        _update_task(task_id, executor="thread")

    return get_card_task(task_id) or {"task_id": task_id, "status": "accepted"}


def reset_tasks() -> None:
    """清空任务登记（测试用）。"""
    with _TASKS_LOCK:
        _TASKS.clear()


__all__ = [
    "CARD_BUILDER_VERSION",
    "CARD_FIELDS",
    "CARD_JOB_NAME",
    "CARD_SCHEMA",
    "CONCLUDING_FIELDS",
    "CardContext",
    "CardPayload",
    "CardRow",
    "CardValidationError",
    "ExperimentalSetup",
    "PaperNotFoundError",
    "SCOPE_ABSTRACT_ONLY",
    "SCOPE_FULLTEXT",
    "SCALAR_FIELDS",
    "SUBMISSION_PATTERNS",
    "SYSTEM_PROMPT",
    "SqlCardRepository",
    "build_card",
    "build_messages",
    "build_prompt_blocks",
    "call_card_llm",
    "collect_card_context",
    "get_card",
    "get_card_task",
    "get_llm_call_log",
    "guard_submission_language",
    "list_card_tasks",
    "list_card_versions",
    "max_call_log_id",
    "publish_card_progress",
    "reset_tasks",
    "run_card_build_sync",
    "submit_card_build",
    "unknown_fields",
    "validate_card_payload",
]
