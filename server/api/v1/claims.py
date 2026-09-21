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
"""草稿 Claim 级证据端点（WP13-T5，附录 B.5）。

==========================================  ==========================================
``GET  /drafts/{id}/claims``                Claim 列表（三态 + 证据数 + 覆盖率统计）
``POST /drafts/{id}/verify-evidence``       重跑校验，返回 ``unsupported_spans`` 与覆盖率
==========================================  ==========================================

口径与硬约束
------------
- ``claim_coverage = supported Claim 数 ÷ 事实性 Claim 总数``；事实性总数为 0 时返回
  ``null`` 并附 ``coverage_note``（**不用 0 或 1 冒充**）。
- 事实性 Claim 必给三态 ``supported | contradicted | insufficient``，不得留空；
  非 supported 的事实性 Claim 一律出现在 ``unsupported_spans`` 里（供前端高亮）。
- **容忍草稿不存在**：``paper_drafts`` 由 WP14 并行产出，行不存在时两个端点都返回
  ``draft_found=false`` + ``warnings``，**不抛 404**，UI 可显示如实的未就绪态。
- ``POST`` 支持 ``content_md`` 调试入口：草稿未落库时可直接校验纯文本（只读，不写库）。
- 写操作仅 Owner 面（contracts.api_contract：「所有 POST 默认要求 X-Owner-Token」；
  ``public_demo`` 只放行 GET），非 Owner 一律 403。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.session import AsyncSessionLocal
from services.evidence import (
    CLAIM_STATUSES,
    EvidenceError,
    claim_coverage,
    load_draft,
    make_llm_conflict_detector,
    verify_claims,
    verify_content,
)

logger = logging.getLogger("sciloop.wp13.claims_api")

router = APIRouter(tags=["claims"])

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 200
STATUS_HELP = "受控 Claim 状态之一：" + " / ".join(CLAIM_STATUSES)

DRAFT_MISSING_NOTE = (
    "paper_drafts 中不存在该草稿（WP14 尚未产出）：返回未就绪态，未伪造任何 Claim"
)


async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "database_unavailable",
            "异步数据库会话工厂不可用（DATABASE_URL / asyncpg 未就绪）",
        )
    async with AsyncSessionLocal() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(_session)]


def _error(status_code: int, code: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):  # pragma: no cover - NUMERIC 列受控
        return None


# --------------------------------------------------------------------------------------
# GET /drafts/{id}/claims
# --------------------------------------------------------------------------------------
async def _claims_with_evidence(
    session: AsyncSession, draft_id: int
) -> tuple[list[Mapping[str, Any]], dict[int, list[dict[str, Any]]]]:
    """读 ``draft_claims`` 及其绑定的 ``evidences``（一次查询，避免 N+1）。"""
    rows = (
        await session.execute(
            text(
                """
                SELECT id, draft_id, section_heading, claim_text, is_factual,
                       support_status, status_reason, evidence_count, created_at
                  FROM draft_claims
                 WHERE draft_id = :draft_id
                 ORDER BY id
                """
            ),
            {"draft_id": int(draft_id)},
        )
    ).mappings().all()
    claims = [dict(row) for row in rows]
    if not claims:
        return claims, {}

    evidence_rows = (
        await session.execute(
            text(
                """
                SELECT id, owner_id, evidence_type, paper_span_id, paper_id,
                       card_field, experiment_run_id, experiment_passport_id,
                       decision_log_id, metric_name, quote_text, weight
                  FROM evidences
                 WHERE owner_type = 'draft_claim' AND owner_id = ANY(:claim_ids)
                 ORDER BY id
                """
            ),
            {"claim_ids": [int(row["id"]) for row in claims]},
        )
    ).mappings().all()

    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in evidence_rows:
        grouped.setdefault(int(row["owner_id"]), []).append(
            {
                "id": int(row["id"]),
                "evidence_id": int(row["id"]),
                "evidence_type": row["evidence_type"],
                "paper_id": row["paper_id"],
                "paper_span_id": row["paper_span_id"],
                "card_field": row["card_field"],
                "experiment_run_id": row["experiment_run_id"],
                "experiment_passport_id": row["experiment_passport_id"],
                "decision_log_id": row["decision_log_id"],
                "metric_name": row["metric_name"],
                "quote_text": row["quote_text"],
                "weight": _num(row["weight"]),
            }
        )
    return claims, grouped


def _claim_item(
    row: Mapping[str, Any], evidence: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """单条 Claim 的响应体（``status`` 为 ``support_status`` 的前端别名）。"""
    support_status = str(row.get("support_status") or "insufficient")
    return {
        "id": int(row["id"]),
        "claim_id": int(row["id"]),
        "draft_id": int(row["draft_id"]),
        "index": None,
        "section_heading": row.get("section_heading"),
        "claim_text": row.get("claim_text"),
        "text": row.get("claim_text"),
        "is_factual": bool(row.get("is_factual")),
        "support_status": support_status,
        "status": support_status,
        "status_reason": row.get("status_reason"),
        "evidence_count": int(row.get("evidence_count") or 0),
        "evidence_ids": [int(item["id"]) for item in evidence],
        "evidence": [dict(item) for item in evidence],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


@router.get(
    "/drafts/{draft_id}/claims",
    summary="Claim 列表（supported / contradicted / insufficient + 证据数与覆盖率）",
)
async def list_draft_claims(
    draft_id: int,
    session: DbSession,
    support_status: str | None = Query(None, description=STATUS_HELP),
    factual_only: bool = Query(
        False, description="只返回事实性 Claim（claim_coverage 的分母口径）"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """返回该草稿的 Claim 列表与统计；草稿不存在时返回未就绪态（非 404）。"""
    if support_status is not None and support_status not in CLAIM_STATUSES:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_claim_status",
            f"support_status='{support_status}' 不在受控值域内",
            {"allowed": list(CLAIM_STATUSES)},
        )

    draft = await load_draft(session, int(draft_id))
    if draft is None:
        logger.info("list_draft_claims 草稿不存在 draft_id=%s", draft_id)
        return {
            "draft_id": int(draft_id),
            "draft_found": False,
            "items": [],
            "total": 0,
            "page": page,
            "page_size": page_size,
            "counts": dict.fromkeys(CLAIM_STATUSES, 0) | {"total": 0, "factual": 0},
            "claim_coverage": None,
            "coverage_note": "草稿不存在：claim_coverage 无定义",
            "unsupported_spans": [],
            "warnings": [DRAFT_MISSING_NOTE],
            "note": DRAFT_MISSING_NOTE,
        }

    claims, grouped = await _claims_with_evidence(session, int(draft_id))
    items = [_claim_item(row, grouped.get(int(row["id"]), [])) for row in claims]
    if factual_only:
        items = [item for item in items if item["is_factual"]]
    if support_status is not None:
        items = [item for item in items if item["support_status"] == support_status]

    factual_all = [
        item for item in items if item["is_factual"]
    ] if not factual_only else items
    counts = dict.fromkeys(CLAIM_STATUSES, 0)
    for item in factual_all:
        counts[item["support_status"]] = counts.get(item["support_status"], 0) + 1
    totals = {
        **counts,
        "factual": len(factual_all),
        "non_factual": 0 if factual_only else sum(1 for item in items if not item["is_factual"]),
        "total": len(items),
    }
    supported = counts["supported"]
    coverage = claim_coverage(supported, len(factual_all))

    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]

    return {
        "draft_id": int(draft_id),
        "draft_found": True,
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": totals,
        "claim_coverage": coverage,
        "claim_coverage_persisted": _num(draft.get("claim_coverage")),
        "coverage_formula": "supported Claim 数 ÷ 事实性 Claim 总数",
        "coverage_note": (
            None
            if coverage is not None
            else "无事实性 Claim：claim_coverage 无定义（不用 0 或 1 冒充）"
        ),
        "unsupported_spans": [
            {
                "claim_id": item["claim_id"],
                "section_heading": item["section_heading"],
                "claim_text": item["claim_text"],
                "support_status": item["support_status"],
                "status_reason": item["status_reason"],
            }
            for item in factual_all
            if item["support_status"] != "supported"
        ],
        "warnings": [],
        "note": (
            "使用 GET /drafts/{id}/claims 读取落库状态；POST /drafts/{id}/verify-evidence "
            "会重跑拆分与哈希优先校验并回写三态与 claim_coverage"
        ),
    }


# --------------------------------------------------------------------------------------
# POST /drafts/{id}/verify-evidence
# --------------------------------------------------------------------------------------
class VerifyEvidenceRequest(BaseModel):
    """重跑校验的入参（全部可选）。"""

    content_md: str | None = Field(
        default=None,
        description=(
            "调试入口：草稿尚未落库（WP14 未产出）时按此正文直接校验（只读，不写库）。"
            "不传则读 paper_drafts.content_md"
        ),
    )
    persist: bool = Field(
        default=True,
        description="是否把三态写回 draft_claims 并回写 paper_drafts.claim_coverage",
    )
    use_llm_conflict: bool = Field(
        default=False,
        description=(
            "是否启用 LLM 冲突判定（WP02 chat_json）。默认关闭：规则层三态可离线复现；"
            "启用后必须给出冲突证据 id"
        ),
    )
    max_claims: int = Field(default=0, ge=0, description="0 = 不限；>0 按出现顺序截断")


@router.post(
    "/drafts/{draft_id}/verify-evidence",
    summary="重跑 Claim 级证据校验（返回 unsupported_spans 与 claim_coverage，owner 面）",
    dependencies=[Depends(require_owner)],
)
async def verify_draft_evidence(
    draft_id: int,
    session: DbSession,
    body: VerifyEvidenceRequest | None = None,
) -> dict[str, Any]:
    """重跑拆分 + 哈希优先校验 + 三态判定，返回未支撑段落位置与覆盖率。

    草稿行不存在时：若给了 ``content_md`` 则走只读调试校验；否则返回
    ``draft_found=false`` 与 ``warnings``（**不抛 404**，容忍 WP14 并行未就绪）。
    """
    request = body or VerifyEvidenceRequest()
    conflict_detector = (
        make_llm_conflict_detector() if request.use_llm_conflict else None
    )
    try:
        if request.content_md is not None:
            report = await verify_content(
                request.content_md,
                session=session,
                draft_id=int(draft_id),
                conflict_detector=conflict_detector,
                max_claims=request.max_claims,
            )
            report["mode"] = "content_md_debug"
        else:
            report = await verify_claims(
                int(draft_id),
                session=session,
                conflict_detector=conflict_detector,
                persist=request.persist,
                max_claims=request.max_claims,
            )
            report["mode"] = "draft_persisted"
    except EvidenceError as exc:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message, exc.detail
        ) from exc

    report["coverage_formula"] = "supported Claim 数 ÷ 事实性 Claim 总数"
    report["unsupported_total"] = len(report.get("unsupported_spans") or [])
    logger.info(
        "verify_evidence draft_id=%s mode=%s draft_found=%s coverage=%s unsupported=%s",
        draft_id,
        report.get("mode"),
        report.get("draft_found"),
        report.get("claim_coverage"),
        report.get("unsupported_total"),
    )
    return report
