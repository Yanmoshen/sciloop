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
"""多篇论文聚合端点（WP08-T1/T2/T3，附录 B.2）。

==========================================  ==================================================
``POST /aggregations``                      创建聚合：对比矩阵 + 方法演进 + 空白清单（Owner）
``GET  /aggregations``                      聚合列表（UI 选择器用）
``GET  /aggregations/{id}``                 取回矩阵 + 演进 + 空白清单（单元格可展开证据）
``GET  /aggregations/{id}/gaps``            只取空白清单（每条含提出者论文 + 未解决证据）
``DELETE /aggregations/{id}``               删除聚合（Owner；``gaps`` 级联删除）
==========================================  ==================================================

- 写操作仅 Owner 面（``X-Owner-Token``）；``public_demo`` 只放行 GET。
- 论文必须真实存在，找不到即 422（``unknown_paper_id``），**不为不存在的论文产出行**。
- 空白清单只引用真实 ``paper_span`` / ``card_field``；不能证据化的候选在
  ``gaps_payload_meta.dropped`` 中如实列出原因。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_owner
from app.db.session import AsyncSessionLocal
from app.services.aggregation import (
    MAX_PAPERS,
    MIN_PAPERS,
    AggregationError,
    create_aggregation,
    delete_aggregation,
    get_aggregation,
    list_aggregations,
    load_gaps,
)

logger = logging.getLogger("sciloop.wp08.aggregations_api")

router = APIRouter(tags=["aggregations"])


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
        status_code=status_code, detail={"code": code, "message": message, "detail": detail}
    )


class AggregationCreate(BaseModel):
    """``POST /aggregations`` 入参。"""

    paper_ids: list[int] = Field(
        ...,
        min_length=MIN_PAPERS,
        max_length=MAX_PAPERS,
        description=f"参与聚合的论文 id（{MIN_PAPERS}–{MAX_PAPERS} 篇，必须真实存在）",
    )
    project_id: int | None = Field(
        default=None, description="可选：关联项目；为空时聚合不归属任何项目"
    )


@router.post(
    "/aggregations",
    summary="创建聚合（对比矩阵 + 方法演进 + 空白清单，Owner）",
    dependencies=[Depends(require_owner)],
)
async def post_aggregation(body: AggregationCreate, session: DbSession) -> dict[str, Any]:
    """一次调用产出三份产物并落库（``aggregations`` + ``gaps``）。"""
    try:
        payload = await create_aggregation(
            session, list(body.paper_ids), project_id=body.project_id
        )
    except AggregationError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message, exc.detail) from exc
    payload["endpoints"] = {
        "self": f"/api/v1/aggregations/{payload['id']}",
        "gaps": f"/api/v1/aggregations/{payload['id']}/gaps",
        "ideas_generate": "POST /api/v1/ideas/generate",
    }
    return payload


@router.get("/aggregations", summary="聚合列表")
async def get_aggregations(
    session: DbSession,
    project_id: int | None = Query(None, description="按项目过滤"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    items = await list_aggregations(session, project_id=project_id, limit=limit)
    return {"items": items, "total": len(items), "project_id": project_id}


@router.get("/aggregations/{aggregation_id}", summary="聚合详情（矩阵 + 演进 + 空白清单）")
async def get_aggregation_detail(aggregation_id: int, session: DbSession) -> dict[str, Any]:
    payload = await get_aggregation(session, int(aggregation_id))
    if payload is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "aggregation_not_found",
            f"聚合 {aggregation_id} 不存在",
            {"aggregation_id": int(aggregation_id)},
        )
    payload["evidence_hint"] = (
        "矩阵单元格的 evidence[].candidate 可直接用于 POST /ideas 的证据绑定；"
        "空白清单的 unsolved_evidence 是真实 paper_span，可调 GET /evidence/paper_span/{id} 展开"
    )
    return payload


@router.get("/aggregations/{aggregation_id}/gaps", summary="空白清单（提出者论文 + 未解决证据）")
async def get_aggregation_gaps(aggregation_id: int, session: DbSession) -> dict[str, Any]:
    gaps = await load_gaps(session, int(aggregation_id))
    return {
        "aggregation_id": int(aggregation_id),
        "items": gaps,
        "total": len(gaps),
        "scope_note": (
            "「未被解决」仅在本次聚合的论文集合内判定；本包不做空白反证检索"
            "（WP08 out_of_scope / 计划书 v1.2 P1）"
        ),
    }


@router.delete(
    "/aggregations/{aggregation_id}",
    summary="删除聚合（Owner；gaps 级联删除）",
    dependencies=[Depends(require_owner)],
)
async def remove_aggregation(aggregation_id: int, session: DbSession) -> dict[str, Any]:
    deleted = await delete_aggregation(session, int(aggregation_id))
    if not deleted:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "aggregation_not_found",
            f"聚合 {aggregation_id} 不存在",
            {"aggregation_id": int(aggregation_id)},
        )
    return {"deleted": True, "aggregation_id": int(aggregation_id)}
