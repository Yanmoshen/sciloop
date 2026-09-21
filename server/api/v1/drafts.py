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
"""草稿 API（WP14-T6，附录 B / contracts.api_contract.key_endpoints.drafts）。

端点
----
``GET  /drafts/{id}``                   草稿正文 + Claim 三态 + ``claim_coverage`` + 引用映射
``GET  /drafts/{id}/export?format=md``  Markdown 导出（引用索引带可点击证据链接）
``GET  /drafts/{id}/export?format=pdf`` 未实现 → **501** 并说明原因（P1 路线图）

``/drafts/{id}/claims`` 与 ``POST /drafts/{id}/verify-evidence`` 由 WP13 的
``app/api/v1/claims.py`` 提供（在 ``main.py`` 中**先于本模块注册**，路径优先命中它），
本模块**不重复定义**同路径路由，避免同一 URL 出现两套行为；草稿详情接口内联返回
Claim 三态与 ``unsupported`` 段落位置，供工作台一次取全。

红线
----
- 只读端点返回真实落库数据；取不到即 ``null`` 并披露，**禁止编造指标或引用**
- ``pdf`` 导出未实现时明确返回 501，不返回伪 PDF
- 本模块导入时注册 ``writing`` / ``review`` 两个环节（``main.py`` 保持零改动）
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy import text as sql_text

from db.models import DraftClaim, PaperDraft, PipelineRun, ReviewScore
from db.session import AsyncSessionLocal
from services.writing import drafter

logger = logging.getLogger("sciloop.api.drafts")

router = APIRouter(tags=["drafts"])

#: ``/evidence/{type}/{id}`` 的合法类型（与 WP13 ``records.EVIDENCE_TYPES`` 一致）
EVIDENCE_TYPES: tuple[str, ...] = (
    "paper_span",
    "card_field",
    "experiment_run",
    "experiment_passport",
    "decision",
)

DISCLAIMER = "本内容由 AI 辅助生成，需研究者自行核验"

_TITLE_RE = re.compile(r"^\s*#\s+(.*)$", re.MULTILINE)
_REF_LINE_RE = re.compile(r"^\[(\d{1,3})\]\s+([A-Za-z_]+):(\d+)\s*\|(.*)$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# 环节注册（导入本模块即注入 writing / review；main.py 保持零改动）
# --------------------------------------------------------------------------- #
def _register_stages() -> dict[str, str]:
    """注册 ``writing`` 与 ``review`` 环节实现；失败不阻断 API 挂载但会告警。"""
    result: dict[str, str] = {}
    for stage_name in ("writing", "review"):
        module_path = f"services.pipeline.stages.{stage_name}"
        try:
            module = importlib.import_module(module_path)
            module.register()
            result[stage_name] = "registered"
        except Exception as exc:  # noqa: BLE001 - 注册失败必须可见（缺失环节会被引擎显式报错）
            logger.warning("%s 环节注册失败：%s", stage_name, exc, exc_info=True)
            result[stage_name] = f"failed:{type(exc).__name__}:{exc}"
    return result


STAGE_REGISTRATION = _register_stages()


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _session_factory() -> Any:
    if AsyncSessionLocal is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "db_unavailable", "message": "数据库会话不可用", "detail": None},
        )
    return AsyncSessionLocal


def _not_found(draft_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "draft_not_found", "message": f"draft {draft_id} 不存在", "detail": None},
    )


def _title_of(content_md: str) -> str | None:
    match = _TITLE_RE.search(content_md or "")
    if not match:
        return None
    return " ".join(match.group(1).split()) or None


def _splitter_claims(content_md: str) -> list[Any]:
    """用 WP13 的切分器复算 Claim 位置（纯函数；不可用时返回空）。"""
    try:
        from services.evidence.claim_splitter import split_claims

        return list(split_claims(content_md))
    except Exception as exc:  # noqa: BLE001 - WP13 未就绪时降级（位置字段置 null）
        logger.warning("claim_splitter 不可用，unsupported 位置将置 null：%s", exc)
        return []


def _claims_payload(
    rows: Sequence[DraftClaim],
    *,
    content_md: str,
    evidence_ids_by_claim: Mapping[int, list[int]] | None = None,
) -> list[dict[str, Any]]:
    """Claim 行 → API 结构（含 WP15 期望的 ``status`` / ``section_name`` / ``note`` 别名）。"""
    splits = _splitter_claims(content_md)
    positions: dict[int, Any] = {}
    if len(splits) == len(rows):
        positions = {int(row.id): claim for row, claim in zip(rows, splits, strict=True)}
    else:
        by_text = {str(getattr(claim, "claim_text", "")).strip(): claim for claim in splits}
        for row in rows:
            claim = by_text.get(str(row.claim_text or "").strip())
            if claim is not None:
                positions[int(row.id)] = claim

    payload: list[dict[str, Any]] = []
    for row in rows:
        claim_id = int(row.id)
        claim = positions.get(claim_id)
        evidence_ids = list((evidence_ids_by_claim or {}).get(claim_id, []))
        payload.append(
            {
                "id": claim_id,
                "claim_id": claim_id,
                "draft_id": int(row.draft_id),
                "index": getattr(claim, "index", None),
                "claim_text": row.claim_text,
                "text": row.claim_text,
                "section_heading": row.section_heading,
                "section_name": row.section_heading,
                "is_factual": bool(row.is_factual),
                "support_status": str(row.support_status),
                "status": str(row.support_status),
                "status_reason": row.status_reason,
                "note": row.status_reason,
                "evidence_count": int(row.evidence_count or 0),
                "evidence_ids": evidence_ids,
                "evidence": [
                    {"evidence_id": evidence_id, "evidence_url": None} for evidence_id in evidence_ids
                ],
                "char_start": getattr(claim, "char_start", None),
                "char_end": getattr(claim, "char_end", None),
            }
        )
    return payload


def _references_of(content_md: str) -> dict[str, Any]:
    """引用编号 → 证据键 + （可用时）证据详情 URL；不可点击的引用保持纯文本，不编造链接。"""
    index = drafter.parse_citation_index(content_md)
    references: dict[str, Any] = {}
    for number, ref in sorted(index.items()):
        kind, _, native = str(ref).partition(":")
        clickable = kind in EVIDENCE_TYPES and native.isdigit()
        references[str(number)] = {
            "ref": ref,
            "evidence_type": kind if clickable else None,
            "evidence_id": int(native) if clickable else None,
            "evidence_url": f"/api/v1/evidence/{kind}/{native}" if clickable else None,
            "clickable": clickable,
            "note": None if clickable else "该引用键类型无对应 /evidence 端点，导出时保留纯文本",
        }
    return references


def _export_markdown(content_md: str) -> str:
    """导出用 Markdown：引用索引行加可点击链接，正文与结构保持不变。"""

    def replace(match: re.Match[str]) -> str:
        number, kind, native, tail = match.group(1), match.group(2), match.group(3), match.group(4)
        if kind not in EVIDENCE_TYPES:
            return match.group(0)
        return f"[{number}] [{kind}:{native}](/api/v1/evidence/{kind}/{native}) ｜{tail}"

    exported = _REF_LINE_RE.sub(replace, content_md or "")
    if DISCLAIMER not in exported:
        exported = f"{exported}\n> {DISCLAIMER}\n"
    return exported


async def _load_draft(session: Any, draft_id: int) -> PaperDraft:
    row = (
        await session.execute(select(PaperDraft).where(PaperDraft.id == int(draft_id)))
    ).scalar_one_or_none()
    if row is None:
        raise _not_found(draft_id)
    return row


async def _evidence_ids_by_claim(session: Any, draft_id: int) -> dict[int, list[int]]:
    """``evidences`` 中绑定到本稿 Claim 的证据 id（``owner_type='draft_claim'``）。"""
    try:
        rows = (
            await session.execute(
                sql_text(
                    """
                    SELECT e.owner_id AS claim_id, e.id AS evidence_id
                    FROM evidences e
                    JOIN draft_claims c ON c.id = e.owner_id
                    WHERE e.owner_type = 'draft_claim' AND c.draft_id = :draft_id
                    ORDER BY e.id
                    """
                ),
                {"draft_id": int(draft_id)},
            )
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001 - 证据表未就绪时降级为空（不编造）
        logger.warning("读取 draft_claim 证据失败：%s", exc)
        with contextlib.suppress(Exception):
            await session.rollback()
        return {}
    grouped: dict[int, list[int]] = {}
    for row in rows:
        grouped.setdefault(int(row["claim_id"]), []).append(int(row["evidence_id"]))
    return grouped


async def _review_summary(session: Any, pipeline_run_id: int | None) -> dict[str, Any] | None:
    if pipeline_run_id is None:
        return None
    row = (
        await session.execute(
            select(ReviewScore)
            .where(ReviewScore.pipeline_run_id == int(pipeline_run_id))
            .order_by(ReviewScore.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    total = float(row.total) if row.total is not None else None
    dims = {
        "novelty": float(row.novelty) if row.novelty is not None else None,
        "rigor": float(row.rigor) if row.rigor is not None else None,
        "completeness": float(row.completeness) if row.completeness is not None else None,
        "reproducibility": float(row.reproducibility) if row.reproducibility is not None else None,
    }
    values = [value for value in dims.values() if value is not None]
    return {
        "review_score_id": int(row.id),
        "dimensions": dims,
        "total": total,
        "total_rule": "total = novelty + rigor + completeness + reproducibility",
        "total_matches_sum": (round(sum(values), 2) == round(total, 2)) if (total is not None and len(values) == 4) else None,
        "comments": row.comments,
    }


async def _pipeline_summary(session: Any, pipeline_run_id: int | None) -> dict[str, Any] | None:
    if pipeline_run_id is None:
        return None
    row = (
        await session.execute(select(PipelineRun).where(PipelineRun.id == int(pipeline_run_id)))
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "pipeline_run_id": int(row.id),
        "project_id": int(row.project_id),
        "iteration": int(row.iteration or 1),
        "status": str(row.status),
        "stop_reason": row.stop_reason,
        "total_cost_usd": float(row.total_cost_usd or 0),
    }


# --------------------------------------------------------------------------- #
# GET /drafts/{id}
# --------------------------------------------------------------------------- #
@router.get("/drafts/{draft_id}", summary="草稿详情（含 Claim 三态、覆盖率与未支撑段落）")
async def draft_detail(draft_id: int) -> dict[str, Any]:
    async with _session_factory()() as session:
        draft = await _load_draft(session, draft_id)
        rows = list(
            (
                await session.execute(
                    select(DraftClaim)
                    .where(DraftClaim.draft_id == int(draft_id))
                    .order_by(DraftClaim.id)
                )
            )
            .scalars()
            .all()
        )
        evidence_map = await _evidence_ids_by_claim(session, draft_id)
        counts_row = (
            await session.execute(
                select(
                    func.count(DraftClaim.id),
                    func.count(DraftClaim.id).filter(DraftClaim.is_factual.is_(True)),
                    func.count(DraftClaim.id).filter(
                        DraftClaim.is_factual.is_(True), DraftClaim.support_status == "supported"
                    ),
                    func.count(DraftClaim.id).filter(
                        DraftClaim.is_factual.is_(True), DraftClaim.support_status == "contradicted"
                    ),
                    func.count(DraftClaim.id).filter(
                        DraftClaim.is_factual.is_(True), DraftClaim.support_status == "insufficient"
                    ),
                ).where(DraftClaim.draft_id == int(draft_id))
            )
        ).one()
        review = await _review_summary(session, draft.pipeline_run_id)
        pipeline = await _pipeline_summary(session, draft.pipeline_run_id)

    content_md = str(draft.content_md or "")
    claims = _claims_payload(rows, content_md=content_md, evidence_ids_by_claim=evidence_map)
    references = _references_of(content_md)
    unsupported = [
        {
            "claim_id": claim["claim_id"],
            "index": claim["index"],
            "section_heading": claim["section_heading"],
            "claim_text": claim["claim_text"],
            "char_start": claim["char_start"],
            "char_end": claim["char_end"],
            "support_status": claim["support_status"],
            "reason": claim["status_reason"],
        }
        for claim in claims
        if claim["is_factual"] and claim["support_status"] != "supported"
    ]
    counts = {
        "total": int(counts_row[0] or 0),
        "factual": int(counts_row[1] or 0),
        "supported": int(counts_row[2] or 0),
        "contradicted": int(counts_row[3] or 0),
        "insufficient": int(counts_row[4] or 0),
    }
    counts["non_factual"] = counts["total"] - counts["factual"]
    counts["note"] = "supported/contradicted/insufficient 仅统计事实性 Claim（claim_coverage 的分母口径）"

    return {
        "id": int(draft.id),
        "draft_id": int(draft.id),
        "project_id": int(draft.project_id),
        "pipeline_run_id": int(draft.pipeline_run_id) if draft.pipeline_run_id is not None else None,
        "iteration": int(draft.iteration or 1),
        "title": _title_of(content_md) or f"项目 {draft.project_id} 研究草稿",
        "content_md": content_md,
        "content_markdown": content_md,
        "claim_coverage": float(draft.claim_coverage) if draft.claim_coverage is not None else None,
        "coverage_note": None
        if draft.claim_coverage is not None
        else "无事实性 Claim：claim_coverage 无定义（不用 0 或 1 冒充）",
        "status": "verified" if counts["factual"] else "unverified",
        "created_at": draft.created_at.isoformat() if draft.created_at else None,
        "claim_counts": counts,
        "unsupported": unsupported,
        "unsupported_note": "无证据/校验未通过的事实性句子必须标 insufficient 或 contradicted，前端据此高亮",
        "claims": claims,
        "references": references,
        "citation_count": len(references),
        "review": review,
        "pipeline": pipeline,
        "artifacts": [
            {
                "name": "draft.md",
                "kind": "draft_markdown",
                "url": f"/api/v1/drafts/{int(draft.id)}/export?format=md",
                "note": "Markdown 草稿（三件套中的数据/记录由 WP11/WP15 汇总）",
            }
        ],
        "related_endpoints": {
            "claims": f"/api/v1/drafts/{int(draft.id)}/claims",
            "verify_evidence": f"/api/v1/drafts/{int(draft.id)}/verify-evidence",
            "exporter": "WP13 app/api/v1/claims.py（先于本模块注册，路径优先命中它）",
        },
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- #
# GET /drafts/{id}/export
# --------------------------------------------------------------------------- #
@router.get("/drafts/{draft_id}/export", summary="导出草稿（md 可用；pdf 未实现返回 501）")
async def export_draft(
    draft_id: int,
    format: Annotated[str, Query(pattern="^(md|markdown|pdf)$")] = "md",
) -> Response:
    async with _session_factory()() as session:
        draft = await _load_draft(session, draft_id)
        content_md = str(draft.content_md or "")

    if format in {"md", "markdown"}:
        body = _export_markdown(content_md)
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="draft-{int(draft.id)}.md"',
                "X-SciLoop-Citation-Count": str(len(drafter.parse_citation_index(content_md))),
            },
        )

    # pdf：P1 路线图（不引入运行时渲染依赖，也不返回伪 PDF）
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "code": "export_format_not_implemented",
            "message": "format=pdf 未实现：Markdown 为 P0 权威导出格式，PDF 属 P1 路线图",
            "detail": {
                "requested_format": "pdf",
                "available_formats": ["md"],
                "reason": (
                    "P0 不引入 PDF 渲染依赖（禁止运行时安装依赖）；且草稿引用需保持可点击结构，"
                    "Markdown 是唯一权威载体。三件套打包由 WP15 汇总（代码/实验记录见 WP11）"
                ),
                "workaround": "先导出 md，再由前端/评审人按需打印为 PDF",
            },
        },
    )


__all__ = ["STAGE_REGISTRATION", "router"]
