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
"""任务书端点（WP08-T6，附录 B.3）。

==========================================  ==================================================
``POST  /taskbooks``                        创建任务书（默认 ``draft``，Owner）
``GET   /taskbooks``                        按项目列任务书
``GET   /taskbooks/{id}``                   取任务书
``PATCH /taskbooks/{id}``                   编辑（**已锁定 → 409**，Owner）
``POST  /taskbooks/{id}/lock``              锁定为流水线唯一执行依据（Owner）
==========================================  ==================================================

红线：

- 锁定后 ``PATCH`` 一律 **409**（``taskbook_locked``），提示已锁定且只读；
- ``compute_budget.max_llm_cost_usd`` 不得超过环境硬护栏（默认 8.0），
  试图放宽返回 **422**（``guardrail_relaxed``）——护栏阈值不可被参数覆盖；
- 锁定前校验 idea 已绑定至少 1 条证据，否则 422（``idea_without_evidence``）。
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
from app.services.feasibility import (
    DELIVERABLE_OPTIONS,
    TaskbookError,
    TaskbookLockedError,
    create_taskbook,
    default_compute_budget,
    default_rounds,
    get_taskbook,
    list_taskbooks,
    lock_taskbook,
    update_taskbook,
)

logger = logging.getLogger("sciloop.wp08.taskbooks_api")

router = APIRouter(tags=["taskbooks"])


async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:  # pragma: no cover
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


def _raise_taskbook_error(exc: TaskbookError, *, default_status: int) -> None:
    if isinstance(exc, TaskbookLockedError):
        raise _error(status.HTTP_409_CONFLICT, exc.code, exc.message, exc.detail) from exc
    code = (
        status.HTTP_404_NOT_FOUND
        if exc.code in ("taskbook_not_found", "idea_not_found")
        else default_status
    )
    raise _error(code, exc.code, exc.message, exc.detail) from exc


class TaskbookCreate(BaseModel):
    """``POST /taskbooks`` 入参（附录 A.4 字段）。"""

    project_id: int = Field(..., description="所属项目")
    idea_id: int = Field(..., description="依据的 idea（锁定前必须已绑定证据）")
    research_question: str = Field(..., min_length=1)
    target_datasets: list[str] = Field(default_factory=list)
    baselines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    compute_budget: dict[str, Any] | None = Field(
        default=None,
        description=(
            "成本预算：{max_llm_cost_usd, demo_cost_quota_usd, max_stage_minutes}；"
            "max_llm_cost_usd 超过硬护栏会被 422 拒绝（不可放宽）"
        ),
    )
    deliverables: list[str] | None = Field(
        default=None, description="交付形态：" + " / ".join(DELIVERABLE_OPTIONS)
    )
    rounds: dict[str, Any] | None = Field(
        default=None,
        description="轮次配置：{max_iterations, score_threshold, marginal_gain_threshold, max_retry}",
    )


class TaskbookPatch(BaseModel):
    """``PATCH /taskbooks/{id}`` 入参（全部可选，锁定后不可用）。"""

    research_question: str | None = None
    target_datasets: list[str] | None = None
    baselines: list[str] | None = None
    metrics: list[str] | None = None
    compute_budget: dict[str, Any] | None = None
    deliverables: list[str] | None = None
    max_iterations: int | None = None
    score_threshold: float | None = None
    marginal_gain_threshold: float | None = None
    max_retry: int | None = None


@router.post(
    "/taskbooks",
    summary="创建任务书（默认 draft，Owner）",
    dependencies=[Depends(require_owner)],
)
async def post_taskbook(body: TaskbookCreate, session: DbSession) -> dict[str, Any]:
    try:
        payload = await create_taskbook(
            session,
            project_id=body.project_id,
            idea_id=body.idea_id,
            research_question=body.research_question,
            target_datasets=body.target_datasets,
            baselines=body.baselines,
            metrics=body.metrics,
            compute_budget=body.compute_budget,
            deliverables=body.deliverables,
            rounds=body.rounds,
        )
    except TaskbookError as exc:
        _raise_taskbook_error(exc, default_status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        raise  # pragma: no cover - _raise_taskbook_error 一定抛异常
    payload["defaults"] = {"rounds": default_rounds(), "compute_budget": default_compute_budget()}
    return payload


@router.get("/taskbooks", summary="任务书列表（可按项目过滤）")
async def get_taskbooks(
    session: DbSession,
    project_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    items = await list_taskbooks(session, project_id=project_id, limit=limit)
    return {"items": items, "total": len(items), "project_id": project_id}


@router.get("/taskbooks/{taskbook_id}", summary="任务书详情")
async def get_taskbook_detail(taskbook_id: int, session: DbSession) -> dict[str, Any]:
    payload = await get_taskbook(session, int(taskbook_id))
    if payload is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "taskbook_not_found",
            f"任务书 {taskbook_id} 不存在",
            {"taskbook_id": int(taskbook_id)},
        )
    return payload


@router.patch(
    "/taskbooks/{taskbook_id}",
    summary="编辑任务书（已锁定返回 409，Owner）",
    dependencies=[Depends(require_owner)],
)
async def patch_taskbook(
    taskbook_id: int, body: TaskbookPatch, session: DbSession
) -> dict[str, Any]:
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "empty_patch",
            "PATCH 未提供任何可修改字段",
        )
    try:
        return await update_taskbook(session, int(taskbook_id), patch)
    except TaskbookError as exc:
        _raise_taskbook_error(exc, default_status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        raise  # pragma: no cover


@router.post(
    "/taskbooks/{taskbook_id}/lock",
    summary="锁定任务书为流水线唯一执行依据（Owner）",
    dependencies=[Depends(require_owner)],
)
async def post_lock(taskbook_id: int, session: DbSession) -> dict[str, Any]:
    try:
        payload = await lock_taskbook(session, int(taskbook_id))
    except TaskbookError as exc:
        _raise_taskbook_error(exc, default_status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        raise  # pragma: no cover
    logger.info("taskbook locked id=%s already=%s", taskbook_id, payload.get("already_locked"))
    return payload
