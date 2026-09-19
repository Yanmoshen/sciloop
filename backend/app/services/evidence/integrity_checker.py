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
"""Claim 级证据判定（WP13-T4，contracts.evidence_rules.claim_rule）。

对外稳定签名（一经发布不得中途修改；WP14 的 writing 环节、WP15 的草稿阅读器依赖）::

    report = await verify_claims(draft_id, session=session)                 # 落库版
    report = await verify_content(content_md, session=session, draft_id=7)  # 调试版（不落库）
    index  = parse_citation_index(content_md)   # {引用编号: 证据键}
    ref_info = parse_evidence_ref("paper_span:8821")

三态判定口径（**事实性 Claim 必给状态，不得留空**）
--------------------------------------------------
=================  ==============================================================
``supported``      至少 1 条引用证据通过 ``binder`` 的来源/定位/哈希校验并成功绑定，
                   且本轮冲突判定未报出冲突证据
``contradicted``   ① 冲突判定（可注入 LLM）报出冲突证据 id；或
                   ② 该 Claim 引用了证据池内的键，但候选**全部被拒**——引证与真实
                      证据冲突（哈希未命中 / fulltext_gate 不通过 / 对象不存在），
                      ``status_reason`` 里带被拒代码，绝不静默降级
``insufficient``   无引用、引用键不在池内（模型自造 ref 一律丢弃并留痕）、
                   定位失效且无任何候选可校验
=================  ==============================================================

``claim_coverage = supported Claim 数 ÷ 事实性 Claim 总数``（``records.claim_coverage``
为唯一口径实现；事实性总数为 0 时返回 ``None``，禁止用 0 或 1 冒充）。

引用编号 → 证据键
-----------------
草稿渲染时（WP14 ``drafter.DraftDocument.render_markdown``）会在文末写入
``## 引用索引（证据池映射）``，形如 ``[1] paper_span:8821 | …``。本模块直接解析
该索引，因此 ``verify_claims`` **不依赖 WP14 的进程内证据池**，可脱离写作环节重跑校验
（这正是 P0-10「可审计重放」需要的性质）。调用方也可显式传入 ``citation_map`` 覆盖。

容忍草稿不存在
--------------
``paper_drafts`` 行由 WP14 并行产出。行不存在时 ``verify_claims`` 返回
``draft_found=false`` 与 ``warnings``（**不抛异常**）；需要校验纯文本时改用
:func:`verify_content`，它只做内存校验并明示「未落库」。
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.services.evidence.binder import (
    _as_int,
    _validate_candidate,
    bind_evidence_detailed,
)
from app.services.evidence.claim_splitter import split_claims
from app.services.evidence.records import (
    CLAIM_STATUSES,
    Claim,
    is_claim_status,
)
from app.services.evidence.records import (
    claim_coverage as _coverage_ratio,
)
from app.services.fulltext.text_cache import TextCache, default_text_cache

logger = logging.getLogger("sciloop.wp13.integrity")

#: 引用角标（与 WP14 ``drafter.CITATION_RE`` 同口径）
CITATION_RE = re.compile(r"\[(\d{1,3})\]")
#: 句末标点之后紧跟的连续引用角标串（``[1][2]``）
_TRAILING_CITATIONS_RE = re.compile(r"^(?:\s*\[\d{1,3}\])+")
#: 引用角标串之后的窗口长度（够容纳 ``[1][2][3]`` 与少量空白）
_TRAILING_WINDOW = 48
#: ``## 引用索引（证据池映射）`` 小节的标题识别
REFERENCE_HEADING_RE = re.compile(r"引用索引|证据池映射|references", re.IGNORECASE)
#: 索引行：``[1] paper_span:8821 | 对应证据见证据池``
REFERENCE_ROW_RE = re.compile(r"^\s*\[(\d{1,3})\]\s+(\S+)")
#: 证据键格式：``<kind>:<id>[:<extra>]``
REF_KINDS: tuple[str, ...] = (
    "paper_span",
    "card_field",
    "experiment_run",
    "experiment_metric",
    "experiment_passport",
    "decision",
)

#: 冲突判定器（可注入）：返回 ``{"conflicting_evidence_ids": [...], "reason": str}``
ConflictDetector = Callable[..., Awaitable[Mapping[str, Any]]]


# --------------------------------------------------------------------------------------
# 会话/HTTP 无关的小工具
# --------------------------------------------------------------------------------------
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


async def _commit(session: Any) -> None:
    await _maybe_await(session.commit())


def _snippet(value: Any, limit: int = 160) -> str | None:
    if value is None:
        return None
    flat = " ".join(str(value).split())
    return flat[:limit] if flat else None


# --------------------------------------------------------------------------------------
# 证据键解析（纯函数 + 需要查库的一种）
# --------------------------------------------------------------------------------------
def parse_evidence_ref(ref: str | None) -> dict[str, Any] | None:
    """把证据池键解析成 ``binder`` 的候选字段（纯函数，不查库）。

    支持：``paper_span:<span_id>`` / ``card_field:<paper_id>:<field>`` /
    ``experiment_run:<run_id>[:<metric_name>]`` / ``experiment_metric:<metric_id>`` /
    ``experiment_passport:<passport_id>`` / ``decision:<log_id>``。

    ``experiment_metric`` 需要查库补 ``experiment_run_id``，此处返回带
    ``"__metric_id__"`` 标记的中间结构，由 :func:`ref_to_candidate` 补齐。
    无法识别的键返回 ``None``（调用方必须丢弃并留痕，禁止编造引用）。
    """
    if not ref or not isinstance(ref, str):
        return None
    parts = [piece.strip() for piece in ref.strip().split(":")]
    kind = parts[0]
    if kind not in REF_KINDS:
        return None

    def _int(value: str) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    if kind == "paper_span":
        if len(parts) < 2 or _int(parts[1]) is None:
            return None
        # 兼容 ``paper_span:<paper_id>:<span_id>`` 冗余写法：取最后一段为 span_id
        span_id = _int(parts[-1])
        return {"evidence_type": "paper_span", "paper_span_id": span_id}

    if kind == "card_field":
        if len(parts) < 3 or _int(parts[1]) is None:
            return None
        return {
            "evidence_type": "card_field",
            "paper_id": _int(parts[1]),
            "card_field": parts[2],
        }

    if kind == "experiment_run":
        if len(parts) < 2 or _int(parts[1]) is None:
            return None
        payload: dict[str, Any] = {
            "evidence_type": "experiment_run",
            "experiment_run_id": _int(parts[1]),
        }
        if len(parts) >= 3 and parts[2]:
            payload["metric_name"] = parts[2]
        return payload

    if kind == "experiment_metric":
        if len(parts) < 2 or _int(parts[1]) is None:
            return None
        return {"evidence_type": "experiment_run", "__metric_id__": _int(parts[1])}

    if kind == "experiment_passport":
        if len(parts) < 2 or _int(parts[1]) is None:
            return None
        return {
            "evidence_type": "experiment_passport",
            "experiment_passport_id": _int(parts[1]),
        }

    if len(parts) < 2 or _int(parts[1]) is None:
        return None
    return {"evidence_type": "decision", "decision_log_id": _int(parts[1])}


async def ref_to_candidate(ref: str | None, *, session: Any) -> dict[str, Any] | None:
    """:func:`parse_evidence_ref` 的查库版（仅 ``experiment_metric`` 需要查库）。"""
    candidate = parse_evidence_ref(ref)
    if candidate is None:
        return None
    metric_id = candidate.pop("__metric_id__", None)
    if metric_id is None:
        return candidate
    metric = await _row(
        session,
        "SELECT experiment_run_id, metric_name FROM experiment_metrics WHERE id = :id",
        id=int(metric_id),
    )
    if metric is None:
        logger.warning("证据键 %s 指向的 experiment_metrics 行不存在，已丢弃", ref)
        return None
    candidate["experiment_run_id"] = int(metric["experiment_run_id"])
    if metric.get("metric_name"):
        candidate["metric_name"] = str(metric["metric_name"])
    return candidate


# --------------------------------------------------------------------------------------
# 引用索引解析
# --------------------------------------------------------------------------------------
def parse_citation_index(content_md: str | None) -> dict[int, str]:
    """解析草稿文末的 ``## 引用索引（证据池映射）``，返回 ``{编号: 证据键}``。

    优先只在索引小节内扫描；小节的标题不存在时退回全文扫描（只要行形如
    ``[n] <kind>:<id>``）。解析出的键一律经 :func:`parse_evidence_ref` 白名单校验，
    格式非法的行被忽略——**宁可少认，不编造引用**。
    """
    index: dict[int, str] = {}
    if not content_md:
        return index

    sans_section: dict[int, str] = {}
    in_section = False
    has_section = bool(REFERENCE_HEADING_RE.search(str(content_md)))

    for raw_line in str(content_md).split("\n"):
        line = raw_line.replace("\r", "").rstrip()
        if line.lstrip().startswith("#"):
            title = line.lstrip("#").strip()
            in_section = bool(REFERENCE_HEADING_RE.search(title))
            continue
        match = REFERENCE_ROW_RE.match(line)
        if not match:
            continue
        ref = match.group(2).strip().rstrip("|").strip()
        if parse_evidence_ref(ref) is None:
            continue
        number = int(match.group(1))
        sans_section.setdefault(number, ref)
        if in_section:
            index.setdefault(number, ref)

    return index or ({} if has_section else sans_section)


def _citations_in_span(content_md: str, claim: Claim) -> list[int]:
    """取出该 Claim 对应的引用编号（保持出现顺序、去重）。

    渲染器把角标写在**句末标点之后**（``text[1]``），而 Claim 的
    ``char_end`` 停在句末标点上，因此必须把紧随其后的
    ``[1][2]…`` 角标串一并纳入，否则每条引用都取不到编号。
    """
    if claim.char_start is None or claim.char_end is None:
        return []
    slice_text = content_md[claim.char_start : claim.char_end]
    tail = content_md[claim.char_end : claim.char_end + _TRAILING_WINDOW]
    trailing = _TRAILING_CITATIONS_RE.match(tail)
    if trailing:
        slice_text += trailing.group(0)
    numbers: list[int] = []
    for match in CITATION_RE.finditer(slice_text):
        number = int(match.group(1))
        if number not in numbers:
            numbers.append(number)
    return numbers


# --------------------------------------------------------------------------------------
# 草稿装载
# --------------------------------------------------------------------------------------
async def load_draft(session: Any, draft_id: int) -> Mapping[str, Any] | None:
    """装载 ``paper_drafts`` 一行；不存在返回 ``None``（**不抛异常**，WP14 并行中）。"""
    return await _row(
        session,
        """
        SELECT id, project_id, pipeline_run_id, iteration, content_md, claim_coverage, created_at
          FROM paper_drafts
         WHERE id = :id
        """,
        id=int(draft_id),
    )


# --------------------------------------------------------------------------------------
# 单条 Claim 的判定
# --------------------------------------------------------------------------------------
async def _evaluate_claim(
    claim: Claim,
    *,
    session: Any,
    content_md: str,
    citation_index: Mapping[int, str],
    candidates_by_claim: Mapping[int, Sequence[Mapping[str, Any]]] | None,
    persist_bindings: bool,
    text_cache: TextCache,
    conflict_detector: ConflictDetector | None,
) -> Claim:
    """判定一条事实性 Claim 的三态，就地写回 ``claim`` 并返回。"""
    numbers = _citations_in_span(content_md, claim)
    refs: list[str] = []
    unknown_citations: list[int] = []
    for number in numbers:
        ref = citation_index.get(number)
        if ref is None:
            unknown_citations.append(number)
            continue
        if ref not in refs:
            refs.append(ref)

    injected = (candidates_by_claim or {}).get(claim.index) or (
        (candidates_by_claim or {}).get(claim.claim_id) if claim.claim_id is not None else None
    )

    candidates: list[dict[str, Any]] = []
    dropped_refs: list[str] = []
    if injected:
        candidates.extend(dict(item) for item in injected)
    else:
        for ref in refs:
            candidate = await ref_to_candidate(ref, session=session)
            if candidate is None:
                dropped_refs.append(ref)
            else:
                candidates.append(candidate)

    rejected: list[dict[str, Any]] = []
    validated_ok: list[dict[str, Any]] = []
    bound_ids: list[int] = []

    if not candidates:
        claim.support_status = "insufficient"
        reasons: list[str] = []
        if unknown_citations:
            reasons.append(
                "引用编号无法在草稿引用索引中解析（已丢弃，禁止编造引用）："
                + ", ".join(str(value) for value in unknown_citations)
            )
        if dropped_refs:
            reasons.append("引用键不在证据池内或格式非法（已丢弃）：" + ", ".join(dropped_refs))
        if not numbers:
            reasons.append("该事实性 Claim 未挂载任何证据（无证据必须如实标记，禁止隐藏）")
        claim.status_reason = "；".join(reasons)
        claim.evidence_count = 0
        return claim

    if persist_bindings and claim.claim_id is not None:
        result = await bind_evidence_detailed(
            "draft_claim",
            int(claim.claim_id),
            candidates,
            session=session,
            text_cache=text_cache,
            replace=True,
        )
        bound_ids = [int(value) for value in (result.get("evidence_ids") or [])]
        rejected = list(result.get("rejected") or [])
        claim.evidence_refs = list(result.get("bound") or [])
    else:
        # 未落库（草稿不存在 / 显式调试）：只做只读校验，明确不写 evidences
        for candidate in candidates:
            values, reject = await _validate_candidate(
                candidate, session=session, text_cache=text_cache
            )
            if reject is not None:
                rejected.append(reject)
            elif values is not None:
                validated_ok.append(values)

    # ---- 冲突判定（可注入 LLM；未注入时按规则） ----
    conflict_ids: list[int] = []
    conflict_reason: str | None = None
    if conflict_detector is not None:
        try:
            verdict = await conflict_detector(
                claim=claim,
                claim_text=claim.claim_text,
                refs=refs,
                bound_ids=bound_ids,
                session=session,
            )
            if isinstance(verdict, Mapping):
                conflict_ids = [
                    int(value)
                    for value in (verdict.get("conflicting_evidence_ids") or [])
                    if _as_int(value) is not None
                ]
                conflict_reason = verdict.get("reason")
        except Exception as exc:  # noqa: BLE001 - 冲突判定失败不掩盖主判定
            logger.warning("冲突判定器失败（claim_index=%s）：%s", claim.index, exc)
            conflict_reason = f"冲突判定器不可用（{type(exc).__name__}: {exc}），本轮只按规则判定"

    rejected_codes = sorted(
        {str(item.get("code")) for item in rejected if isinstance(item, Mapping)}
    )

    if conflict_ids:
        claim.support_status = "contradicted"
        claim.status_reason = (
            f"冲突判定报出 {len(conflict_ids)} 条与 Claim 冲突的证据（evidence_id="
            + ", ".join(str(value) for value in conflict_ids)
            + "）："
            + (str(conflict_reason) if conflict_reason else "需人工复核")
        )
    elif bound_ids or validated_ok:
        claim.support_status = "supported"
        if bound_ids:
            claim.status_reason = (
                f"引用 {len(refs)} 条证据且全部通过来源/定位/哈希校验（已绑定 evidences="
                + ", ".join(str(value) for value in bound_ids)
                + "）"
            )
        else:
            claim.status_reason = (
                f"引用 {len(refs)} 条证据且全部通过来源/定位/哈希校验"
                "（草稿未落库，本结果未写入 evidences，仅供参考）"
            )
    elif rejected:
        claim.support_status = "contradicted"
        claim.status_reason = (
            "引用的证据未通过来源/定位/哈希校验，引证与实际证据冲突（哈希优先于偏移）："
            + (", ".join(rejected_codes) if rejected_codes else "全部候选被拒")
        )
    else:
        claim.support_status = "insufficient"
        claim.status_reason = "无有效证据可判定"

    claim.evidence_count = len(bound_ids) or len(validated_ok)
    if rejected_codes:
        claim.status_reason = (claim.status_reason or "") + f"；被拒代码：{', '.join(rejected_codes)}"
    if not is_claim_status(claim.support_status):  # pragma: no cover - 受控值域兜底
        claim.support_status = "insufficient"
    return claim


# --------------------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------------------
async def _run(
    *,
    session: Any,
    draft_id: int | None,
    content_md: str,
    draft_found: bool,
    persist: bool,
    commit: bool,
    citation_map: Mapping[int, str] | None,
    candidates_by_claim: Mapping[int, Sequence[Mapping[str, Any]]] | None,
    conflict_detector: ConflictDetector | None,
    max_claims: int,
    text_cache: TextCache | None,
    warnings: list[str],
) -> dict[str, Any]:
    cache = text_cache if text_cache is not None else default_text_cache()

    if citation_map is not None:
        citation_index = {int(key): str(value) for key, value in citation_map.items()}
        citation_source = "provided"
    else:
        citation_index = parse_citation_index(content_md)
        citation_source = "content_md_reference_index" if citation_index else "none"

    claims = split_claims(content_md, draft_id=draft_id, max_claims=max_claims)
    factual = [claim for claim in claims if claim.is_factual]

    if persist and draft_found and draft_id is not None:
        from app.services.evidence.claim_splitter import persist_claims

        await persist_claims(session, int(draft_id), claims, commit=False)
    elif persist and not draft_found:
        warnings.append(
            f"paper_drafts 中不存在 id={draft_id}（WP14 尚未产出）：本次只做**只读校验**，"
            "未写入 draft_claims / evidences，也未回写 claim_coverage"
        )

    persist_bindings = bool(persist and draft_found and draft_id is not None)

    for claim in factual:
        await _evaluate_claim(
            claim,
            session=session,
            content_md=content_md,
            citation_index=citation_index,
            candidates_by_claim=candidates_by_claim,
            persist_bindings=persist_bindings,
            text_cache=cache,
            conflict_detector=conflict_detector,
        )

    non_factual_status = "insufficient"
    for claim in claims:
        if claim.is_factual:
            continue
        claim.support_status = non_factual_status
        claim.status_reason = claim.status_reason or claim.factual_reason or "非事实性句：不参与覆盖率统计"

    if persist_bindings:
        # 先清孤儿：persist_claims 是全量替换（旧 claim 行被删），旧 claim 上绑定的
        # evidences 会变成指向已删除 claim_id 的孤儿行——那会让证据链审计出现
        # "查不到归属"的噪声记录，且与本轮重跑的结论不一致，必须一并清除。
        orphans = await _purge_orphan_claim_evidence(session, warnings)
        if orphans:
            logger.info("verify_claims 清理孤儿证据 %d 行（owner_id 已不在 draft_claims）", orphans)
        await _write_back(
            session, draft_id=int(draft_id), claims=claims, warnings=warnings
        )

    supported = [claim for claim in factual if claim.support_status == "supported"]
    contradicted = [claim for claim in factual if claim.support_status == "contradicted"]
    insufficient = [claim for claim in factual if claim.support_status == "insufficient"]
    coverage = _coverage_ratio(len(supported), len(factual))

    if persist_bindings and commit:
        await _commit(session)

    unsupported_spans = [
        {
            "claim_id": claim.claim_id,
            "index": claim.index,
            "section_heading": claim.section_heading,
            "claim_text": claim.claim_text,
            "char_start": claim.char_start,
            "char_end": claim.char_end,
            "line_no": claim.line_no,
            "support_status": claim.support_status,
            "status_reason": claim.status_reason,
            "evidence_count": claim.evidence_count,
        }
        for claim in factual
        if claim.support_status != "supported"
    ]

    if not citation_index and factual:
        warnings.append(
            "草稿未包含可解析的引用索引（`## 引用索引（证据池映射）`）："
            "所有事实性 Claim 按无证据处理，标 insufficient"
        )

    report: dict[str, Any] = {
        "draft_id": int(draft_id) if draft_id is not None else None,
        "draft_found": bool(draft_found),
        "persisted": persist_bindings,
        "claim_coverage": coverage,
        "counts": {
            "total": len(claims),
            "factual": len(factual),
            "non_factual": len(claims) - len(factual),
            "supported": len(supported),
            "contradicted": len(contradicted),
            "insufficient": len(insufficient),
        },
        "claims": [claim.to_dict() for claim in claims],
        "unsupported_spans": unsupported_spans,
        "unsupported": unsupported_spans,
        "citation_map": {str(key): value for key, value in sorted(citation_index.items())},
        "citation_count": len(citation_index),
        "citation_source": citation_source,
        "conflict_detection": "injected" if conflict_detector is not None else "rule_only",
        "status_values": list(CLAIM_STATUSES),
        "coverage_note": (
            None
            if coverage is not None
            else "无事实性 Claim：claim_coverage 无定义（不用 0 或 1 冒充）"
        ),
        "warnings": warnings,
        "verified_at": datetime.now(UTC).isoformat(),
    }
    logger.info(
        "verify_claims draft_id=%s found=%s persisted=%s factual=%d supported=%d "
        "contradicted=%d insufficient=%d coverage=%s",
        draft_id,
        draft_found,
        persist_bindings,
        len(factual),
        len(supported),
        len(contradicted),
        len(insufficient),
        coverage,
    )
    return report


async def _purge_orphan_claim_evidence(session: Any, warnings: list[str]) -> int:
    """删除 ``owner_id`` 已不在 ``draft_claims`` 的 ``draft_claim`` 证据行。

    定义严格：只清「owner_type='draft_claim' 但其 owner_id 不是任何现存 Claim」的行，
    因此不会误删正在写入的 Claim 证据（并发写时那些 claim 行一定已存在）。
    """
    result = await _maybe_await(
        session.execute(
            text(
                """
                DELETE FROM evidences
                 WHERE owner_type = 'draft_claim'
                   AND owner_id NOT IN (SELECT id FROM draft_claims)
                """
            )
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def _write_back(
    session: Any, *, draft_id: int, claims: Sequence[Claim], warnings: list[str]
) -> None:
    """回写 ``draft_claims``（三态/理由/证据数）与 ``paper_drafts.claim_coverage``。"""
    for claim in claims:
        if claim.claim_id is None:
            continue
        await _maybe_await(
            session.execute(
                text(
                    """
                    UPDATE draft_claims
                       SET support_status = :status,
                           status_reason = :reason,
                           evidence_count = :count
                     WHERE id = :claim_id
                    """
                ),
                {
                    "status": claim.support_status,
                    "reason": claim.status_reason,
                    "count": int(claim.evidence_count),
                    "claim_id": int(claim.claim_id),
                },
            )
        )
    coverage = _coverage_ratio(
        sum(1 for claim in claims if claim.is_factual and claim.support_status == "supported"),
        sum(1 for claim in claims if claim.is_factual),
    )
    result = await _maybe_await(
        session.execute(
            text("UPDATE paper_drafts SET claim_coverage = :coverage WHERE id = :draft_id"),
            {"coverage": coverage, "draft_id": int(draft_id)},
        )
    )
    if getattr(result, "rowcount", 1) == 0:
        warnings.append(f"回写 claim_coverage 时未命中 paper_drafts.id={draft_id}")


async def verify_claims(
    draft_id: int,
    *,
    session: Any,
    content_md: str | None = None,
    citation_map: Mapping[int, str] | None = None,
    candidates_by_claim: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
    conflict_detector: ConflictDetector | None = None,
    persist: bool = True,
    commit: bool = True,
    max_claims: int = 0,
    text_cache: TextCache | None = None,
) -> dict[str, Any]:
    """重跑某草稿的 Claim 三态判定，返回完整报告（含 ``unsupported_spans``）。

    :param draft_id: ``paper_drafts.id``；行不存在时返回 ``draft_found=false`` 并给
        ``warnings``（**不抛异常**，容忍 WP14 并行未就绪）
    :param content_md: 显式正文；``None`` 时读 ``paper_drafts.content_md``
    :param citation_map: 覆盖草稿内索引的 ``{编号: 证据键}``（例如写作环节的进程内池子）
    :param candidates_by_claim: 按 ``claim.index`` 或 ``claim_id`` 直接给定候选，
        绕过引用键解析（调试与 WP14 复用同一实现时使用）
    :param conflict_detector: 冲突判定器（可接 WP02 的 ``app.llm.chat_json``）；
        需返回 ``{"conflicting_evidence_ids": [...], "reason": str}``
    :param persist: ``False`` 时只校验不落库
    """
    draft = await load_draft(session, int(draft_id))
    warnings: list[str] = []
    if draft is None:
        if content_md is None:
            logger.warning("verify_claims 未命中草稿 draft_id=%s 且未提供 content_md", draft_id)
            return {
                "draft_id": int(draft_id),
                "draft_found": False,
                "persisted": False,
                "claim_coverage": None,
                "counts": {"total": 0, "factual": 0, "non_factual": 0, "supported": 0,
                           "contradicted": 0, "insufficient": 0},
                "claims": [],
                "unsupported_spans": [],
                "unsupported": [],
                "citation_map": {},
                "citation_count": 0,
                "citation_source": "none",
                "conflict_detection": "not_run",
                "status_values": list(CLAIM_STATUSES),
                "coverage_note": "草稿不存在：无 Claim 可统计",
                "warnings": [
                    f"paper_drafts 中不存在 id={draft_id}（WP14 尚未产出）。"
                    "如需校验纯文本，请改用 POST /drafts/{id}/verify-evidence 的 content_md 调试入口"
                ],
                "verified_at": datetime.now(UTC).isoformat(),
            }
        warnings.append(
            f"paper_drafts 中不存在 id={draft_id}（WP14 尚未产出）：使用调用方提供的 content_md，"
            "只做只读校验"
        )
        body = str(content_md)
    else:
        body = str(content_md) if content_md is not None else str(draft.get("content_md") or "")

    return await _run(
        session=session,
        draft_id=int(draft_id),
        content_md=body,
        draft_found=draft is not None,
        persist=persist,
        commit=commit,
        citation_map=citation_map,
        candidates_by_claim=candidates_by_claim,
        conflict_detector=conflict_detector,
        max_claims=max_claims,
        text_cache=text_cache,
        warnings=warnings,
    )


async def verify_content(
    content_md: str,
    *,
    session: Any,
    draft_id: int | None = None,
    citation_map: Mapping[int, str] | None = None,
    candidates_by_claim: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
    conflict_detector: ConflictDetector | None = None,
    max_claims: int = 0,
    text_cache: TextCache | None = None,
) -> dict[str, Any]:
    """按 ``content_md`` 直接校验（**不落库**）——草稿尚未产出时的调试入口。"""
    warnings = [
        "调试入口：未写入 draft_claims / evidences，也未回写 claim_coverage"
    ]
    return await _run(
        session=session,
        draft_id=int(draft_id) if draft_id is not None else None,
        content_md=str(content_md or ""),
        draft_found=False,
        persist=False,
        commit=False,
        citation_map=citation_map,
        candidates_by_claim=candidates_by_claim,
        conflict_detector=conflict_detector,
        max_claims=max_claims,
        text_cache=text_cache,
        warnings=warnings,
    )


# --------------------------------------------------------------------------------------
# 可选：LLM 冲突判定器（默认不启用；启用即真实调用并落 llm_call_logs）
# --------------------------------------------------------------------------------------
_CONFLICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conflicting_evidence_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["conflicting_evidence_ids", "reason"],
    "additionalProperties": False,
}


def make_llm_conflict_detector(
    *, model_ref: str | None = None, project_id: int | None = None
) -> ConflictDetector:
    """构造一个基于 WP02 ``app.llm.chat_json`` 的冲突判定器（需显式注入才生效）。

    判定器只返回 ``conflicting_evidence_ids`` 与 ``reason``；**不给出分数**，
    规则层的三态裁决权不下放给 LLM（仅提供"哪条证据与 Claim 冲突"的信号）。
    """

    async def _detector(
        *,
        claim: Claim,
        claim_text: str,
        refs: Sequence[str],
        bound_ids: Sequence[int],
        session: Any,
        **_kwargs: Any,
    ) -> Mapping[str, Any]:
        if not bound_ids:
            return {"conflicting_evidence_ids": [], "reason": "无可比对的已绑定证据"}
        from app.llm import chat_json

        payload = {
            "claim": claim_text,
            "evidence": [
                {"evidence_id": int(evidence_id), "ref": refs[position] if position < len(refs) else None}
                for position, evidence_id in enumerate(bound_ids)
            ],
        }
        result = await chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "你是科研证据审阅员。判断给定证据是否与 Claim 存在**事实冲突**"
                        "（数值/方向/结论相反）。只输出冲突证据的 evidence_id；"
                        "不确定时输出空数组，禁止臆测。"
                    ),
                },
                {"role": "user", "content": str(payload)},
            ],
            _CONFLICT_SCHEMA,
            model_ref,
            project_id=project_id,
            stage="writing",
            purpose="claim_conflict_check",
        )
        parsed = result.parsed if isinstance(result.parsed, Mapping) else {}
        return {
            "conflicting_evidence_ids": list(parsed.get("conflicting_evidence_ids") or []),
            "reason": str(parsed.get("reason") or ""),
        }

    return _detector


__all__ = [
    "CITATION_RE",
    "ConflictDetector",
    "REF_KINDS",
    "load_draft",
    "make_llm_conflict_detector",
    "parse_citation_index",
    "parse_evidence_ref",
    "ref_to_candidate",
    "verify_claims",
    "verify_content",
]
