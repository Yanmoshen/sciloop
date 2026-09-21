# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""证据链的纯数据结构与受控值域（WP13，附录 A.3 / A.6 / E.3）。

本模块**不含 ORM、不含网络、不含 IO**，因此可以脱离数据库与 FastAPI
单独做单元测试。所有对外函数签名与返回结构以本文件与 ``resolver.py``
的 docstring 为准，一经发布不得中途修改（WP08 / WP14 依赖）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# --------------------------------------------------------------------------- #
# 受控值域（contracts.evidence_rules / 附录 A.3）
# --------------------------------------------------------------------------- #
#: ``evidences.evidence_type``
EVIDENCE_TYPES: tuple[str, ...] = (
    "paper_span",
    "card_field",
    "experiment_run",
    "experiment_passport",
    "decision",
)
#: ``evidences.owner_type``
OWNER_TYPES: tuple[str, ...] = (
    "idea",
    "draft_claim",
    "decision",
    "feasibility",
    "plan_review",
)
#: ``verify_span`` 三 verdict（哈希优先）
VERDICTS: tuple[str, ...] = ("valid", "valid_by_hash", "invalid")
#: ``draft_claims.support_status``
CLAIM_STATUSES: tuple[str, ...] = ("supported", "contradicted", "insufficient")

#: 覆盖率保留位数（``paper_drafts.claim_coverage`` 为 NUMERIC(4,3)）
COVERAGE_SCALE = 3


def is_evidence_type(value: str | None) -> bool:
    return bool(value) and str(value) in EVIDENCE_TYPES


def is_owner_type(value: str | None) -> bool:
    return bool(value) and str(value) in OWNER_TYPES


def is_claim_status(value: str | None) -> bool:
    return bool(value) and str(value) in CLAIM_STATUSES


def _field(source: Any, name: str, default: Any = None) -> Any:
    """同时兼容 dataclass 与 Mapping。"""
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #
class EvidenceError(Exception):
    """证据服务的领域错误（路由层据此映射 HTTP 状态码）。"""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class EvidenceNotFoundError(EvidenceError):
    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__("evidence_not_found", message, detail)


class InvalidEvidenceTypeError(EvidenceError):
    def __init__(self, value: str) -> None:
        super().__init__(
            "invalid_evidence_type",
            f"evidence_type='{value}' 不在受控值域内",
            {"allowed": list(EVIDENCE_TYPES)},
        )


class InvalidOwnerTypeError(EvidenceError):
    def __init__(self, value: str) -> None:
        super().__init__(
            "invalid_owner_type",
            f"owner_type='{value}' 不在受控值域内",
            {"allowed": list(OWNER_TYPES)},
        )


# --------------------------------------------------------------------------- #
# evidences 一行的纯数据视图（附录 A.3，多态表）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EvidenceRow:
    """``evidences`` 一行的数据视图；各类型外键均可为空。"""

    id: int
    owner_type: str
    owner_id: int
    evidence_type: str
    paper_id: int | None = None
    paper_span_id: int | None = None
    card_field: str | None = None
    experiment_run_id: int | None = None
    experiment_passport_id: int | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    decision_log_id: int | None = None
    quote_text: str | None = None
    weight: float | None = None
    created_at: Any | None = None

    @property
    def natural_key(self) -> tuple[Any, ...]:
        """用于绑定去重的自然键（同类型内唯一）。"""
        return (
            self.evidence_type,
            self.paper_span_id,
            self.card_field,
            self.experiment_run_id,
            self.experiment_passport_id,
            self.decision_log_id,
            self.metric_name,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "evidence_id": self.id,
            "owner_type": self.owner_type,
            "owner_id": self.owner_id,
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
            "weight": self.weight,
        }
        if self.created_at is not None:
            payload["created_at"] = (
                self.created_at.isoformat() if hasattr(self.created_at, "isoformat") else self.created_at
            )
        return payload


# --------------------------------------------------------------------------- #
# Claim
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Claim:
    """一条原子 Claim（对应 ``draft_claims`` 一行 + 派生的定位信息）。

    ``char_start`` / ``char_end`` 指向 **content_md 原文**中的该句区间
    （含 markdown 标记），``claim_text`` 是可读形式（已去除 markdown 装饰）。
    ``draft_claims`` 表没有偏移列，因此这两个字段只在 API 响应中返回，
    不落库（禁止为此改表结构）。
    """

    index: int
    claim_text: str
    is_factual: bool
    section_heading: str | None = None
    factual_reason: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    line_no: int | None = None
    claim_id: int | None = None
    support_status: str = "insufficient"
    status_reason: str | None = None
    evidence_count: int = 0
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "index": self.index,
            "section_heading": self.section_heading,
            "claim_text": self.claim_text,
            "is_factual": self.is_factual,
            "factual_reason": self.factual_reason,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "line_no": self.line_no,
            "support_status": self.support_status,
            "status_reason": self.status_reason,
            "evidence_count": self.evidence_count,
            "evidence_refs": list(self.evidence_refs),
        }


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def claim_coverage(supported: int, factual_total: int) -> float | None:
    """``claim_coverage = supported Claim 数 ÷ 事实性 Claim 总数``。

    事实性 Claim 数为 0 时除法无定义 → 返回 ``None`` 并如实披露，
    **禁止用 0.0 或 1.0 冒充**。
    """
    if factual_total is None or int(factual_total) <= 0:
        return None
    supported_count = max(0, int(supported))
    ratio = supported_count / float(factual_total)
    if ratio > 1.0:  # 统计口径 bug 时钳制，不产生 >1 的非法值
        ratio = 1.0
    return float(
        Decimal(str(ratio)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    )


def status_counts(statuses: Iterable[str]) -> dict[str, int]:
    """三态计数（缺失状态归入 ``insufficient``，绝不留空）。"""
    counts = dict.fromkeys(CLAIM_STATUSES, 0)
    for status in statuses:
        key = str(status) if is_claim_status(status) else "insufficient"
        counts[key] += 1
    return counts


def num(value: Any) -> float | None:
    """``Decimal`` / ``int`` / ``float`` → ``float``；``None`` 原样返回。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - 列类型受控
        return None


def require_field(value: Any, name: str, code: str = "invalid_candidate") -> Any:
    """候选/入参必填字段校验（返回原值，缺失抛 :class:`EvidenceError`）。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise EvidenceError(code, f"缺少必填字段 {name}", {"field": name})
    return value


__all__ = [
    "CLAIM_STATUSES",
    "COVERAGE_SCALE",
    "EVIDENCE_TYPES",
    "OWNER_TYPES",
    "VERDICTS",
    "Claim",
    "EvidenceError",
    "EvidenceNotFoundError",
    "EvidenceRow",
    "InvalidEvidenceTypeError",
    "InvalidOwnerTypeError",
    "claim_coverage",
    "is_claim_status",
    "is_evidence_type",
    "is_owner_type",
    "num",
    "require_field",
    "status_counts",
]
