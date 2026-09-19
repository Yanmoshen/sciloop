# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""证据绑定服务（WP13-T2）。

对外稳定签名（WP08 的 idea 生成、WP08 的可行性、WP14 的草稿写作复用）::

    bound = await bind_evidence("idea", idea_id, candidates, session=session)
    detail = await bind_evidence_detailed("idea", idea_id, candidates, session=session)
    rows = await list_evidence("idea", idea_id, session=session)
    await clear_evidence("draft_claim", claim_id, session=session)

候选（``candidates``）字段
--------------------------
每条候选是一个 dict，公共字段：``evidence_type``（必填）、``weight``（默认 1.0）。
按类型补充：::

    {"evidence_type": "paper_span",  "paper_span_id": 123, "quote_text": "…"(可选)}
    {"evidence_type": "card_field",  "paper_id": 512, "card_field": "core_method",
                                     "paper_span_id": 88(可选，卡片字段的原文定位)}
    {"evidence_type": "experiment_run", "experiment_run_id": 7,
                                     "metric_name": "accuracy"(可选), "metric_value": 0.81(可选)}
    {"evidence_type": "experiment_passport", "experiment_passport_id": 3,
                                     "metric_name": "accuracy"(可选)}
    {"evidence_type": "decision",    "decision_log_id": 21}

硬约束（逐条照做，禁止放宽）
----------------------------
1. **定位必须可验证**：``paper_span`` / 带 ``paper_span_id`` 的 ``card_field``
   在入库前一定跑 WP05 的 :func:`verify_span`；``verdict='invalid'``
   （``quote_sha256`` 不匹配）→ **拒绝绑定**。
2. **fulltext_gate**：只有 ``parse_status='ok'`` 且 ``coverage>=0.60`` 的文档
   才允许产出正文级 ``paper_span`` 证据；其余候选被拒并记录原因。
3. **禁止降级为无来源引用**：候选全部被拒时返回空列表（``bind_evidence``）
   或 ``bound=[] + rejected=[…]``（``bind_evidence_detailed``），
   绝不写入"有证据但没有定位"的行；调用方据此丢弃对应的 idea / Claim。
4. **幂等**：同一 owner 下自然键相同的证据不会重复插入（重跑安全）。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.services.evidence.records import (
    EvidenceError,
    EvidenceRow,
    InvalidOwnerTypeError,
    is_evidence_type,
    is_owner_type,
    num,
)
from app.services.evidence.resolver import (
    CARD_FIELDS,
    _gate_payload,
    _load_document,
    _load_span,
    resolve_evidence,
)
from app.services.fulltext import verify_span
from app.services.fulltext.text_cache import TextCache, default_text_cache

logger = logging.getLogger("sciloop.wp13.binder")

#: 允许的候选字段（防止调用方拼错字段名后"静默绑定成功"）
CANDIDATE_FIELDS: frozenset[str] = frozenset(
    {
        "evidence_type",
        "paper_id",
        "paper_span_id",
        "document_version",
        "card_field",
        "experiment_run_id",
        "experiment_passport_id",
        "metric_name",
        "metric_value",
        "decision_log_id",
        "quote_text",
        "weight",
    }
)

#: 各类型必需的定位字段
_REQUIRED_REFS: dict[str, tuple[str, ...]] = {
    "paper_span": ("paper_span_id",),
    "card_field": ("paper_id", "card_field"),
    "experiment_run": ("experiment_run_id",),
    "experiment_passport": ("experiment_passport_id",),
    "decision": ("decision_log_id",),
}


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


def _reject(candidate: Any, code: str, reason: str) -> dict[str, Any]:
    return {"candidate": candidate, "code": code, "reason": reason}


# --------------------------------------------------------------------------- #
# 候选校验
# --------------------------------------------------------------------------- #
async def _validate_candidate(
    candidate: Mapping[str, Any],
    *,
    session: Any,
    text_cache: TextCache,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """校验单条候选；通过返回 ``(values, None)``，被拒返回 ``(None, rejected)``。"""
    if not isinstance(candidate, Mapping):
        return None, _reject(candidate, "invalid_candidate", "候选必须是对象（dict）")

    unknown = set(candidate) - CANDIDATE_FIELDS
    if unknown:
        return None, _reject(
            candidate,
            "unknown_candidate_field",
            f"候选含未知字段 {sorted(unknown)}：拒绝绑定，避免调用方误以为已生效",
        )

    evidence_type = candidate.get("evidence_type")
    if not is_evidence_type(evidence_type):
        return None, _reject(
            candidate,
            "invalid_evidence_type",
            f"evidence_type='{evidence_type}' 不在受控值域内",
        )
    evidence_type = str(evidence_type)

    for field in _REQUIRED_REFS[evidence_type]:
        value = candidate.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return None, _reject(
                candidate, "missing_required_ref", f"{evidence_type} 证据缺少必填字段 {field}"
            )

    values: dict[str, Any] = {
        "evidence_type": evidence_type,
        "paper_id": _as_int(candidate.get("paper_id")),
        "paper_span_id": _as_int(candidate.get("paper_span_id")),
        "card_field": (str(candidate["card_field"]).strip() if candidate.get("card_field") else None),
        "experiment_run_id": _as_int(candidate.get("experiment_run_id")),
        "experiment_passport_id": _as_int(candidate.get("experiment_passport_id")),
        "metric_name": (str(candidate["metric_name"]).strip() if candidate.get("metric_name") else None),
        "metric_value": _as_float(candidate.get("metric_value")),
        "decision_log_id": _as_int(candidate.get("decision_log_id")),
        "quote_text": candidate.get("quote_text"),
        "weight": _as_float(candidate.get("weight")) if candidate.get("weight") is not None else 1.0,
    }

    # ---- 引用存在性 + fulltext_gate + 哈希优先校验 ----
    if evidence_type == "paper_span":
        span = await _load_span(session, values["paper_span_id"])
        if span is None:
            return None, _reject(
                candidate,
                "span_not_found",
                f"paper_spans 中不存在 id={values['paper_span_id']}",
            )
        rejected = await _check_span_candidate(
            candidate, span, session=session, text_cache=text_cache, values=values
        )
        if rejected:
            return None, rejected

    elif evidence_type == "card_field":
        if values["card_field"] not in CARD_FIELDS:
            return None, _reject(
                candidate,
                "invalid_card_field",
                f"card_field='{values['card_field']}' 不属于 8 字段白名单",
            )
        card = await _row(
            session,
            "SELECT id, version FROM paper_cards WHERE paper_id = :paper_id "
            "ORDER BY version DESC, id DESC LIMIT 1",
            paper_id=int(values["paper_id"]),
        )
        if card is None:
            # WP06 未就绪：安全降级为"仅摘要级"绑定（不编造字段值），并记录原因
            values["card_field_only"] = True
            logger.warning(
                "bind_evidence 降级：paper_id=%s 尚无 paper_cards（WP06 未落地），"
                "按摘要级卡片证据绑定 field=%s",
                values["paper_id"],
                values["card_field"],
            )
        if values["paper_span_id"] is not None:
            span = await _load_span(session, values["paper_span_id"])
            if span is None:
                return None, _reject(
                    candidate,
                    "span_not_found",
                    f"card_field 关联的 paper_spans.id={values['paper_span_id']} 不存在",
                )
            rejected = await _check_span_candidate(
                candidate, span, session=session, text_cache=text_cache, values=values
            )
            if rejected:
                return None, rejected

    elif evidence_type == "experiment_run":
        exists = await _row(
            session,
            "SELECT id FROM experiment_runs WHERE id = :id",
            id=int(values["experiment_run_id"]),
        )
        if exists is None:
            return None, _reject(
                candidate,
                "run_not_found",
                f"experiment_runs 中不存在 id={values['experiment_run_id']}",
            )

    elif evidence_type == "experiment_passport":
        exists = await _row(
            session,
            "SELECT id FROM experiment_passports WHERE id = :id",
            id=int(values["experiment_passport_id"]),
        )
        if exists is None:
            return None, _reject(
                candidate,
                "passport_not_found",
                f"experiment_passports 中不存在 id={values['experiment_passport_id']}",
            )

    elif evidence_type == "decision":
        exists = await _row(
            session,
            "SELECT id FROM decision_logs WHERE id = :id",
            id=int(values["decision_log_id"]),
        )
        if exists is None:
            return None, _reject(
                candidate,
                "decision_not_found",
                f"decision_logs 中不存在 id={values['decision_log_id']}",
            )

    return values, None


async def _check_span_candidate(
    candidate: Mapping[str, Any],
    span: Mapping[str, Any],
    *,
    session: Any,
    text_cache: TextCache,
    values: dict[str, Any],
) -> dict[str, Any] | None:
    """``paper_span`` 候选的三道门：paper 归属、fulltext_gate、哈希优先校验。"""
    paper_id = int(span["paper_id"])
    document_version = str(span["document_version"])

    if values.get("paper_id") is not None and int(values["paper_id"]) != paper_id:
        return _reject(
            candidate,
            "paper_mismatch",
            f"候选 paper_id={values['paper_id']} 与 span 实际归属 paper_id={paper_id} 不一致",
        )
    declared_version = candidate.get("document_version")
    if declared_version and str(declared_version) != document_version:
        return _reject(
            candidate,
            "document_version_mismatch",
            f"候选 document_version={declared_version} 与 span 实际 {document_version} 不一致",
        )

    document = await _load_document(session, paper_id, document_version)
    gate = _gate_payload(document)
    if not gate["ok"]:
        return _reject(
            candidate,
            "fulltext_gate_blocked",
            "该文档未通过 fulltext_gate（需 parse_status='ok' 且 coverage>=0.60），"
            f"不允许生成正文级 paper_span 证据：{gate['reason']}",
        )

    verification = verify_span(span, text_cache=text_cache)
    if verification["verdict"] == "invalid":
        return _reject(
            candidate,
            "quote_sha256_mismatch",
            "quote_sha256 不匹配：引用文本与落库不一致，证据不成立（哈希优先于偏移）",
        )

    values["paper_id"] = paper_id
    values["document_version"] = document_version
    values["quote_text"] = span.get("quote_text")
    values["quote_sha256"] = span.get("quote_sha256")
    values["verification_verdict"] = verification["verdict"]
    return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 写库
# --------------------------------------------------------------------------- #
async def _existing_keys(session: Any, owner_type: str, owner_id: int) -> set[tuple[Any, ...]]:
    rows = await _rows(
        session,
        """
        SELECT evidence_type, paper_span_id, card_field, experiment_run_id,
               experiment_passport_id, decision_log_id, metric_name
          FROM evidences
         WHERE owner_type = :owner_type AND owner_id = :owner_id
        """,
        owner_type=owner_type,
        owner_id=int(owner_id),
    )
    return {
        (
            str(row["evidence_type"]),
            _as_int(row["paper_span_id"]),
            row["card_field"],
            _as_int(row["experiment_run_id"]),
            _as_int(row["experiment_passport_id"]),
            _as_int(row["decision_log_id"]),
            row["metric_name"],
        )
        for row in rows
    }


def _natural_key(values: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(values["evidence_type"]),
        _as_int(values.get("paper_span_id")),
        values.get("card_field"),
        _as_int(values.get("experiment_run_id")),
        _as_int(values.get("experiment_passport_id")),
        _as_int(values.get("decision_log_id")),
        values.get("metric_name"),
    )


async def _insert_evidence(
    session: Any, owner_type: str, owner_id: int, values: Mapping[str, Any]
) -> int:
    result = await _maybe_await(
        session.execute(
            text(
                """
                INSERT INTO evidences (
                    owner_type, owner_id, evidence_type, paper_id, paper_span_id,
                    card_field, experiment_run_id, experiment_passport_id,
                    metric_name, metric_value, decision_log_id, quote_text, weight
                ) VALUES (
                    :owner_type, :owner_id, :evidence_type, :paper_id, :paper_span_id,
                    :card_field, :experiment_run_id, :experiment_passport_id,
                    :metric_name, :metric_value, :decision_log_id, :quote_text, :weight
                ) RETURNING id
                """
            ),
            {
                "owner_type": owner_type,
                "owner_id": int(owner_id),
                "evidence_type": values["evidence_type"],
                "paper_id": values.get("paper_id"),
                "paper_span_id": values.get("paper_span_id"),
                "card_field": values.get("card_field"),
                "experiment_run_id": values.get("experiment_run_id"),
                "experiment_passport_id": values.get("experiment_passport_id"),
                "metric_name": values.get("metric_name"),
                "metric_value": values.get("metric_value"),
                "decision_log_id": values.get("decision_log_id"),
                "quote_text": values.get("quote_text"),
                "weight": values.get("weight", 1.0),
            },
        )
    )
    return int(result.scalar_one())


async def _commit(session: Any) -> None:
    await _maybe_await(session.commit())


async def clear_evidence(
    owner_type: str, owner_id: int, *, session: Any, commit: bool = True
) -> int:
    """删除某个 owner 的全部证据（重跑前的清理；返回删除行数）。"""
    if not is_owner_type(owner_type):
        raise InvalidOwnerTypeError(str(owner_type))
    result = await _maybe_await(
        session.execute(
            text("DELETE FROM evidences WHERE owner_type = :owner_type AND owner_id = :owner_id"),
            {"owner_type": owner_type, "owner_id": int(owner_id)},
        )
    )
    if commit:
        await _commit(session)
    count = getattr(result, "rowcount", 0) or 0
    logger.info("clear_evidence owner=%s#%s deleted=%s", owner_type, owner_id, count)
    return int(count)


async def bind_evidence_detailed(
    owner_type: str,
    owner_id: int,
    candidates: Sequence[Mapping[str, Any]] | None,
    *,
    session: Any,
    text_cache: TextCache | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """绑定证据并返回 ``{bound, bound_ids, rejected, warnings, evidence_ids}``。

    - ``replace=True`` 时先清空该 owner 的既有证据（重跑安全）。
    - 任何定位失效 / 闸门不通过的候选都会被拒绝并记录 ``warnings``，
      **绝不写入没有定位依据的证据行**。
    """
    if not is_owner_type(owner_type):
        raise InvalidOwnerTypeError(str(owner_type))
    cache = text_cache if text_cache is not None else default_text_cache()
    warnings: list[str] = []
    rejected: list[dict[str, Any]] = []
    bound_ids: list[int] = []
    bound_rows: list[tuple[int, str]] = []

    if replace:
        await clear_evidence(owner_type, owner_id, session=session)

    existing = await _existing_keys(session, owner_type, owner_id)
    seen: set[tuple[Any, ...]] = set()

    for candidate in candidates or []:
        values, reject = await _validate_candidate(candidate, session=session, text_cache=cache)
        if reject is not None:
            rejected.append(reject)
            warnings.append(f"[{reject['code']}] {reject['reason']}")
            continue
        assert values is not None
        key = _natural_key(values)
        if key in existing or key in seen:
            rejected.append(
                _reject(candidate, "duplicate_evidence", "该证据已绑定（自然键重复），已跳过")
            )
            continue
        evidence_id = await _insert_evidence(session, owner_type, owner_id, values)
        seen.add(key)
        bound_ids.append(evidence_id)
        bound_rows.append((evidence_id, str(values["evidence_type"])))

    if bound_ids:
        await _commit(session)

    bound: list[dict[str, Any]] = []
    for evidence_id, evidence_type in bound_rows:
        try:
            bound.append(
                await resolve_evidence(
                    evidence_type,
                    evidence_id,
                    session=session,
                    id_kind="evidence",
                    text_cache=cache,
                )
            )
        except EvidenceError as exc:  # pragma: no cover - 刚写入的行必然可解析
            warnings.append(f"evidence_id={evidence_id} 解析失败：{exc.message}")

    if not bound and (candidates or []):
        logger.warning(
            "bind_evidence 未绑定任何证据 owner=%s#%s rejected=%d reasons=%s",
            owner_type,
            owner_id,
            len(rejected),
            [item["code"] for item in rejected],
        )
    logger.info(
        "bind_evidence owner=%s#%s bound=%d rejected=%d",
        owner_type,
        owner_id,
        len(bound),
        len(rejected),
    )
    return {
        "owner_type": owner_type,
        "owner_id": int(owner_id),
        "bound": bound,
        "bound_ids": bound_ids,
        "evidence_ids": bound_ids,
        "rejected": rejected,
        "warnings": warnings,
        "bound_at": datetime.now(UTC).isoformat(),
    }


async def bind_evidence(
    owner_type: str,
    owner_id: int,
    candidates: Sequence[Mapping[str, Any]] | None,
    *,
    session: Any,
    text_cache: TextCache | None = None,
    replace: bool = False,
) -> list[dict[str, Any]]:
    """绑定证据，返回**已绑定证据的解析结果列表**（附录 E.3 形状）。

    定位失效 / 闸门不通过的候选返回空（该候选不入库），原因记入结构化日志；
    需要 rejected 明细时改用 :func:`bind_evidence_detailed`。
    """
    detail = await bind_evidence_detailed(
        owner_type,
        owner_id,
        candidates,
        session=session,
        text_cache=text_cache,
        replace=replace,
    )
    return list(detail["bound"])


async def list_evidence(
    owner_type: str,
    owner_id: int,
    *,
    session: Any,
    text_cache: TextCache | None = None,
) -> list[dict[str, Any]]:
    """列出某个 owner 的全部证据（解析为可跳转结构）；坏行以 ``errors`` 形式返回。"""
    if not is_owner_type(owner_type):
        raise InvalidOwnerTypeError(str(owner_type))
    rows = await _rows(
        session,
        """
        SELECT id, owner_type, owner_id, evidence_type, paper_id, paper_span_id,
               card_field, experiment_run_id, experiment_passport_id, metric_name,
               metric_value, decision_log_id, quote_text, weight, created_at
          FROM evidences
         WHERE owner_type = :owner_type AND owner_id = :owner_id
         ORDER BY id
        """,
        owner_type=owner_type,
        owner_id=int(owner_id),
    )
    cache = text_cache if text_cache is not None else default_text_cache()
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row in rows:
        record = EvidenceRow(
            id=int(row["id"]),
            owner_type=str(row["owner_type"]),
            owner_id=int(row["owner_id"]),
            evidence_type=str(row["evidence_type"]),
            paper_id=_as_int(row.get("paper_id")),
            paper_span_id=_as_int(row.get("paper_span_id")),
            card_field=row.get("card_field"),
            experiment_run_id=_as_int(row.get("experiment_run_id")),
            experiment_passport_id=_as_int(row.get("experiment_passport_id")),
            metric_name=row.get("metric_name"),
            metric_value=num(row.get("metric_value")),
            decision_log_id=_as_int(row.get("decision_log_id")),
            quote_text=row.get("quote_text"),
            weight=num(row.get("weight")),
            created_at=row.get("created_at"),
        )
        try:
            items.append(
                await resolve_evidence(
                    record.evidence_type,
                    record.id,
                    session=session,
                    id_kind="evidence",
                    text_cache=cache,
                )
            )
        except EvidenceError as exc:
            errors.append(
                {"evidence_id": record.id, "code": exc.code, "message": exc.message}
            )
    return items


__all__ = [
    "CANDIDATE_FIELDS",
    "bind_evidence",
    "bind_evidence_detailed",
    "clear_evidence",
    "list_evidence",
]
