# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""写作服务（WP14-T1/T2，附录 D.5）。

职责
----
1. **证据池**：把项目可用的证据（``paper_span`` / ``card_field`` / ``experiment_run`` /
   ``experiment_passport`` / ``decision``）组装成带**稳定引用键**的池子。池子里的每一项都能
   直接转成 WP13 ``binder`` 的候选结构，因此「草稿引用了什么」与「绑定进 ``evidences``
   的是什么」永远同源。
2. **大纲**：产出 ``outline[]``（D6 决策点的动作域 ``{outline[], section_focus{}}``）。
3. **分节撰写**：按 section 生成 Claim（事实性句子），每条 Claim 携带 ``evidence_refs``。
   **引用键只能来自池子**；模型自造的 ref 会被丢弃并计入 ``dropped_refs``。
4. **渲染**：把 Claim 渲染成 Markdown，其中 ``[n]`` 编号**由服务端分配**（按首次出现顺序），
   文末给出「引用索引」把 ``[n]`` 映射回证据键 —— 因此引用编号 100% 可在证据池解析。

合规
----
- 草稿顶部固定 ``DISCLAIMER``（AI 辅助声明）。
- ``FORBIDDEN_PUBLISH_PHRASES`` 为指向外部出版渠道的表述词表：LLM prompt 与生成文本都
  必须 0 命中（命中即替换并留痕，绝不静默放过）。
- 无 key 时可走 ``stub_outline`` / ``stub_sections`` 本地桩跑通；桩产出一律标注
  ``is_stub=True``，且只**转述**证据池条目原文，不引入任何新事实。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sciloop.wp14.drafter")

WP_ID = "WP14"
PROMPT_VERSION = "writing-v1"

#: 产出物顶部与导出文件必须包含的声明（contracts.ui_mandatory_elements）
DISCLAIMER = "本内容由 AI 辅助生成，需研究者自行核验"

#: 指向外部出版渠道的表述（合规红线词表；草稿与 prompt 均须 0 命中）
FORBIDDEN_PUBLISH_PHRASES: tuple[str, ...] = (
    "投稿",
    "投稿信",
    "期刊投递",
    "期刊投稿",
    "投递至期刊",
    "提交至期刊",
    "见刊",
    "录用",
    "发表",
    "camera-ready",
    "cover letter",
    "submit to",
    "submission to",
    "publish in",
)

#: 服务端渲染的引用标记：``[1]`` ``[12]``
CITATION_RE = re.compile(r"\[(\d{1,3})\]")
#: 引用索引行：``[3] paper_span:240 | ...``
REF_LINE_RE = re.compile(r"^\[(\d{1,3})\]\s+([A-Za-z_]+:[^\s|]+)\s*\|(.*)$", re.MULTILINE)

_SENTENCE_END = "。！？.!?"

#: 证据类型 → 引用键前缀（与 binder 的 evidence_type 对齐）
EVIDENCE_TYPE_PREFIX = {
    "paper_span": "paper_span",
    "card_field": "card_field",
    "experiment_run": "experiment_run",
    "experiment_passport": "experiment_passport",
    "decision": "decision",
}

#: 大纲与分节的 JSON Schema（附录 D.5 / D.6 的结构化输出约束）
OUTLINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outline"],
    "properties": {
        "outline": {
            "type": "array",
            "minItems": 3,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["heading", "purpose"],
                "properties": {
                    "heading": {"type": "string", "minLength": 2, "maxLength": 60},
                    "purpose": {"type": "string", "minLength": 4, "maxLength": 200},
                    "focus": {"type": "string", "maxLength": 200},
                },
            },
        },
        "rationale": {"type": "string", "maxLength": 500},
    },
}

SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["text", "evidence_refs"],
                "properties": {
                    "text": {"type": "string", "minLength": 8, "maxLength": 400},
                    "evidence_refs": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "string"},
                    },
                    "grounded": {"type": "boolean"},
                },
            },
        }
    },
}


# --------------------------------------------------------------------------------------
# 证据池
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class EvidenceEntry:
    """证据池中一条可被引用的证据。"""

    ref: str
    evidence_type: str
    label: str
    support_scope: str = "fulltext"
    evidence_id: int | None = None
    paper_id: int | None = None
    paper_span_id: int | None = None
    document_version: str | None = None
    section_name: str | None = None
    card_field: str | None = None
    experiment_run_id: int | None = None
    experiment_passport_id: int | None = None
    decision_log_id: int | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    quote_text: str | None = None
    summary: str | None = None
    source: str = "unknown"

    def candidate(self) -> dict[str, Any]:
        """转成 WP13 ``binder`` 的候选结构（字段名与 ``CANDIDATE_FIELDS`` 严格一致）。"""
        payload: dict[str, Any] = {
            "evidence_type": self.evidence_type,
            "paper_id": self.paper_id,
            "paper_span_id": self.paper_span_id,
            "card_field": self.card_field,
            "experiment_run_id": self.experiment_run_id,
            "experiment_passport_id": self.experiment_passport_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "decision_log_id": self.decision_log_id,
            "quote_text": self.quote_text,
        }
        if self.document_version:
            payload["document_version"] = self.document_version
        return payload

    def prompt_item(self, limit: int = 180) -> dict[str, Any]:
        """给模型看的池子条目（不含数据库内部 id 之外的隐式信息）。"""
        note = self.summary or self.quote_text or ""
        note = " ".join(str(note).split())
        return {
            "ref": self.ref,
            "type": self.evidence_type,
            "label": self.label,
            "scope": self.support_scope,
            "note": note[:limit],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "evidence_type": self.evidence_type,
            "evidence_id": self.evidence_id,
            "label": self.label,
            "support_scope": self.support_scope,
            "paper_id": self.paper_id,
            "paper_span_id": self.paper_span_id,
            "document_version": self.document_version,
            "section_name": self.section_name,
            "card_field": self.card_field,
            "experiment_run_id": self.experiment_run_id,
            "experiment_passport_id": self.experiment_passport_id,
            "decision_log_id": self.decision_log_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "quote_preview": (self.quote_text or "")[:120] or None,
            "summary": self.summary,
            "source": self.source,
        }


class EvidencePool:
    """按引用键索引的证据池（服务端唯一的引用来源）。"""

    def __init__(self, entries: Iterable[EvidenceEntry] = ()) -> None:
        self._entries: dict[str, EvidenceEntry] = {}
        for entry in entries:
            self._entries.setdefault(entry.ref, entry)
        self.scope_note: str | None = None

    # -- 查询 ---------------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> dict[str, EvidenceEntry]:
        return dict(self._entries)

    def keys(self) -> list[str]:
        return list(self._entries)

    def values(self) -> list[EvidenceEntry]:
        return list(self._entries.values())

    def resolve(self, ref: str | None) -> EvidenceEntry | None:
        if not ref:
            return None
        return self._entries.get(str(ref).strip())

    def has(self, ref: str | None) -> bool:
        return self.resolve(ref) is not None

    def by_type(self, evidence_type: str) -> list[EvidenceEntry]:
        return [entry for entry in self._entries.values() if entry.evidence_type == evidence_type]

    def first_of(self, *evidence_types: str) -> EvidenceEntry | None:
        for entry in self._entries.values():
            if entry.evidence_type in evidence_types:
                return entry
        return None

    # -- 输出 ---------------------------------------------------------------- #
    def prompt_items(self, limit: int = 48) -> list[dict[str, Any]]:
        items = [entry.prompt_item() for entry in self._entries.values()]
        return items[:limit]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        scopes: dict[str, int] = {}
        for entry in self._entries.values():
            counts[entry.evidence_type] = counts.get(entry.evidence_type, 0) + 1
            scopes[entry.support_scope] = scopes.get(entry.support_scope, 0) + 1
        return {
            "total": len(self._entries),
            "by_type": counts,
            "by_scope": scopes,
            "scope_note": self.scope_note,
        }


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _squash(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text_value = " ".join(str(value).split())
    return text_value[:limit] or None


async def _rows(session: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    """执行只读查询并返回映射行；任何异常由调用方决定是否降级。"""
    from sqlalchemy import text as sql_text

    result = await session.execute(sql_text(sql), dict(params or {}))
    try:
        return list(result.mappings().all())
    except AttributeError:  # pragma: no cover - 兼容 - 非 SQLAlchemy 结果
        return list(result)


async def _safe_rows(
    session: Any, sql: str, params: Mapping[str, Any] | None = None, *, tag: str
) -> list[Any]:
    """容错查询：表/列缺失（并行工作包未就绪）时返回空并记日志，不阻塞写作环节。

    失败后**必须回滚**：PostgreSQL 在语句报错后会把事务置为 aborted，若不回滚，
    同会话后续查询会全部失败（把一次列名不匹配放大成整篇草稿失败）。
    """
    try:
        return await _rows(session, sql, params)
    except Exception as exc:  # noqa: BLE001 - 并行工作包未就绪属预期情况
        logger.warning("证据池查询降级 tag=%s error=%s: %s", tag, type(exc).__name__, exc)
        import contextlib

        with contextlib.suppress(Exception):
            value = session.rollback()
            if hasattr(value, "__await__"):
                await value
        return []


async def _project_paper_ids(session: Any, project_id: int | None) -> list[int]:
    """项目相关的论文 id（来自 idea 级证据；无则空）。"""
    if project_id is None:
        return []
    rows = await _safe_rows(
        session,
        """
        SELECT DISTINCT e.paper_id AS paper_id
        FROM evidences e
        JOIN ideas i ON i.id = e.owner_id AND e.owner_type = 'idea'
        WHERE i.project_id = :pid AND e.paper_id IS NOT NULL
        ORDER BY e.paper_id
        LIMIT 40
        """,
        {"pid": int(project_id)},
        tag="project_paper_ids",
    )
    return [int(row["paper_id"]) for row in rows if row["paper_id"] is not None]


def _placeholders(prefix: str, values: Sequence[int]) -> tuple[str, dict[str, Any]]:
    names = [f"{prefix}{index}" for index in range(len(values))]
    params = {name: int(value) for name, value in zip(names, values, strict=False)}
    return ", ".join(f":{name}" for name in names), params


async def build_evidence_pool(
    session: Any,
    project_id: int | None,
    *,
    span_limit: int = 24,
    card_limit: int = 12,
    metric_limit: int = 16,
    passport_limit: int = 6,
    decision_limit: int = 8,
) -> EvidencePool:
    """组装项目证据池（真实数据；取不到就少，**绝不编造**）。"""
    entries: list[EvidenceEntry] = []

    # 1) 已是 canonical 证据行（WP13 绑定的 idea 级证据）：优先复用其 evidence_id
    idea_rows = (
        await _safe_rows(
            session,
            """
            SELECT e.id, e.evidence_type, e.paper_id, e.paper_span_id, e.card_field,
                   e.experiment_run_id, e.experiment_passport_id, e.decision_log_id,
                   e.metric_name, e.metric_value, e.quote_text
            FROM evidences e
            WHERE e.owner_type = 'idea'
              AND e.owner_id IN (SELECT id FROM ideas WHERE project_id = :pid)
            ORDER BY e.id
            LIMIT 200
            """,
            {"pid": int(project_id)} if project_id is not None else None,
            tag="idea_evidences",
        )
        if project_id is not None
        else []
    )
    for row in idea_rows:
        entry = _entry_from_evidence_row(row, source="evidences.idea")
        if entry is not None:
            entries.append(entry)

    # 2) 原文片段（fulltext 门禁：parse_status='ok' 且 coverage>=0.60）
    paper_ids = await _project_paper_ids(session, project_id)
    span_scope = "project"
    span_filter = ""
    span_params: dict[str, Any] = {"limit": int(span_limit)}
    if paper_ids:
        clause, extra = _placeholders("pid", paper_ids)
        span_filter = f" AND s.paper_id IN ({clause})"
        span_params.update(extra)
    span_rows = await _safe_rows(
        session,
        f"""
        SELECT s.id AS span_id, s.paper_id, s.section_name, s.document_version,
               s.char_start, s.char_end, s.quote_text,
               d.coverage, d.parse_status, p.title AS paper_title, p.external_id AS external_id,
               p.source AS source
        FROM paper_spans s
        JOIN paper_documents d
          ON d.paper_id = s.paper_id AND d.document_version = s.document_version
        LEFT JOIN papers p ON p.id = s.paper_id
        WHERE d.parse_status = 'ok' AND d.coverage >= 0.60{span_filter}
        ORDER BY s.paper_id, s.id
        LIMIT :limit
        """,
        span_params,
        tag="paper_spans",
    )
    if not span_rows:
        span_scope = "global_fallback"
        span_rows = await _safe_rows(
            session,
            """
            SELECT s.id AS span_id, s.paper_id, s.section_name, s.document_version,
                   s.char_start, s.char_end, s.quote_text,
                   d.coverage, d.parse_status, p.title AS paper_title, p.external_id AS external_id,
                   p.source AS source
            FROM paper_spans s
            JOIN paper_documents d
              ON d.paper_id = s.paper_id AND d.document_version = s.document_version
            LEFT JOIN papers p ON p.id = s.paper_id
            WHERE d.parse_status = 'ok' AND d.coverage >= 0.60
            ORDER BY s.paper_id, s.id
            LIMIT :limit
            """,
            {"limit": int(span_limit)},
            tag="paper_spans_global",
        )
    for row in span_rows:
        paper_id = row["paper_id"]
        title = _squash(row.get("paper_title"), 60)
        external_id = _text(row.get("external_id"))
        source = _text(row.get("source")) or "src"
        if not title and external_id:
            title = f"{source}:{external_id}"
        section = _text(row.get("section_name")) or "unknown"
        label = f"论文 {paper_id}{'（' + title + '）' if title else ''} · {section}"
        entries.append(
            EvidenceEntry(
                ref=f"paper_span:{int(row['span_id'])}",
                evidence_type="paper_span",
                label=label,
                support_scope="fulltext",
                paper_id=int(paper_id) if paper_id is not None else None,
                paper_span_id=int(row["span_id"]),
                document_version=_text(row.get("document_version")),
                section_name=section,
                quote_text=str(row["quote_text"]) if row.get("quote_text") is not None else None,
                summary=f"{label}：{_squash(row.get('quote_text'), 160) or ''}",
                source="paper_spans",
            )
        )

    # 3) 卡片字段：只复用已绑定到 evidences 的行（见步骤 1），不猜测 paper_cards 的 JSON 结构
    _ = card_limit  # 保留参数以固定调用口径（卡片字段上限由池去重与去噪自然保证）

    # 4) 实验指标（WP11 产物；表/列缺失时跳过）
    metric_rows = await _safe_rows(
        session,
        """
        SELECT m.id AS metric_id, m.experiment_run_id, m.metric_name, m.metric_value,
               m.metric_unit, e.template_id, e.id AS experiment_id
        FROM experiment_metrics m
        JOIN experiment_runs r ON r.id = m.experiment_run_id
        JOIN experiments e ON e.id = r.experiment_id
        LEFT JOIN stage_outputs so ON so.id = e.stage_output_id
        LEFT JOIN pipeline_runs pr ON pr.id = so.pipeline_run_id
        WHERE (CAST(:pid AS BIGINT) IS NULL OR pr.project_id = CAST(:pid AS BIGINT))
        ORDER BY m.id
        LIMIT :limit
        """,
        {"pid": int(project_id) if project_id is not None else None, "limit": int(metric_limit)},
        tag="experiment_metrics",
    )
    for row in metric_rows:
        run_id = row.get("experiment_run_id")
        name = _text(row.get("metric_name")) or "metric"
        value = _num(row.get("metric_value"))
        unit = _text(row.get("metric_unit")) or ""
        template = _text(row.get("template_id")) or "unknown_template"
        if run_id is None:
            continue
        entries.append(
            EvidenceEntry(
                ref=f"experiment_metric:{int(row['metric_id'])}",
                evidence_type="experiment_run",
                label=f"实验运行 #{int(run_id)} 指标 {name}",
                support_scope="experiment",
                experiment_run_id=int(run_id),
                metric_name=name,
                metric_value=value,
                summary=f"{template} 运行 #{int(run_id)}：{name}={value}{unit}（真实落库指标）",
                source="experiment_metrics",
            )
        )

    # 5) Passport（可复现性证据）
    passport_rows = await _safe_rows(
        session,
        """
        SELECT p.id, p.experiment_run_id, p.status, p.is_replay, p.template_id,
               p.dataset_name, p.dataset_version, p.model_id, p.code_commit_sha
        FROM experiment_passports p
        JOIN experiment_runs r ON r.id = p.experiment_run_id
        JOIN experiments e ON e.id = r.experiment_id
        LEFT JOIN stage_outputs so ON so.id = e.stage_output_id
        LEFT JOIN pipeline_runs pr ON pr.id = so.pipeline_run_id
        WHERE (CAST(:pid AS BIGINT) IS NULL OR pr.project_id = CAST(:pid AS BIGINT))
        ORDER BY p.id
        LIMIT :limit
        """,
        {"pid": int(project_id) if project_id is not None else None, "limit": int(passport_limit)},
        tag="experiment_passports",
    )
    for row in passport_rows:
        status = _text(row.get("status")) or "incomplete"
        replay = "回放" if row.get("is_replay") else "实时"
        entries.append(
            EvidenceEntry(
                ref=f"experiment_passport:{int(row['id'])}",
                evidence_type="experiment_passport",
                label=f"Passport #{int(row['id'])}（{_text(row.get('dataset_name')) or 'unknown'}）",
                support_scope="experiment",
                experiment_run_id=int(row["experiment_run_id"])
                if row.get("experiment_run_id") is not None
                else None,
                experiment_passport_id=int(row["id"]),
                summary=(
                    f"Passport #{int(row['id'])} status={status} 类型={replay} "
                    f"模板={_text(row.get('template_id')) or 'unknown'} "
                    f"模型={_text(row.get('model_id')) or 'unknown'}"
                ),
                source="experiment_passports",
            )
        )

    # 6) 决策留痕（方法选择的审计依据）
    decision_rows = await _safe_rows(
        session,
        """
        SELECT id, decision_point, chosen, rationale, stage
        FROM decision_logs
        WHERE (CAST(:pid AS BIGINT) IS NULL OR project_id = CAST(:pid AS BIGINT))
        ORDER BY id DESC
        LIMIT :limit
        """,
        {"pid": int(project_id) if project_id is not None else None, "limit": int(decision_limit)},
        tag="decision_logs",
    )
    for row in decision_rows:
        point = _text(row.get("decision_point")) or "D?"
        chosen = _text(row.get("chosen")) or "unknown"
        entries.append(
            EvidenceEntry(
                ref=f"decision:{int(row['id'])}",
                evidence_type="decision",
                label=f"决策日志 #{int(row['id'])}（{point}）",
                support_scope="decision",
                decision_log_id=int(row["id"]),
                summary=f"{point} 决策：{chosen}；{_squash(row.get('rationale'), 120) or ''}",
                source="decision_logs",
            )
        )

    pool = EvidencePool(entries)
    pool.scope_note = (
        f"span_scope={span_scope}；paper_ids={paper_ids or 'none'}；"
        f"说明：仅收录真实落库证据（原文片段/指标/Passport/决策），未取到即为空，不编造"
    )
    logger.info(
        "build_evidence_pool project_id=%s pool=%d summary=%s",
        project_id,
        len(pool),
        pool.summary(),
    )
    return pool


def _entry_from_evidence_row(row: Mapping[str, Any], *, source: str) -> EvidenceEntry | None:
    """``evidences`` 行 → 池条目（引用键按原生对象 id，便于与后续查询去重）。"""
    evidence_type = _text(row.get("evidence_type"))
    if evidence_type == "paper_span":
        span_id = row.get("paper_span_id")
        if span_id is None:
            return None
        paper_id = row.get("paper_id")
        quote = row.get("quote_text")
        return EvidenceEntry(
            ref=f"paper_span:{int(span_id)}",
            evidence_type="paper_span",
            label=f"论文 {paper_id} · 原文片段 #{int(span_id)}",
            support_scope="fulltext",
            evidence_id=int(row["id"]) if row.get("id") is not None else None,
            paper_id=int(paper_id) if paper_id is not None else None,
            paper_span_id=int(span_id),
            quote_text=str(quote) if quote is not None else None,
            summary=_squash(quote, 160),
            source=source,
        )
    if evidence_type == "card_field":
        paper_id = row.get("paper_id")
        card_field = _text(row.get("card_field"))
        if paper_id is None or not card_field:
            return None
        return EvidenceEntry(
            ref=f"card_field:{int(paper_id)}:{card_field}",
            evidence_type="card_field",
            label=f"论文 {paper_id} 卡片字段 {card_field}",
            support_scope="abstract_only",
            evidence_id=int(row["id"]) if row.get("id") is not None else None,
            paper_id=int(paper_id),
            card_field=card_field,
            quote_text=_text(row.get("quote_text")),
            summary=_squash(row.get("quote_text"), 160) or f"卡片字段 {card_field}",
            source=source,
        )
    if evidence_type == "experiment_run":
        run_id = row.get("experiment_run_id")
        if run_id is None:
            return None
        metric_name = _text(row.get("metric_name"))
        value = _num(row.get("metric_value"))
        return EvidenceEntry(
            ref=f"experiment_run:{int(run_id)}" + (f":{metric_name}" if metric_name else ""),
            evidence_type="experiment_run",
            label=f"实验运行 #{int(run_id)}" + (f" 指标 {metric_name}" if metric_name else ""),
            support_scope="experiment",
            evidence_id=int(row["id"]) if row.get("id") is not None else None,
            experiment_run_id=int(run_id),
            metric_name=metric_name,
            metric_value=value,
            summary=(
                f"运行 #{int(run_id)}" + (f"：{metric_name}={value}" if metric_name else "（真实落库）")
            ),
            source=source,
        )
    if evidence_type == "experiment_passport":
        passport_id = row.get("experiment_passport_id")
        if passport_id is None:
            return None
        return EvidenceEntry(
            ref=f"experiment_passport:{int(passport_id)}",
            evidence_type="experiment_passport",
            label=f"Passport #{int(passport_id)}",
            support_scope="experiment",
            evidence_id=int(row["id"]) if row.get("id") is not None else None,
            experiment_passport_id=int(passport_id),
            experiment_run_id=int(row["experiment_run_id"])
            if row.get("experiment_run_id") is not None
            else None,
            summary="Passport（可复现凭证）",
            source=source,
        )
    if evidence_type == "decision":
        log_id = row.get("decision_log_id")
        if log_id is None:
            return None
        return EvidenceEntry(
            ref=f"decision:{int(log_id)}",
            evidence_type="decision",
            label=f"决策日志 #{int(log_id)}",
            support_scope="decision",
            evidence_id=int(row["id"]) if row.get("id") is not None else None,
            decision_log_id=int(log_id),
            summary=_squash(row.get("quote_text"), 160) or "决策留痕",
            source=source,
        )
    return None


# --------------------------------------------------------------------------------------
# 大纲
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class OutlineItem:
    index: int
    heading: str
    purpose: str = ""
    focus: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "heading": self.heading, "purpose": self.purpose, "focus": self.focus}


#: 本地桩的大纲骨架（仅在 LLM 不可用时使用，如实标注 is_stub）
STUB_OUTLINE: tuple[tuple[str, str], ...] = (
    ("摘要", "凝练研究问题、方法与主要证据支持的结论"),
    ("研究问题与动机", "说明研究问题来源与现有证据显示的空白"),
    ("相关工作与证据基础", "转述已解析论文中与本研究直接相关的证据"),
    ("方法设计", "描述候选方案与其审计留痕（决策日志）"),
    ("实验与结果", "报告真实落库的实验指标与可复现凭证"),
    ("局限与后续工作", "如实列出证据不足之处与尚未验证的假设"),
)


def stub_outline(*, max_sections: int = 6, reasons: Sequence[str] = ()) -> dict[str, Any]:
    """本地桩大纲（确定性、可复现）。"""
    items = [
        {"heading": heading, "purpose": purpose, "focus": purpose}
        for heading, purpose in STUB_OUTLINE[: max(3, min(max_sections, len(STUB_OUTLINE)))]
    ]
    return {
        "outline": items,
        "rationale": "本地桩：LLM 不可用时按附录 D.5 的固定骨架产出大纲，未引入任何新事实",
        "is_stub": True,
        "stub_reasons": list(reasons),
    }


def build_outline_messages(
    *,
    taskbook: Mapping[str, Any],
    pool: EvidencePool,
    upstream: Mapping[str, Any],
    section_hint: int = 6,
) -> list[dict[str, Any]]:
    """D.5 大纲 prompt（系统前缀遵守附录 D.0；文本中不含任何外部出版渠道表述）。"""
    system = (
        "你是科研辅助系统的「论文写作」模块。你的输出将作为结构化数据被程序消费。\n"
        "必须严格遵守：1) 只输出合法 JSON，不要任何解释性文字；2) 所有结论性内容必须能追溯到"
        "给定证据引用键（ref）；3) 不确定时输出 unknown 而不是编造；"
        "4) 本文档仅用于研究者内部核验与技术审计，不得包含任何面向外部出版渠道的流程性表述。"
    )
    user = {
        "任务": "为下述研究项目设计论文式草稿的大纲，并说明组织理由",
        "研究问题": taskbook.get("research_question"),
        "目标数据集": taskbook.get("target_datasets"),
        "评价指标": taskbook.get("metrics"),
        "期望章节数": section_hint,
        "上游环节摘要": upstream,
        "可用证据引用键": [item["ref"] for item in pool.prompt_items(limit=40)],
        "输出格式": {
            "outline": [{"heading": "章节标题", "purpose": "该节要回答什么", "focus": "写作焦点"}],
            "rationale": "大纲组织理由",
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _json(user)},
    ]


def normalize_outline(raw: Mapping[str, Any] | None, *, max_sections: int = 8) -> dict[str, Any]:
    """校验并规范化模型返回的大纲；不合法则抛 ValueError（由环节决定降级）。"""
    payload = dict(raw or {})
    items = payload.get("outline")
    if not isinstance(items, list) or not items:
        raise ValueError("大纲缺少 outline[] 数组")
    normalized: list[dict[str, Any]] = []
    for position, item in enumerate(items[:max_sections]):
        if not isinstance(item, Mapping):
            raise ValueError(f"outline[{position}] 不是对象")
        heading = _squash(item.get("heading"), 60)
        if not heading:
            raise ValueError(f"outline[{position}] 缺少 heading")
        normalized.append(
            {
                "index": position,
                "heading": _sanitize_text(heading),
                "purpose": _sanitize_text(_squash(item.get("purpose"), 200) or ""),
                "focus": _sanitize_text(_squash(item.get("focus"), 200) or ""),
            }
        )
    if len(normalized) < 3:
        raise ValueError("大纲章节数不足 3（附录 D.5 要求至少覆盖问题/方法/证据）")
    return {
        "outline": normalized,
        "rationale": _sanitize_text(_squash(payload.get("rationale"), 500) or ""),
        "is_stub": False,
        "stub_reasons": [],
    }


def build_section_messages(
    *,
    heading: str,
    purpose: str,
    taskbook: Mapping[str, Any],
    pool: EvidencePool,
    upstream: Mapping[str, Any],
    max_claims: int = 6,
) -> list[dict[str, Any]]:
    """分节撰写 prompt：要求每条 Claim 给出 ``evidence_refs``（只能用给定 ref）。"""
    system = (
        "你是科研辅助系统的「论文写作」模块。你的输出将作为结构化数据被程序消费。\n"
        "必须严格遵守：1) 只输出合法 JSON，不要任何解释性文字；2) 每条 claim 的 evidence_refs "
        "只能取自给定证据引用键列表，禁止自造引用编号或引用键；3) 若该句无法由任何证据支撑，"
        "把 evidence_refs 置为空数组（系统会标为 insufficient，不要伪造证据）；"
        "4) 不确定时输出 unknown 而不是编造；5) 本文档仅用于研究者内部核验与技术审计，"
        "不得包含任何面向外部出版渠道的流程性表述。"
    )
    user = {
        "章节": heading,
        "本章目的": purpose,
        "研究问题": taskbook.get("research_question"),
        "上游环节摘要": upstream,
        "证据池": pool.prompt_items(limit=40),
        "写作要求": {
            "句式": "每一条 claim 是一个完整陈述句（中文以句号结尾），不得包含引用标记",
            "数量": f"1~{max_claims} 条",
            "禁止": "不得出现任何外部出版渠道相关表述；不得编造指标或引用",
        },
        "输出格式": {
            "claims": [
                {
                    "text": "完整陈述句",
                    "evidence_refs": ["paper_span:123"],
                    "grounded": True,
                }
            ]
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _json(user)},
    ]


def normalize_sections(
    raw: Mapping[str, Any] | None,
    *,
    heading: str,
    purpose: str,
    pool: EvidencePool,
    max_claims: int = 6,
) -> list[dict[str, Any]]:
    """校验模型返回的 Claim 列表：丢弃池外 ref（计入 ``dropped_refs``）。"""
    payload = dict(raw or {})
    claims = payload.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError(f"章节「{heading}」未返回任何 claim")
    normalized: list[dict[str, Any]] = []
    for position, item in enumerate(claims[:max_claims]):
        if not isinstance(item, Mapping):
            raise ValueError(f"章节「{heading}」claim[{position}] 不是对象")
        text_value = _clean_claim_text(item.get("text"))
        if len(text_value) < 8:
            raise ValueError(f"章节「{heading}」claim[{position}] 过短或为空")
        refs_raw = item.get("evidence_refs")
        refs: list[str] = []
        dropped: list[str] = []
        if isinstance(refs_raw, list):
            for ref in refs_raw:
                key = str(ref).strip()
                if not key:
                    continue
                if pool.has(key):
                    if key not in refs:
                        refs.append(key)
                elif key not in dropped:
                    dropped.append(key)
        normalized.append(
            {
                "text": text_value,
                "evidence_refs": refs,
                "dropped_refs": dropped,
                "grounded": bool(item.get("grounded", bool(refs))),
            }
        )
    return normalized


def stub_sections(
    *,
    heading: str,
    pool: EvidencePool,
    taskbook: Mapping[str, Any],
    max_claims: int = 4,
) -> list[dict[str, Any]]:
    """本地桩分节：**只转述证据池原文**；局限章节保留 1 条无证据 claim（如实标 insufficient）。"""
    claims: list[dict[str, Any]] = []
    section = heading or ""

    def add(text_value: str, refs: Sequence[str]) -> None:
        if len(claims) >= max_claims:
            return
        claims.append(
            {
                "text": _clean_claim_text(text_value),
                "evidence_refs": list(refs),
                "dropped_refs": [],
                "grounded": bool(refs),
            }
        )

    if "摘要" in section:
        entry = pool.first_of("paper_span")
        if entry is not None:
            add(f"现有可用证据中可定位的原文片段共 {len(pool.by_type('paper_span'))} 条，本节结论仅基于这些片段。", [entry.ref])
        add("本稿为科研辅助系统的自动化草稿，结论需研究者自行核验。", [])
    elif "研究问题" in section or "动机" in section:
        for entry in pool.by_type("paper_span")[:3]:
            add(f"证据片段指出：{_quote_snippet(entry)}", [entry.ref])
        question = _squash(taskbook.get("research_question"), 200)
        if question:
            add(f"本研究问题为：{question}", [])
    elif "相关工作" in section or "证据基础" in section:
        for entry in pool.by_type("paper_span")[:6]:
            add(f"论文 {entry.paper_id} 的 {entry.section_name or '正文'} 中写道：{_quote_snippet(entry)}", [entry.ref])
        for entry in pool.by_type("card_field")[:1]:
            add(f"结构化卡片字段 {entry.card_field}（论文 {entry.paper_id}）给出的记录是：{_quote_snippet(entry)}", [entry.ref])
    elif "方法" in section:
        for entry in pool.by_type("decision")[:4]:
            add(f"方法相关留痕：{entry.summary}", [entry.ref])
        for entry in pool.by_type("experiment_run")[:1]:
            add(f"实验配置留痕：{entry.summary}", [entry.ref])
    elif "实验" in section or "结果" in section:
        for entry in pool.by_type("experiment_run")[:3]:
            add(f"真实落库指标：{entry.summary}", [entry.ref])
        for entry in pool.by_type("experiment_passport")[:1]:
            add(f"可复现凭证记录：{entry.summary}", [entry.ref])
    elif "局限" in section or "后续" in section:
        add("当前证据池尚未覆盖该假设，本节不作结论，需补充实验或全文解析后再判定。", [])

    if not claims:
        entry = pool.first_of("paper_span", "experiment_run", "decision")
        if entry is not None:
            add(f"证据池条目记录：{entry.summary}", [entry.ref])
        else:
            add("本节无可用证据，未作任何结论性陈述。", [])
    return claims


def _quote_snippet(entry: EvidenceEntry, limit: int = 90) -> str:
    raw = entry.quote_text or entry.summary or "（无原文）"
    squashed = " ".join(str(raw).split())
    snippet = squashed[:limit]
    if not snippet.endswith(tuple(_SENTENCE_END)):
        snippet = snippet + "…"
    return f"“{snippet}”"


# --------------------------------------------------------------------------------------
# 草稿文档与渲染
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class ClaimDraft:
    """一条待渲染的 Claim（引用键已是池内键，编号在渲染时分配）。"""

    local_id: str
    text: str
    evidence_refs: list[str] = field(default_factory=list)
    dropped_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SectionDraft:
    heading: str
    purpose: str = ""
    claims: list[ClaimDraft] = field(default_factory=list)


@dataclass(slots=True)
class DraftDocument:
    """草稿文档：渲染即确定引用编号（服务端唯一权威）。"""

    title: str
    outline: list[dict[str, Any]] = field(default_factory=list)
    sections: list[SectionDraft] = field(default_factory=list)
    extra_notes: list[str] = field(default_factory=list)
    is_stub: bool = False
    stub_reasons: list[str] = field(default_factory=list)
    #: 渲染结果（render_markdown 后填充）
    citation_numbers: dict[str, int] = field(default_factory=dict)
    citation_refs: dict[int, str] = field(default_factory=dict)
    claim_citations: dict[str, list[int]] = field(default_factory=dict)
    dropped_refs: list[str] = field(default_factory=list)
    sanitized_phrases: list[str] = field(default_factory=list)

    def render_markdown(self, *, number: int = 1) -> str:
        """渲染 Markdown：``[n]`` 编号按首次出现顺序分配，文末给出引用索引。"""
        self.citation_numbers = {}
        self.citation_refs = {}
        self.claim_citations = {}
        self.dropped_refs = []
        self.sanitized_phrases = []

        def cite(refs: Sequence[str]) -> str:
            numbers: list[int] = []
            for ref in refs:
                key = str(ref).strip()
                if not key:
                    continue
                if key not in self.citation_numbers:
                    assigned = len(self.citation_numbers) + 1
                    self.citation_numbers[key] = assigned
                    self.citation_refs[assigned] = key
                numbers.append(self.citation_numbers[key])
            if not numbers:
                return ""
            return "[" + "][".join(str(value) for value in numbers) + "]"

        lines: list[str] = [f"# {self.title}", ""]
        lines.append(f"> {DISCLAIMER}")
        lines.append("")
        lines.append(
            "> 合规声明：本文件为科研辅助系统产出，仅用于研究者内部核验与技术审计留痕，"
            "不构成任何外部出版渠道的流程安排。"
        )
        lines.append("")
        if self.is_stub:
            reasons = "；".join(self.stub_reasons) if self.stub_reasons else "LLM 不可用"
            lines.append(f"> 生成方式：本地桩（{reasons}）。桩只转述证据池内的真实记录，未引入新事实。")
            lines.append("")

        for position, section in enumerate(self.sections, start=1):
            heading = _sanitize_text(section.heading) or f"第 {position} 节"
            lines.append(f"## {position}. {heading}")
            lines.append("")
            if section.purpose:
                lines.append(f"本节目的：{_sanitize_text(section.purpose)}")
                lines.append("")
            for claim in section.claims:
                text_value = _clean_claim_text(claim.text)
                marker = cite(claim.evidence_refs)
                lines.append(f"{text_value}{marker}" if marker else text_value)
                self.claim_citations[claim.local_id] = [
                    self.citation_numbers[ref] for ref in claim.evidence_refs if ref in self.citation_numbers
                ]
                for ref in claim.dropped_refs:
                    if ref not in self.dropped_refs:
                        self.dropped_refs.append(ref)
            lines.append("")

        lines.append("## 引用索引（证据池映射）")
        lines.append("")
        if self.citation_refs:
            ordered = sorted(self.citation_refs)
            for number in ordered:
                lines.append(f"[{number}] {self.citation_refs[number]} | 对应证据见证据池（evidence_id 由服务端绑定）")
        else:
            lines.append("（本稿未引用任何证据：证据池为空或全部 Claim 无证据，已如实标注）")
        lines.append("")

        if self.extra_notes:
            lines.append("## 生成说明")
            lines.append("")
            for note in self.extra_notes:
                lines.append(f"- {_sanitize_text(note)}")
            lines.append("")

        lines.append(f"> {DISCLAIMER}")
        lines.append("")
        # 合规扫描（渲染后再扫一遍，命中即替换并留痕）
        return self._sanitize_markdown("\n".join(lines))

    # -- 合规 ---------------------------------------------------------------- #
    def _sanitize_markdown(self, markdown: str) -> str:
        sanitized = markdown
        for phrase in FORBIDDEN_PUBLISH_PHRASES:
            if phrase.lower() in sanitized.lower():
                self.sanitized_phrases.append(phrase)
                sanitized = re.sub(re.escape(phrase), "[已按合规词表移除]", sanitized, flags=re.IGNORECASE)
        return sanitized

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "outline": list(self.outline),
            "sections": [
                {
                    "heading": section.heading,
                    "purpose": section.purpose,
                    "claims": [
                        {
                            "local_id": claim.local_id,
                            "text": claim.text,
                            "evidence_refs": list(claim.evidence_refs),
                            "dropped_refs": list(claim.dropped_refs),
                        }
                        for claim in section.claims
                    ],
                }
                for section in self.sections
            ],
            "citations": {str(number): ref for number, ref in sorted(self.citation_refs.items())},
            "citation_numbers": dict(self.citation_numbers),
            "dropped_refs": list(self.dropped_refs),
            "sanitized_phrases": list(self.sanitized_phrases),
            "is_stub": self.is_stub,
            "stub_reasons": list(self.stub_reasons),
        }


# --------------------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------------------
def scan_forbidden(text_value: str | None) -> list[str]:
    """合规词表扫描：返回命中的短语（大小写不敏感）。"""
    if not text_value:
        return []
    lowered = str(text_value).lower()
    return [phrase for phrase in FORBIDDEN_PUBLISH_PHRASES if phrase.lower() in lowered]


def _sanitize_text(text_value: str | None) -> str:
    value = str(text_value or "")
    for phrase in FORBIDDEN_PUBLISH_PHRASES:
        if phrase.lower() in value.lower():
            value = re.sub(re.escape(phrase), "[已按合规词表移除]", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def _clean_claim_text(text_value: Any) -> str:
    """Claim 文本规范化：单句、无换行、无引用标记（编号由渲染阶段分配）。"""
    value = " ".join(str(text_value or "").split())
    value = CITATION_RE.sub("", value)
    value = value.replace("]", "").replace("[", "")
    value = value.strip()
    if value and not value.endswith(tuple(_SENTENCE_END)):
        value = value + "。"
    return _sanitize_text(value)


def parse_citation_index(content_md: str) -> dict[int, str]:
    """从草稿的「引用索引」反解 ``{编号: 引用键}``（导出与校验共用同一解析口径）。"""
    mapping: dict[int, str] = {}
    for match in REF_LINE_RE.finditer(content_md or ""):
        try:
            mapping[int(match.group(1))] = match.group(2)
        except (TypeError, ValueError):  # pragma: no cover - 正则已限定数字
            continue
    return mapping


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


__all__ = [
    "CITATION_RE",
    "DISCLAIMER",
    "EVIDENCE_TYPE_PREFIX",
    "FORBIDDEN_PUBLISH_PHRASES",
    "OUTLINE_SCHEMA",
    "REF_LINE_RE",
    "SECTION_SCHEMA",
    "ClaimDraft",
    "DraftDocument",
    "EvidenceEntry",
    "EvidencePool",
    "OutlineItem",
    "PROMPT_VERSION",
    "STUB_OUTLINE",
    "SectionDraft",
    "build_evidence_pool",
    "build_outline_messages",
    "build_section_messages",
    "normalize_outline",
    "normalize_sections",
    "parse_citation_index",
    "scan_forbidden",
    "stub_outline",
    "stub_sections",
]
