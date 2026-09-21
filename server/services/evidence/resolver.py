# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""多态证据解析器（WP13-T1，附录 A.3 的 ``evidences`` + 附录 E.3 的返回结构）。

设计要点
--------
1. **五类证据统一出口**：``paper_span`` / ``card_field`` / ``experiment_run`` /
   ``experiment_passport`` / ``decision`` 都解析为附录 E.3 的同一形状
   （``evidence_id / evidence_type / paper / span / quote_text / quote_sha256 /
   verification / jump_url``），差异部分放在各自的具名子对象里。
2. **哈希优先**：``paper_span`` 与 ``card_field`` 一律复用 WP05 的
   :func:`services.fulltext.verify_span`（先比对 ``quote_sha256``，
   再比对字符偏移），绝不自行实现第二套定位逻辑。
3. **定位必须携带 ``document_version``**：返回值里 ``document_version`` 必填，
   ``span`` 是 ``{section_name,page_number,bbox,char_start,char_end}`` 的复合体；
   禁止把裸字符偏移当作唯一定位依据（contracts.forbidden_actions）。
4. **fulltext_gate 如实披露**：``coverage < 0.60`` 或 ``parse_status != 'ok'`` 的文档，
   返回值里 ``gate.ok=false`` 且 ``evidence_scope='abstract_only'``，
   绝不在未达标文档上宣称正文级证据。

会话兼容：同时接受 ``AsyncSession`` 与同步 ``Session``（内部按 awaitable 兼容），
因此 FastAPI 路由、后台任务、脚本三处共用同一份实现。
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from services.evidence.records import (
    EVIDENCE_TYPES,
    Claim,
    EvidenceError,
    EvidenceNotFoundError,
    EvidenceRow,
    InvalidEvidenceTypeError,
    claim_coverage,
    is_evidence_type,
    num,
    status_counts,
)
from services.fulltext import (
    coverage_note,
    evidence_scope,
    spans_allowed,
    verify_span,
)
from services.fulltext.text_cache import TextCache, default_text_cache

logger = logging.getLogger("sciloop.wp13.resolver")

#: ``paper_cards`` 的 8 字段（附录 A.2；用于 ``card_field`` 证据取值白名单）
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

#: ``GET /evidence/{type}/{id}`` 的 id 语义
ID_KINDS: tuple[str, ...] = ("evidence", "native")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _rows(session: Any, sql: str, **params: Any) -> list[Mapping[str, Any]]:
    result = await _maybe_await(session.execute(text(sql), params))
    return [dict(row) for row in result.mappings().all()]


async def _row(session: Any, sql: str, **params: Any) -> Mapping[str, Any] | None:
    rows = await _rows(session, sql, **params)
    return rows[0] if rows else None


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _text_of(value: Any) -> str | None:
    """JSONB / 文本字段归一成可引用的字符串（``None`` 保持 ``None``）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - JSONB 一定可序列化
        return str(value)


# --------------------------------------------------------------------------- #
# 行装载
# --------------------------------------------------------------------------- #
async def load_evidence_row(session: Any, evidence_id: int) -> EvidenceRow:
    """按 ``evidences.id`` 装载一行；不存在抛 :class:`EvidenceNotFoundError`。"""
    row = await _row(
        session,
        """
        SELECT id, owner_type, owner_id, evidence_type, paper_id, paper_span_id,
               card_field, experiment_run_id, experiment_passport_id, metric_name,
               metric_value, decision_log_id, quote_text, weight, created_at
          FROM evidences
         WHERE id = :id
        """,
        id=int(evidence_id),
    )
    if row is None:
        raise EvidenceNotFoundError(
            f"evidences 表中不存在 id={evidence_id}",
            {"evidence_id": int(evidence_id)},
        )
    return _evidence_row_from_mapping(row)


def _evidence_row_from_mapping(row: Mapping[str, Any]) -> EvidenceRow:
    return EvidenceRow(
        id=int(row["id"]),
        owner_type=str(row["owner_type"]),
        owner_id=int(row["owner_id"]),
        evidence_type=str(row["evidence_type"]),
        paper_id=int(row["paper_id"]) if row.get("paper_id") is not None else None,
        paper_span_id=(
            int(row["paper_span_id"]) if row.get("paper_span_id") is not None else None
        ),
        card_field=row.get("card_field"),
        experiment_run_id=(
            int(row["experiment_run_id"]) if row.get("experiment_run_id") is not None else None
        ),
        experiment_passport_id=(
            int(row["experiment_passport_id"])
            if row.get("experiment_passport_id") is not None
            else None
        ),
        metric_name=row.get("metric_name"),
        metric_value=num(row.get("metric_value")),
        decision_log_id=(
            int(row["decision_log_id"]) if row.get("decision_log_id") is not None else None
        ),
        quote_text=row.get("quote_text"),
        weight=num(row.get("weight")),
        created_at=row.get("created_at"),
    )


async def _load_paper(session: Any, paper_id: int | None) -> dict[str, Any] | None:
    """论文摘要（附录 E.3 的 ``paper`` 子对象）；字段缺失一律 ``null``，不编造。"""
    if paper_id is None:
        return None
    row = await _row(
        session,
        """
        SELECT id, title, venue, venue_source, published_at, doi, source, external_id
          FROM papers
         WHERE id = :id
        """,
        id=int(paper_id),
    )
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "title": row.get("title"),
        "venue": row.get("venue"),
        "venue_source": row.get("venue_source"),
        "published_at": iso(row.get("published_at")),
        "doi": row.get("doi"),
        "source": row.get("source"),
        "external_id": row.get("external_id"),
    }


async def _load_document(
    session: Any, paper_id: int | None, document_version: str | None
) -> Mapping[str, Any] | None:
    if paper_id is None or not document_version:
        return None
    return await _row(
        session,
        """
        SELECT id, paper_id, document_version, source_type, parser, page_count,
               char_count, locatable_chars, coverage, parse_status, parse_error
          FROM paper_documents
         WHERE paper_id = :paper_id AND document_version = :document_version
        """,
        paper_id=int(paper_id),
        document_version=str(document_version),
    )


async def _load_span(session: Any, span_id: int) -> Mapping[str, Any] | None:
    return await _row(
        session,
        """
        SELECT id, paper_id, document_version, section_name, page_number, bbox,
               char_start, char_end, quote_text, quote_sha256
          FROM paper_spans
         WHERE id = :id
        """,
        id=int(span_id),
    )


def _gate_payload(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """``fulltext_gate`` 的如实披露（coverage / parse_status / 是否允许正文级证据）。"""
    if document is None:
        return {
            "ok": False,
            "parse_status": None,
            "coverage": None,
            "reason": "无 paper_documents 记录：全文未解析，证据覆盖范围：仅摘要",
            "coverage_note": "解析状态未知，证据覆盖范围：仅摘要",
            "evidence_scope": "abstract_only",
        }
    parse_status = document.get("parse_status")
    coverage = num(document.get("coverage"))
    ok = spans_allowed(parse_status, coverage)
    return {
        "ok": ok,
        "parse_status": parse_status,
        "coverage": coverage,
        "reason": coverage_note(parse_status, coverage),
        "coverage_note": coverage_note(parse_status, coverage),
        "evidence_scope": evidence_scope(parse_status, coverage),
    }


def _jump_url(evidence_type: str, native_id: int, paper_id: int | None) -> str:
    """可跳转 URL（与 WP01 路由表一致：``/parse/:paperId``、``/workbench/:projectId``）。"""
    if evidence_type == "paper_span":
        return f"/parse/{paper_id}#span-{native_id}"
    if evidence_type == "card_field":
        return f"/parse/{paper_id}#card-{native_id}"
    if evidence_type == "experiment_run":
        return f"/workbench/demo#run-{native_id}"
    if evidence_type == "experiment_passport":
        return f"/workbench/demo#passport-{native_id}"
    return f"/workbench/demo#decision-{native_id}"


def _native_id(row: EvidenceRow) -> int:
    """本类型对应的原生主键（不存在时返回 0，调用方已在校验中拦下）。"""
    mapping = {
        "paper_span": row.paper_span_id,
        "card_field": row.paper_span_id or row.paper_id,
        "experiment_run": row.experiment_run_id,
        "experiment_passport": row.experiment_passport_id,
        "decision": row.decision_log_id,
    }
    value = mapping.get(row.evidence_type)
    return int(value) if value is not None else 0


def _base_payload(row: EvidenceRow) -> dict[str, Any]:
    return {
        "evidence_id": row.id,
        "evidence_type": row.evidence_type,
        "owner_type": row.owner_type,
        "owner_id": row.owner_id,
        "weight": row.weight,
        "paper": None,
        "document_version": None,
        "span": None,
        "card": None,
        "run": None,
        "passport": None,
        "decision": None,
        "metric": None,
        "quote_text": row.quote_text,
        "quote_sha256": None,
        "verification": None,
        "gate": _gate_payload(None),
        "evidence_scope": "abstract_only",
        "jump_url": None,
        "resolved_at": datetime.now(UTC).isoformat(),
        "warnings": [],
    }


# --------------------------------------------------------------------------- #
# 各类型解析
# --------------------------------------------------------------------------- #
async def _resolve_paper_span(
    session: Any,
    row: EvidenceRow,
    *,
    text_cache: TextCache | None = None,
    document_text: str | None = None,
) -> dict[str, Any]:
    payload = _base_payload(row)
    if row.paper_span_id is None:
        raise EvidenceNotFoundError(
            "paper_span 证据缺少 paper_span_id，无法定位",
            {"evidence_id": row.id},
        )
    span = await _load_span(session, row.paper_span_id)
    if span is None:
        raise EvidenceNotFoundError(
            f"paper_spans 中不存在 id={row.paper_span_id}（被引用片段已删除）",
            {"evidence_id": row.id, "paper_span_id": row.paper_span_id},
        )

    paper_id = int(span["paper_id"])
    document_version = str(span["document_version"])
    document = await _load_document(session, paper_id, document_version)
    gate = _gate_payload(document)

    verification = verify_span(span, document_text, text_cache=text_cache)
    payload.update(
        {
            "paper": await _load_paper(session, paper_id),
            "document_version": document_version,
            "span": {
                "paper_span_id": int(span["id"]),
                "section_name": span.get("section_name"),
                "page_number": span.get("page_number"),
                "bbox": span.get("bbox"),
                "char_start": int(span["char_start"]),
                "char_end": int(span["char_end"]),
            },
            "quote_text": span.get("quote_text"),
            "quote_sha256": span.get("quote_sha256"),
            "has_quote_sha256": bool(span.get("quote_sha256")),
            "verification": verification,
            "gate": gate,
            "evidence_scope": gate["evidence_scope"],
            "jump_url": _jump_url("paper_span", int(span["id"]), paper_id),
        }
    )
    if not gate["ok"]:
        payload["warnings"].append(
            "该 document_version 未通过 fulltext_gate（需 parse_status='ok' 且 "
            f"coverage>=0.60）：{gate['reason']}"
        )
    if verification["verdict"] == "invalid":
        payload["warnings"].append(
            "引用文本与落库时不一致（quote_sha256 不匹配）：证据不成立，对应 Claim 应为 insufficient"
        )
    elif verification["verdict"] == "valid_by_hash":
        payload["warnings"].append("偏移已漂移但文本一致：以引用文本为准，精确定位建议重新锚定")
    return payload


async def _resolve_card_field(
    session: Any,
    row: EvidenceRow,
    *,
    text_cache: TextCache | None = None,
    document_text: str | None = None,
) -> dict[str, Any]:
    payload = _base_payload(row)
    field = row.card_field
    if not field:
        raise EvidenceError("invalid_evidence", "card_field 证据缺少 card_field 字段名", None)
    if field not in CARD_FIELDS:
        raise EvidenceError(
            "invalid_card_field",
            f"card_field='{field}' 不属于 8 字段白名单",
            {"allowed": list(CARD_FIELDS)},
        )

    paper_id = row.paper_id
    card: Mapping[str, Any] | None = None
    if paper_id is not None:
        card = await _row(
            session,
            f"""
            SELECT id, paper_id, version, {field} AS field_value, created_at
              FROM paper_cards
             WHERE paper_id = :paper_id
             ORDER BY version DESC, id DESC
             LIMIT 1
            """,
            paper_id=int(paper_id),
        )

    # WP06 卡片未就绪时安全降级：保留字段名与论文，值置 null 并明示原因
    if card is None:
        payload.update(
            {
                "paper": await _load_paper(session, paper_id),
                "card": {
                    "card_id": None,
                    "version": None,
                    "field": field,
                    "value": None,
                    "value_available": False,
                    "unavailable_reason": (
                        "paper_cards 尚无该论文的卡片记录（WP06 未生成）：字段值不可引用"
                    ),
                },
                "gate": _gate_payload(None),
                "evidence_scope": "abstract_only",
                "jump_url": _jump_url("card_field", row.paper_span_id or 0, paper_id),
            }
        )
        payload["warnings"].append("card_field 取值缺失：不得据此宣称卡片级证据")
        return payload

    raw_value = card.get("field_value")
    value_text = _text_of(raw_value)

    verification: dict[str, Any] | None = None
    span_payload: dict[str, Any] | None = None
    document_version: str | None = None
    gate = _gate_payload(None)
    if row.paper_span_id is not None:
        span = await _load_span(session, row.paper_span_id)
        if span is not None:
            span_paper_id = int(span["paper_id"])
            document_version = str(span["document_version"])
            gate = _gate_payload(await _load_document(session, span_paper_id, document_version))
            verification = verify_span(span, document_text, text_cache=text_cache)
            span_payload = {
                "paper_span_id": int(span["id"]),
                "section_name": span.get("section_name"),
                "page_number": span.get("page_number"),
                "bbox": span.get("bbox"),
                "char_start": int(span["char_start"]),
                "char_end": int(span["char_end"]),
            }
            payload["quote_text"] = span.get("quote_text")
            payload["quote_sha256"] = span.get("quote_sha256")
            payload["has_quote_sha256"] = bool(span.get("quote_sha256"))
        else:
            payload["warnings"].append(
                f"card_field 关联的 paper_span_id={row.paper_span_id} 不存在：定位失效，"
                "该证据视为无原文支撑"
            )
    else:
        payload["warnings"].append(
            "card_field 未关联 paper_span（卡片字段未在原文定位）：证据覆盖范围：仅摘要"
        )

    payload.update(
        {
            "paper": await _load_paper(session, paper_id if paper_id is not None else None),
            "document_version": document_version,
            "span": span_payload,
            "card": {
                "card_id": int(card["id"]),
                "version": int(card["version"]),
                "field": field,
                "value": value_text,
                "value_available": value_text is not None,
                "created_at": iso(card.get("created_at")),
            },
            "quote_text": payload.get("quote_text") or value_text,
            "verification": verification,
            "gate": gate,
            "evidence_scope": gate["evidence_scope"],
            "jump_url": _jump_url(
                "card_field",
                int(card["id"]) if row.paper_span_id is None else int(row.paper_span_id),
                paper_id,
            ),
        }
    )
    if verification is not None and verification["verdict"] == "invalid":
        payload["warnings"].append(
            "卡片字段的原文定位引用已失效（quote_sha256 不匹配）：只能按摘要级证据使用"
        )
    return payload


async def _resolve_experiment_run(session: Any, row: EvidenceRow) -> dict[str, Any]:
    payload = _base_payload(row)
    if row.experiment_run_id is None:
        raise EvidenceNotFoundError(
            "experiment_run 证据缺少 experiment_run_id", {"evidence_id": row.id}
        )
    run = await _row(
        session,
        """
        SELECT er.id, er.experiment_id, er.attempt, er.status, er.artifact_path,
               er.error, er.duration_ms, er.created_at,
               e.template_id, e.config,
               pr.project_id
          FROM experiment_runs er
          JOIN experiments e ON e.id = er.experiment_id
          LEFT JOIN stage_outputs so ON so.id = e.stage_output_id
          LEFT JOIN pipeline_runs pr ON pr.id = so.pipeline_run_id
         WHERE er.id = :id
        """,
        id=int(row.experiment_run_id),
    )
    if run is None:
        raise EvidenceNotFoundError(
            f"experiment_runs 中不存在 id={row.experiment_run_id}",
            {"evidence_id": row.id},
        )

    metric_name = row.metric_name
    metric_value = row.metric_value
    if metric_name and metric_value is None:
        metric = await _row(
            session,
            """
            SELECT metric_name, metric_value, metric_unit
              FROM experiment_metrics
             WHERE experiment_run_id = :run_id AND metric_name = :metric_name
             ORDER BY id DESC LIMIT 1
            """,
            run_id=int(row.experiment_run_id),
            metric_name=str(metric_name),
        )
        if metric is not None:
            metric_value = num(metric.get("metric_value"))

    project_id = int(run["project_id"]) if run.get("project_id") is not None else None
    quote_text = row.quote_text
    if not quote_text and metric_name:
        quote_text = f"experiment_run#{run['id']} {metric_name}={metric_value}"

    payload.update(
        {
            "run": {
                "run_id": int(run["id"]),
                "experiment_id": int(run["experiment_id"]),
                "template_id": run.get("template_id"),
                "attempt": run.get("attempt"),
                "status": run.get("status"),
                "duration_ms": run.get("duration_ms"),
                "artifact_path": run.get("artifact_path"),
                "error": run.get("error"),
                "created_at": iso(run.get("created_at")),
                "project_id": project_id,
                "config": run.get("config"),
            },
            "metric": (
                {"name": metric_name, "value": metric_value} if metric_name else None
            ),
            "quote_text": quote_text,
            "gate": _gate_payload(None),
            "evidence_scope": "experiment",
            "jump_url": (
                f"/workbench/{project_id}#run-{int(run['id'])}"
                if project_id is not None
                else _jump_url("experiment_run", int(run["id"]), None)
            ),
        }
    )
    if metric_name and metric_value is None:
        payload["warnings"].append(
            f"experiment_metrics 中查不到指标 {metric_name}：数值置 null，不编造"
        )
    return payload


async def _resolve_experiment_passport(session: Any, row: EvidenceRow) -> dict[str, Any]:
    payload = _base_payload(row)
    if row.experiment_passport_id is None:
        raise EvidenceNotFoundError(
            "experiment_passport 证据缺少 experiment_passport_id", {"evidence_id": row.id}
        )
    passport = await _row(
        session,
        """
        SELECT p.id, p.passport_uid, p.experiment_run_id, p.parent_passport_id,
               p.dataset_name, p.dataset_version, p.dataset_sha256, p.provider,
               p.model_id, p.prompt_version, p.prompt_sha256, p.template_id,
               p.metrics, p.cost_usd, p.is_replay, p.status, p.started_at,
               p.finished_at, p.created_at, pr.project_id
          FROM experiment_passports p
          LEFT JOIN experiment_runs er ON er.id = p.experiment_run_id
          LEFT JOIN experiments e ON e.id = er.experiment_id
          LEFT JOIN stage_outputs so ON so.id = e.stage_output_id
          LEFT JOIN pipeline_runs pr ON pr.id = so.pipeline_run_id
         WHERE p.id = :id
        """,
        id=int(row.experiment_passport_id),
    )
    if passport is None:
        raise EvidenceNotFoundError(
            f"experiment_passports 中不存在 id={row.experiment_passport_id}",
            {"evidence_id": row.id},
        )

    project_id = int(passport["project_id"]) if passport.get("project_id") is not None else None
    metrics = passport.get("metrics")
    metric_name = row.metric_name
    metric_value = row.metric_value
    if metric_name and metric_value is None and isinstance(metrics, Mapping):
        metric_value = num(metrics.get(metric_name))

    payload.update(
        {
            "passport": {
                "passport_id": int(passport["id"]),
                "passport_uid": str(passport["passport_uid"]),
                "experiment_run_id": int(passport["experiment_run_id"]),
                "parent_passport_id": (
                    int(passport["parent_passport_id"])
                    if passport.get("parent_passport_id") is not None
                    else None
                ),
                "status": passport.get("status"),
                "is_replay": bool(passport.get("is_replay")),
                "template_id": passport.get("template_id"),
                "provider": passport.get("provider"),
                "model_id": passport.get("model_id"),
                "prompt_version": passport.get("prompt_version"),
                "prompt_sha256": passport.get("prompt_sha256"),
                "dataset_name": passport.get("dataset_name"),
                "dataset_version": passport.get("dataset_version"),
                "dataset_sha256": passport.get("dataset_sha256"),
                "cost_usd": num(passport.get("cost_usd")),
                "metrics": metrics,
                "started_at": iso(passport.get("started_at")),
                "finished_at": iso(passport.get("finished_at")),
                "created_at": iso(passport.get("created_at")),
                "project_id": project_id,
            },
            "metric": (
                {"name": metric_name, "value": metric_value} if metric_name else None
            ),
            "quote_text": row.quote_text
            or f"experiment_passport#{passport['id']} status={passport.get('status')}",
            "gate": _gate_payload(None),
            "evidence_scope": "experiment",
            "jump_url": (
                f"/workbench/{project_id}#passport-{int(passport['id'])}"
                if project_id is not None
                else _jump_url("experiment_passport", int(passport["id"]), None)
            ),
        }
    )
    if passport.get("status") != "complete":
        payload["warnings"].append(
            f"Passport 状态为 {passport.get('status')}：关键字段缺失，禁止宣称可复现"
        )
    if bool(passport.get("is_replay")):
        payload["warnings"].append("该 Passport 为回放结果（is_replay=true），不得冒充实时实验")
    return payload


async def _resolve_decision(session: Any, row: EvidenceRow) -> dict[str, Any]:
    payload = _base_payload(row)
    if row.decision_log_id is None:
        raise EvidenceNotFoundError(
            "decision 证据缺少 decision_log_id", {"evidence_id": row.id}
        )
    log = await _row(
        session,
        """
        SELECT id, project_id, pipeline_run_id, decision_point, stage, context_digest,
               options_considered, chosen, rationale, risk_score, confidence_score,
               reversibility_score, policy_action, policy_version, guardrail_checks,
               cost_usd, created_at
          FROM decision_logs
         WHERE id = :id
        """,
        id=int(row.decision_log_id),
    )
    if log is None:
        raise EvidenceNotFoundError(
            f"decision_logs 中不存在 id={row.decision_log_id}", {"evidence_id": row.id}
        )
    project_id = int(log["project_id"])
    payload.update(
        {
            "decision": {
                "decision_log_id": int(log["id"]),
                "project_id": project_id,
                "pipeline_run_id": (
                    int(log["pipeline_run_id"])
                    if log.get("pipeline_run_id") is not None
                    else None
                ),
                "decision_point": log.get("decision_point"),
                "stage": log.get("stage"),
                "chosen": log.get("chosen"),
                "rationale": log.get("rationale"),
                "risk_score": num(log.get("risk_score")),
                "confidence_score": num(log.get("confidence_score")),
                "reversibility_score": num(log.get("reversibility_score")),
                "policy_action": log.get("policy_action"),
                "policy_version": log.get("policy_version"),
                "guardrail_checks": log.get("guardrail_checks"),
                "options_considered": log.get("options_considered"),
                "cost_usd": num(log.get("cost_usd")),
                "created_at": iso(log.get("created_at")),
            },
            "quote_text": row.quote_text or (log.get("rationale") or "")[:2000] or None,
            "gate": _gate_payload(None),
            "evidence_scope": "decision",
            "jump_url": f"/workbench/{project_id}#decision-{int(log['id'])}",
        }
    )
    return payload


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
async def resolve_evidence(
    evidence_type: str,
    evidence_id: int,
    *,
    session: Any,
    id_kind: str = "evidence",
    card_field: str | None = None,
    text_cache: TextCache | None = None,
    document_text: str | None = None,
) -> dict[str, Any]:
    """解析一条证据为可跳转结构（附录 E.3）。

    :param evidence_type: ``paper_span|card_field|experiment_run|experiment_passport|decision``
    :param evidence_id: ``id_kind='evidence'`` 时为 ``evidences.id``；
        ``'native'`` 时为对应类型的主键（``paper_spans.id`` / ``paper_cards.id`` /
        ``experiment_runs.id`` / ``experiment_passports.id`` / ``decision_logs.id``）
    :param session: ``AsyncSession`` 或同步 ``Session``
    :param id_kind: ``evidence``（默认）或 ``native``
    :param card_field: **仅 ``id_kind='native'`` 且 ``evidence_type='card_field'`` 时使用**：
        ``paper_cards`` 上是 8 字段宽表，单个 id 不足以确定要引用哪个字段，
        故由调用方显式给出；缺省 ``core_method``。``id_kind='evidence'`` 时忽略此参数
        （字段名一律取 ``evidences.card_field``）。
    :param text_cache: 全文缓存（缺省用进程内默认实例）
    :param document_text: 显式提供的归一全文（供离线校验；为 ``None`` 时读缓存）
    :returns: 附录 E.3 的统一结构（含 ``verification`` / ``gate`` / ``jump_url``）
    :raises InvalidEvidenceTypeError: ``evidence_type`` 不在受控值域
    :raises EvidenceNotFoundError: 证据行或原生行不存在
    """
    if not is_evidence_type(evidence_type):
        raise InvalidEvidenceTypeError(str(evidence_type))
    if id_kind not in ID_KINDS:
        raise EvidenceError(
            "invalid_id_kind",
            f"id_kind='{id_kind}' 不合法",
            {"allowed": list(ID_KINDS)},
        )
    cache = text_cache if text_cache is not None else default_text_cache()

    if id_kind == "evidence":
        row = await load_evidence_row(session, evidence_id)
        if row.evidence_type != evidence_type:
            raise EvidenceError(
                "evidence_type_mismatch",
                f"evidences.id={evidence_id} 的实际类型为 {row.evidence_type}，"
                f"与请求的 {evidence_type} 不一致",
                {"actual": row.evidence_type, "requested": evidence_type},
            )
    else:
        row = await _synthetic_row(
            session, evidence_type, int(evidence_id), card_field=card_field
        )

    if row.evidence_type == "paper_span":
        return await _resolve_paper_span(
            session, row, text_cache=cache, document_text=document_text
        )
    if row.evidence_type == "card_field":
        return await _resolve_card_field(
            session, row, text_cache=cache, document_text=document_text
        )
    if row.evidence_type == "experiment_run":
        return await _resolve_experiment_run(session, row)
    if row.evidence_type == "experiment_passport":
        return await _resolve_experiment_passport(session, row)
    return await _resolve_decision(session, row)


async def _synthetic_row(
    session: Any,
    evidence_type: str,
    native_id: int,
    *,
    card_field: str | None = None,
) -> EvidenceRow:
    """``id_kind='native'`` 时的临时行（不落库，仅用于解析）。

    ``card_field`` 的特例：``paper_cards`` 是 8 字段宽表，原生 id 只到论文粒度，
    因此这里回查 ``paper_id`` 并由调用方指定字段名（缺省 ``core_method``），
    避免出现 ``jump_url=/parse/None#card-…`` 这种不可跳转的残缺结果。
    """
    row = EvidenceRow(
        id=native_id, owner_type="__native__", owner_id=native_id, evidence_type=evidence_type
    )
    if evidence_type == "paper_span":
        row.paper_span_id = native_id
    elif evidence_type == "card_field":
        field = (card_field or "core_method").strip() or "core_method"
        card = await _row(
            session,
            "SELECT id, paper_id FROM paper_cards WHERE id = :id",
            id=int(native_id),
        )
        if card is None:
            raise EvidenceNotFoundError(
                f"paper_cards 中不存在 id={native_id}",
                {"evidence_type": "card_field", "card_id": int(native_id)},
            )
        row.paper_id = int(card["paper_id"])
        row.card_field = field
    elif evidence_type == "experiment_run":
        row.experiment_run_id = native_id
    elif evidence_type == "experiment_passport":
        row.experiment_passport_id = native_id
    else:
        row.decision_log_id = native_id
    return row


async def resolve_many(
    evidence_type: str,
    evidence_ids: Sequence[int],
    *,
    session: Any,
    id_kind: str = "evidence",
    text_cache: TextCache | None = None,
) -> dict[str, Any]:
    """批量解析（单条失败不拖垮整批，失败原因如实返回）。"""
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for evidence_id in evidence_ids:
        try:
            items.append(
                await resolve_evidence(
                    evidence_type,
                    int(evidence_id),
                    session=session,
                    id_kind=id_kind,
                    text_cache=text_cache,
                )
            )
        except EvidenceError as exc:
            errors.append(
                {"evidence_id": int(evidence_id), "code": exc.code, "message": exc.message}
            )
    return {"evidence_type": evidence_type, "items": items, "errors": errors}


__all__ = [
    "CARD_FIELDS",
    "EVIDENCE_TYPES",
    "ID_KINDS",
    "claim_coverage",
    "iso",
    "load_evidence_row",
    "resolve_evidence",
    "resolve_many",
    "status_counts",
    "Claim",
]
