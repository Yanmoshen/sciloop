# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Claim 级证据校验与落库（WP14-T3，附录 D.5「后置」）。

流程
----
1. **拆分**：优先调用 WP13 的 ``claim_splitter.split_claims``（纯函数、离线可复现）；
   导入失败时使用本文件内的兜底切分器，并在报告中如实标注 ``splitter_source``。
2. **落库**：``claim_splitter.persist_claims`` 写入 ``draft_claims``（全量替换，重跑安全）。
3. **判定**：逐条事实性 Claim 解析草稿中的 ``[n]`` 引用 → 反查证据池 → 调用 WP13
   ``binder.bind_evidence_detailed('draft_claim', claim_id, candidates)`` 做**哈希优先**的
   真实校验；据此写三态：

   ==============  ==========================================================
   supported       引用了池内证据且全部通过校验（并已绑定进 ``evidences``）
   contradicted    引用了证据但校验失败（哈希/偏移/引用对象不存在）——如实暴露冲突
   insufficient    无引用，或引用键不在池内（含模型自造 ref，一律丢弃并留痕）
   ==============  ==========================================================

4. **统计**：``claim_coverage = supported / 事实性 Claim 总数``（WP13 ``claim_coverage`` 口径；
   事实性总数为 0 时为 ``None``，不用 0/1 冒充），并回写 ``paper_drafts``。

无证据的段落**必须**出现在 ``unsupported`` 列表里（供前端高亮），禁止隐藏。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.writing.drafter import (
    CITATION_RE,
    EvidencePool,
    parse_citation_index,
)

logger = logging.getLogger("sciloop.wp14.integrity")

WP_ID = "WP14"

#: 兜底切分器的句终止符
_TERMINATOR_RE = re.compile(r"[。！？!?]+|\.[ \t]*")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+|>\s*)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MIN_FACTUAL_CHARS = 12


@dataclass(slots=True)
class ClaimRecord:
    """一条 Claim 的校验结果（同时用于 API 响应）。"""

    claim_id: int | None
    index: int
    claim_text: str
    is_factual: bool
    section_heading: str | None = None
    support_status: str = "insufficient"
    status_reason: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    dropped_refs: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    char_start: int | None = None
    char_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "index": self.index,
            "section_heading": self.section_heading,
            "claim_text": self.claim_text,
            "is_factual": self.is_factual,
            "support_status": self.support_status,
            "status_reason": self.status_reason,
            "evidence_refs": list(self.evidence_refs),
            "evidence_ids": list(self.evidence_ids),
            "dropped_refs": list(self.dropped_refs),
            "rejected": list(self.rejected),
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


# --------------------------------------------------------------------------------------
# 拆分（WP13 优先，本包兜底）
# --------------------------------------------------------------------------------------
def _fallback_split_claims(content_md: str) -> list[ClaimRecord]:
    """兜底切分器（WP13 不可用时）：与 ``claim_splitter`` 的口径保持最小一致。"""
    records: list[ClaimRecord] = []
    if not content_md:
        return records
    in_fence = False
    heading: str | None = None
    offset = 0
    index = 0
    for raw_line in content_md.split("\n"):
        start = offset
        offset += len(raw_line) + 1
        line = raw_line.replace("\r", "")
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            heading = match.group(1).strip() or heading
            continue
        if not line.strip():
            continue
        body = _PREFIX_RE.sub("", line, count=1)
        body_start = start + (len(line) - len(body))
        cursor = 0
        for terminator in _TERMINATOR_RE.finditer(body):
            segment_start = cursor
            end = terminator.end()
            segment = body[segment_start:end]
            cursor = end
            text_value = _clean(segment)
            if not text_value:
                continue
            is_factual = _looks_factual(text_value)
            records.append(
                ClaimRecord(
                    claim_id=None,
                    index=index,
                    claim_text=text_value,
                    is_factual=is_factual,
                    section_heading=heading,
                    char_start=body_start + segment_start,
                    char_end=body_start + end,
                    status_reason=None if is_factual else "非事实性句：不参与覆盖率统计",
                )
            )
            index += 1
        tail = body[cursor:]
        text_value = _clean(tail)
        if text_value:
            records.append(
                ClaimRecord(
                    claim_id=None,
                    index=index,
                    claim_text=text_value,
                    is_factual=_looks_factual(text_value),
                    section_heading=heading,
                    char_start=body_start + cursor,
                    char_end=body_start + len(body),
                )
            )
            index += 1
    return records


def _clean(text_value: str) -> str:
    value = re.sub(r"[*`_>#]", "", text_value or "")
    return " ".join(value.split())


def _looks_factual(text_value: str) -> bool:
    """兜底事实性判定：含数字/结果措辞即视为事实性（保守但可复现）。"""
    if len(text_value) < _MIN_FACTUAL_CHARS:
        return False
    if re.search(r"\d", text_value):
        return True
    return bool(
        re.search(
            r"(?:提升|提高|降低|下降|达到|超过|优于|相比|表明|显示|证明|说明|发现|"
            r"outperform|improve|reduce|achieve|indicate|show)",
            text_value,
            re.IGNORECASE,
        )
    )


def split_claims_for_draft(content_md: str, *, draft_id: int | None = None) -> tuple[list[ClaimRecord], str]:
    """返回 ``(claims, splitter_source)``；优先 WP13，失败则兜底。"""
    try:
        from app.services.evidence.claim_splitter import split_claims as wp13_split

        claims = wp13_split(content_md, draft_id=draft_id)
        records = [
            ClaimRecord(
                claim_id=None,
                index=int(getattr(claim, "index", position)),
                claim_text=str(getattr(claim, "claim_text", "")),
                is_factual=bool(getattr(claim, "is_factual", False)),
                section_heading=getattr(claim, "section_heading", None),
                status_reason=None if getattr(claim, "is_factual", False) else "非事实性句：不参与覆盖率统计",
                char_start=getattr(claim, "char_start", None),
                char_end=getattr(claim, "char_end", None),
            )
            for position, claim in enumerate(claims)
        ]
        return records, "wp13.claim_splitter"
    except Exception as exc:  # noqa: BLE001 - WP13 未就绪时兜底，禁止静默失败
        logger.warning(
            "WP13 claim_splitter 不可用，使用本包兜底切分器（error=%s: %s）",
            type(exc).__name__,
            exc,
        )
        return _fallback_split_claims(content_md), "wp14.local_fallback"


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
async def verify_and_persist(
    session: Any,
    *,
    draft_id: int,
    content_md: str,
    pool: EvidencePool,
    max_claims: int = 0,
    commit: bool = True,
) -> dict[str, Any]:
    """校验草稿 Claim 并落库，返回完整报告（含 ``unsupported`` 供前端高亮）。"""
    citation_index = parse_citation_index(content_md)
    claims, splitter_source = split_claims_for_draft(content_md, draft_id=draft_id)
    if max_claims and len(claims) > max_claims:
        claims = claims[:max_claims]

    persisted = await _persist_claims(session, draft_id=draft_id, claims=claims)
    records: list[ClaimRecord] = []
    bind_source = "wp13.binder"
    unknown_citation_numbers: list[int] = []
    dropped_total: list[str] = []

    for _position, record in enumerate(claims):
        claim_id = persisted.get(record.index)
        record.claim_id = claim_id
        if not record.is_factual:
            record.support_status = "insufficient"
            record.status_reason = record.status_reason or "非事实性句：不参与覆盖率统计"
            records.append(record)
            continue

        refs, unknown_numbers = _refs_for_claim(record, content_md, citation_index)
        unknown_citation_numbers.extend(unknown_numbers)
        resolved: list[str] = []
        unknown_refs: list[str] = []
        for ref in refs:
            if pool.has(ref):
                resolved.append(ref)
            else:
                unknown_refs.append(ref)
        record.evidence_refs = resolved
        record.dropped_refs = unknown_refs
        dropped_total.extend(unknown_refs)

        if not resolved:
            record.support_status = "insufficient"
            reasons: list[str] = []
            if unknown_refs:
                reasons.append("引用键不在证据池内（已丢弃，禁止编造引用）：" + ", ".join(unknown_refs))
            if unknown_numbers:
                reasons.append("引用编号无法在证据池解析：" + ", ".join(str(n) for n in unknown_numbers))
            if not refs:
                reasons.append("该段未挂载任何证据（无证据必须标记，禁止隐藏）")
            record.status_reason = "；".join(reasons)
            records.append(record)
            continue

        candidates = [entry.candidate() for entry in (pool.resolve(ref) for ref in resolved) if entry is not None]
        bound = await _bind(session, claim_id, candidates)
        bind_source = bound.get("source", bind_source)
        record.evidence_ids = list(bound.get("evidence_ids") or [])
        record.rejected = list(bound.get("rejected") or [])
        if record.evidence_ids:
            record.support_status = "supported"
            record.status_reason = (
                f"引用 {len(resolved)} 条证据且全部通过来源/定位/哈希校验（已绑定 "
                f"evidences={record.evidence_ids}）"
            )
        else:
            rejected_codes = sorted({str(item.get("code")) for item in record.rejected if isinstance(item, Mapping)})
            record.support_status = "contradicted"
            record.status_reason = (
                "引用的证据未通过校验（哈希/定位/对象存在性），引证与实际证据冲突："
                + (", ".join(rejected_codes) if rejected_codes else "全部候选被拒")
            )
        records.append(record)

    factual = [record for record in records if record.is_factual]
    supported = [record for record in factual if record.support_status == "supported"]
    contradicted = [record for record in factual if record.support_status == "contradicted"]
    insufficient = [record for record in factual if record.support_status == "insufficient"]

    await _update_claim_rows(session, records)
    coverage = _coverage(len(supported), len(factual))
    await _update_draft(session, draft_id=draft_id, claim_coverage=coverage)
    if commit:
        await _commit(session)

    unsupported = [
        {
            "claim_id": record.claim_id,
            "index": record.index,
            "section_heading": record.section_heading,
            "claim_text": record.claim_text,
            "char_start": record.char_start,
            "char_end": record.char_end,
            "support_status": record.support_status,
            "reason": record.status_reason,
        }
        for record in records
        if record.is_factual and record.support_status != "supported"
    ]

    report = {
        "draft_id": draft_id,
        "claim_coverage": coverage,
        "counts": {
            "total": len(records),
            "factual": len(factual),
            "supported": len(supported),
            "contradicted": len(contradicted),
            "insufficient": len(insufficient),
            "non_factual": len(records) - len(factual),
        },
        "claims": [record.to_dict() for record in records],
        "unsupported": unsupported,
        "citations": {str(number): ref for number, ref in sorted(citation_index.items())},
        "citation_count": len(citation_index),
        "unknown_citations": unknown_citation_numbers,
        "dropped_refs": dropped_total,
        "splitter_source": splitter_source,
        "binder_source": bind_source,
        "coverage_note": None
        if coverage is not None
        else "无事实性 Claim：覆盖率无定义（不用 0 或 1 冒充）",
    }
    logger.info(
        "verify_and_persist draft_id=%s factual=%d supported=%d contradicted=%d insufficient=%d coverage=%s",
        draft_id,
        len(factual),
        len(supported),
        len(contradicted),
        len(insufficient),
        coverage,
    )
    return report


def _refs_for_claim(
    record: ClaimRecord, content_md: str, citation_index: Mapping[int, str]
) -> tuple[list[str], list[int]]:
    """从 Claim 的原文区间解析 ``[n]``，返回 ``(证据键列表, 无法解析的编号)``。

    渲染口径是「句子。[n]」——引用标记落在句终止符**之后**，因此切片必须向后吞掉
    紧随其后的 ``[n]`` 序列，否则所有 Claim 都会被误判为无证据（insufficient）。
    """
    refs: list[str] = []
    unknown_numbers: list[int] = []
    if record.char_start is None or record.char_end is None:
        return refs, unknown_numbers
    slice_text = content_md[record.char_start : record.char_end]
    if record.char_end is not None:
        trailing = re.match(r"(?:[ \t\u3000]*\[\d{1,3}\])+", content_md[record.char_end :])
        if trailing is not None:
            slice_text = slice_text + trailing.group(0)
    for match in CITATION_RE.finditer(slice_text):
        try:
            number = int(match.group(1))
        except (TypeError, ValueError):  # pragma: no cover
            continue
        ref = citation_index.get(number)
        if ref is None:
            if number not in unknown_numbers:
                unknown_numbers.append(number)
            continue
        if ref not in refs:
            refs.append(ref)
    return refs, unknown_numbers


async def _persist_claims(
    session: Any, *, draft_id: int, claims: Sequence[ClaimRecord]
) -> dict[int, int]:
    """写入 ``draft_claims``，返回 ``{claim.index: claim_id}``。优先复用 WP13 实现。"""
    try:
        from app.services.evidence.claim_splitter import Claim, persist_claims

        objects = [
            Claim(
                index=record.index,
                claim_text=record.claim_text,
                is_factual=record.is_factual,
                section_heading=record.section_heading,
                factual_reason=record.status_reason,
            )
            for record in claims
        ]
        stored = await persist_claims(session, draft_id, objects, commit=False)
        return {
            int(getattr(claim, "index", position)): int(claim.claim_id)
            for position, claim in enumerate(stored)
            if getattr(claim, "claim_id", None) is not None
        }
    except Exception as exc:  # noqa: BLE001 - WP13 未就绪时本包兜底
        logger.warning("WP13 persist_claims 不可用，使用本包兜底写入：%s", exc)
        return await _persist_claims_fallback(session, draft_id=draft_id, claims=claims)


async def _persist_claims_fallback(
    session: Any, *, draft_id: int, claims: Sequence[ClaimRecord]
) -> dict[int, int]:
    from sqlalchemy import text as sql_text

    await session.execute(
        sql_text("DELETE FROM draft_claims WHERE draft_id = :draft_id"), {"draft_id": int(draft_id)}
    )
    mapping: dict[int, int] = {}
    for record in claims:
        result = await session.execute(
            sql_text(
                """
                INSERT INTO draft_claims (
                    draft_id, section_heading, claim_text, is_factual,
                    support_status, status_reason, evidence_count
                ) VALUES (
                    :draft_id, :section_heading, :claim_text, :is_factual,
                    'insufficient', :status_reason, 0
                ) RETURNING id
                """
            ),
            {
                "draft_id": int(draft_id),
                "section_heading": record.section_heading,
                "claim_text": record.claim_text,
                "is_factual": bool(record.is_factual),
                "status_reason": record.status_reason,
            },
        )
        mapping[record.index] = int(result.scalar_one())
    return mapping


async def _bind(session: Any, claim_id: int | None, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """调用 WP13 ``bind_evidence_detailed``（hash-first 真实校验）；失败即如实拒绝。"""
    if claim_id is None or not candidates:
        return {"evidence_ids": [], "rejected": [], "source": "skipped_no_claim_id"}
    try:
        from app.services.evidence.binder import bind_evidence_detailed

        result = await bind_evidence_detailed(
            "draft_claim", int(claim_id), list(candidates), session=session, replace=True
        )
        return {
            "evidence_ids": list(result.get("evidence_ids") or []),
            "rejected": list(result.get("rejected") or []),
            "warnings": list(result.get("warnings") or []),
            "source": "wp13.binder",
        }
    except Exception as exc:  # noqa: BLE001 - WP13 未就绪：标记为 contradicted 而非静默通过
        logger.warning("WP13 binder 不可用（claim_id=%s）：%s", claim_id, exc)
        return {
            "evidence_ids": [],
            "rejected": [{"code": "binder_unavailable", "reason": f"{type(exc).__name__}: {exc}"}],
            "source": "unavailable",
        }


async def _update_claim_rows(session: Any, records: Sequence[ClaimRecord]) -> None:
    from sqlalchemy import text as sql_text

    for record in records:
        if record.claim_id is None:
            continue
        await session.execute(
            sql_text(
                """
                UPDATE draft_claims
                SET support_status = :status,
                    status_reason = :reason,
                    evidence_count = :count
                WHERE id = :claim_id
                """
            ),
            {
                "status": record.support_status,
                "reason": record.status_reason,
                "count": len(record.evidence_ids),
                "claim_id": int(record.claim_id),
            },
        )


async def _update_draft(session: Any, *, draft_id: int, claim_coverage: float | None) -> None:
    from sqlalchemy import text as sql_text

    await session.execute(
        sql_text("UPDATE paper_drafts SET claim_coverage = :coverage WHERE id = :draft_id"),
        {"coverage": claim_coverage, "draft_id": int(draft_id)},
    )


def _coverage(supported: int, factual_total: int) -> float | None:
    """优先复用 WP13 的 ``records.claim_coverage``（口径唯一）。"""
    try:
        from app.services.evidence.records import claim_coverage as wp13_coverage

        return wp13_coverage(supported, factual_total)
    except Exception:  # noqa: BLE001 - 兜底同口径
        if factual_total <= 0:
            return None
        return round(max(0.0, min(1.0, supported / float(factual_total))), 3)


async def _commit(session: Any) -> None:
    import inspect

    value = session.commit()
    if inspect.isawaitable(value):
        await value


__all__ = [
    "ClaimRecord",
    "split_claims_for_draft",
    "verify_and_persist",
]
