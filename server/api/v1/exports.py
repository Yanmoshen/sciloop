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
"""多格式导出端点（EasyPaper 第 ④ 个核心模块在 SciLoop 的落地）。

============================================  ==========================  =====================
路径                                          产物                        Content-Type
============================================  ==========================  =====================
``GET /exports/json``                          全库知识 JSON               ``application/json``
``GET /exports/paper/{paper_id}``              单篇论文完整知识 JSON       ``application/json``
``GET /exports/bibtex``                        BibTeX                      ``text/x-bibtex``
``GET /exports/csl-json``                      CSL-JSON（Zotero/Mendeley） ``application/json``
``GET /exports/obsidian``                      Obsidian ZIP（Markdown）    ``application/zip``
``GET /exports/csv``                           CSV ZIP（实体/关系）        ``application/zip``
============================================  ==========================  =====================

查询参数（全部端点可选）
------------------------
``?limit=``  导出论文数上限，默认 200，允许 1–1000（越界 → **422**）
``?paper_ids=1,2,3``  仅导出这些论文（非数字 → **422**）
``?include_claims=true|false``  是否包含 findings（Claim 三态），默认 true

访问面（口径与 ``/papers/feed`` 一致）
--------------------------------------
SciLoop 是**单 Owner 的 public_demo 演示面**，没有多用户隔离：这 6 个端点
**一律公开只读**，无需 ``X-Owner-Token``。导出内容不含任何用户私有数据
——SciLoop 不存在该概念（无用户表、无批注表）。

错误口径
--------
- 统一错误体 ``{code, message, detail}``（由 ``app/main.py`` 的处理器渲染）；
- 非法参数（``limit=0`` / ``limit=99999`` / ``paper_ids=abc``）→ **422**；
- 未知 ``paper_id`` → **404** ``{"code": "paper_not_found"}``；
- 数据库会话不可用 → 503 ``database_unavailable``。

红线
----
- 仅读取真实表（``papers`` / ``paper_documents`` / ``paper_spans`` / ``paper_cards`` /
  ``draft_claims`` / ``evidences``），**禁止编造**；EasyPaper 有而 SciLoop 没有的字段
  输出空并在 ``notes`` / ``@meta.mapping_notes`` 说明。
- 导出全部为**只读 GET**：public_demo 面不暴露任何写操作。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal
from services.export import (
    DISCLAIMER,
    SCHEMA_VERSION,
    SCOPE_NOTE,
    ExportRepository,
    build_csv_zip,
    build_obsidian_zip,
    derive_entities,
    derive_relationships,
    export_meta,
    load_library,
    load_metadata_list,
    load_paper_knowledge,
    locator_note,
    render_bibtex,
    render_csl_items,
    source_counts,
)

logger = logging.getLogger("sciloop.api.exports")

router = APIRouter(tags=["exports"])

#: ``?limit=`` 默认值与上限
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
#: ``?paper_ids=1,2,3`` 的合法形态（非法 → 422）
PAPER_IDS_PATTERN = r"^[0-9]+(,[0-9]+)*$"

LimitQuery = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
PaperIdsQuery = Annotated[str | None, Query(pattern=PAPER_IDS_PATTERN)]
IncludeClaimsQuery = Annotated[bool, Query()]


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


def _parse_paper_ids(raw: str | None) -> list[int] | None:
    """``"1,2,3"`` → ``[1, 2, 3]``（形态已由 Query pattern 保证，这里只做去重排序）。"""
    if raw is None:
        return None
    values = [int(part) for part in raw.split(",") if part.strip()]
    return sorted(dict.fromkeys(values)) or None


def _export_notes(include_claims: bool) -> list[str]:
    notes = [
        SCOPE_NOTE,
        DISCLAIMER,
        "导出内容全部来自 SciLoop 真实表；EasyPaper 有而 SciLoop 没有的字段输出空并在本列表说明",
        "flashcards 恒为 []：SciLoop 无闪卡/间隔重复（SRS）域；annotations 恒为 []：SciLoop 无用户批注表",
        locator_note(),
        "global_entities / global_relationships 为派生视图（由 paper_cards 与 evidences 派生），每条带 derived_from",
        "本导出为只读 GET：public_demo 面不暴露任何写操作",
    ]
    if not include_claims:
        notes.append("include_claims=false：findings 与 Claim→论文关系均未导出（不伪造）")
    return notes


# --------------------------------------------------------------------------- #
# GET /exports/json —— 全库知识导出
# --------------------------------------------------------------------------- #
@router.get("/exports/json", summary="全库知识导出（JSON，EasyPaper §5.4 形状）")
async def export_library_json(
    session: DbSession,
    limit: LimitQuery = DEFAULT_LIMIT,
    paper_ids: PaperIdsQuery = None,
    include_claims: IncludeClaimsQuery = True,
) -> dict[str, Any]:
    """导出至多 ``limit`` 篇论文的完整知识 + 全局派生实体/关系。

    单篇节点内容与 ``GET /exports/paper/{id}`` 的同名键**完全一致**（同参数同算法），
    便于对照核验。
    """
    ids = _parse_paper_ids(paper_ids)
    exported_at = datetime.now(UTC).isoformat()
    repository = ExportRepository(session)
    papers_total = await repository.count_papers()
    loaded = await load_library(
        repository, limit=limit, paper_ids=ids, include_claims=include_claims
    )
    papers = loaded["papers"]
    entities, entities_truncated = derive_entities(papers)
    relationships, relationships_truncated = derive_relationships(papers, entities)
    meta = export_meta(
        limit=limit,
        paper_ids=ids,
        include_claims=include_claims,
        papers_total=papers_total,
        papers_exported=len(papers),
        entities_truncated=entities_truncated,
        relationships_truncated=relationships_truncated,
        exported_at=exported_at,
    )
    logger.info(
        "export_json papers=%d/%d entities=%d relationships=%d include_claims=%s",
        len(papers),
        papers_total,
        len(entities),
        len(relationships),
        include_claims,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
        "access_mode": "public_demo",
        "scope_note": SCOPE_NOTE,
        "filters": meta["filters"],
        "papers": papers,
        "papers_total_in_db": papers_total,
        "papers_exported": len(papers),
        "global_entities": entities,
        "global_relationships": relationships,
        "source_counts": source_counts(papers),
        "notes": _export_notes(include_claims),
        "@meta": meta,
    }


# --------------------------------------------------------------------------- #
# GET /exports/paper/{paper_id} —— 单篇论文完整知识
# --------------------------------------------------------------------------- #
@router.get("/exports/paper/{paper_id}", summary="单篇论文完整知识（JSON）")
async def export_paper_json(
    paper_id: int,
    session: DbSession,
    include_claims: IncludeClaimsQuery = True,
) -> dict[str, Any]:
    """导出单篇论文；``paper_id`` 不存在 → 404 ``paper_not_found``。"""
    exported_at = datetime.now(UTC).isoformat()
    repository = ExportRepository(session)
    paper = await repository.get_paper(paper_id)
    if paper is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "paper_not_found",
            f"论文 {paper_id} 不存在",
            {"paper_id": paper_id},
        )
    node = await load_paper_knowledge(repository, paper, include_claims=include_claims)
    entities, entities_truncated = derive_entities([node])
    relationships, relationships_truncated = derive_relationships([node], entities)
    papers_total = await repository.count_papers()
    meta = export_meta(
        limit=1,
        paper_ids=[int(paper_id)],
        include_claims=include_claims,
        papers_total=papers_total,
        papers_exported=1,
        entities_truncated=entities_truncated,
        relationships_truncated=relationships_truncated,
        exported_at=exported_at,
    )
    logger.info(
        "export_paper paper_id=%s findings=%d entities=%d",
        paper_id,
        len(node.get("findings") or []),
        len(entities),
    )
    # 顶层先铺开单篇节点（与 /exports/json 的 papers[i] 同名键逐字节一致），再补导出信封
    return {
        **node,
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported_at,
        "paper_id": int(paper_id),
        "access_mode": "public_demo",
        "scope_note": SCOPE_NOTE,
        "global_entities": entities,
        "global_relationships": relationships,
        "@meta": meta,
    }


# --------------------------------------------------------------------------- #
# GET /exports/bibtex
# --------------------------------------------------------------------------- #
@router.get("/exports/bibtex", summary="BibTeX 导出（text/x-bibtex，附件下载）")
async def export_bibtex(
    session: DbSession,
    limit: LimitQuery = DEFAULT_LIMIT,
    paper_ids: PaperIdsQuery = None,
) -> Response:
    """从 ``papers`` 元数据生成 BibTeX；特殊字符已转义、空字段跳过。"""
    ids = _parse_paper_ids(paper_ids)
    exported_at = datetime.now(UTC).isoformat()
    repository = ExportRepository(session)
    metas = await load_metadata_list(repository, limit=limit, paper_ids=ids)
    body = render_bibtex(metas, exported_at=exported_at)
    logger.info("export_bibtex entries=%d", len(metas))
    return Response(
        content=body.encode("utf-8"),
        media_type="text/x-bibtex",
        headers={
            "Content-Disposition": 'attachment; filename="sciloop-references.bib"',
            "X-SciLoop-Export-Schema-Version": SCHEMA_VERSION,
            "X-SciLoop-Exported-At": exported_at,
            "X-SciLoop-Export-Entries": str(len(metas)),
        },
    )


# --------------------------------------------------------------------------- #
# GET /exports/csl-json
# --------------------------------------------------------------------------- #
@router.get("/exports/csl-json", summary="CSL-JSON 导出（Zotero / Mendeley 兼容）")
async def export_csl_json(
    session: DbSession,
    limit: LimitQuery = DEFAULT_LIMIT,
    paper_ids: PaperIdsQuery = None,
) -> JSONResponse:
    """输出 **CSL-JSON 顶层数组**（Zotero/Mendeley 直接导入的形态）。

    数组形态是导入兼容性的硬要求，因此 ``schema_version`` / ``exported_at``
    以「每条 ``custom.sciloop.schema_version`` + 响应头」的形式携带，
    不额外包一层信封（包信封会破坏导入兼容性）。
    """
    ids = _parse_paper_ids(paper_ids)
    exported_at = datetime.now(UTC).isoformat()
    repository = ExportRepository(session)
    metas = await load_metadata_list(repository, limit=limit, paper_ids=ids)
    items = render_csl_items(metas)
    logger.info("export_csl_json items=%d", len(items))
    return JSONResponse(
        content=items,
        headers={
            "Content-Disposition": 'inline; filename="sciloop-csl.json"',
            "X-SciLoop-Export-Schema-Version": SCHEMA_VERSION,
            "X-SciLoop-Exported-At": exported_at,
            "X-SciLoop-Export-Entries": str(len(items)),
        },
    )


# --------------------------------------------------------------------------- #
# GET /exports/obsidian
# --------------------------------------------------------------------------- #
@router.get("/exports/obsidian", summary="Obsidian ZIP（Markdown 笔记 + wiki 链接）")
async def export_obsidian(
    session: DbSession,
    limit: LimitQuery = DEFAULT_LIMIT,
    paper_ids: PaperIdsQuery = None,
    include_claims: IncludeClaimsQuery = True,
) -> Response:
    """把论文与派生实体渲染为 Markdown 笔记并打包为 ZIP（条目名已安全化）。"""
    ids = _parse_paper_ids(paper_ids)
    exported_at = datetime.now(UTC).isoformat()
    repository = ExportRepository(session)
    loaded = await load_library(
        repository, limit=limit, paper_ids=ids, include_claims=include_claims
    )
    papers = loaded["papers"]
    entities, _ = derive_entities(papers)
    relationships, _ = derive_relationships(papers, entities)
    payload, stats = build_obsidian_zip(
        papers, entities, relationships, exported_at=exported_at
    )
    logger.info(
        "export_obsidian papers=%d entities=%d entries=%d bytes=%d",
        stats["paper_notes"],
        stats["entity_notes"],
        stats["entries"],
        len(payload),
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="sciloop-vault.zip"',
            "X-SciLoop-Export-Schema-Version": SCHEMA_VERSION,
            "X-SciLoop-Exported-At": exported_at,
            "X-SciLoop-Export-Entries": str(stats["entries"]),
            "X-SciLoop-Export-Paper-Notes": str(stats["paper_notes"]),
            "X-SciLoop-Export-Entity-Notes": str(stats["entity_notes"]),
        },
    )


# --------------------------------------------------------------------------- #
# GET /exports/csv
# --------------------------------------------------------------------------- #
@router.get("/exports/csv", summary="CSV ZIP（entities.csv + relationships.csv，UTF-8 BOM）")
async def export_csv(
    session: DbSession,
    limit: LimitQuery = DEFAULT_LIMIT,
    paper_ids: PaperIdsQuery = None,
    include_claims: IncludeClaimsQuery = True,
) -> Response:
    """导出派生实体与关系的两张 CSV（编码 UTF-8 with BOM，Excel 直接可读）。"""
    ids = _parse_paper_ids(paper_ids)
    exported_at = datetime.now(UTC).isoformat()
    repository = ExportRepository(session)
    loaded = await load_library(
        repository, limit=limit, paper_ids=ids, include_claims=include_claims
    )
    papers = loaded["papers"]
    entities, _ = derive_entities(papers)
    relationships, _ = derive_relationships(papers, entities)
    payload, stats = build_csv_zip(entities, relationships, exported_at=exported_at)
    logger.info(
        "export_csv entities=%d relationships=%d bytes=%d",
        stats["entity_rows"],
        stats["relationship_rows"],
        len(payload),
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="sciloop-knowledge-csv.zip"',
            "X-SciLoop-Export-Schema-Version": SCHEMA_VERSION,
            "X-SciLoop-Exported-At": exported_at,
            "X-SciLoop-Export-Entries": str(stats["entries"]),
            "X-SciLoop-Export-Entity-Rows": str(stats["entity_rows"]),
            "X-SciLoop-Export-Relationship-Rows": str(stats["relationship_rows"]),
        },
    )


# --------------------------------------------------------------------------- #
# 模块级常量导出（供测试/前端对接引用）
# --------------------------------------------------------------------------- #
ENDPOINTS: Sequence[str] = (
    "/api/v1/exports/json",
    "/api/v1/exports/paper/{paper_id}",
    "/api/v1/exports/bibtex",
    "/api/v1/exports/csl-json",
    "/api/v1/exports/obsidian",
    "/api/v1/exports/csv",
)

__all__ = ["DEFAULT_LIMIT", "ENDPOINTS", "MAX_LIMIT", "router"]
