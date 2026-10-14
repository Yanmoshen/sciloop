# Copyright 2026 SciLoop contributors
"""Twenty-expert Idea pipeline API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.models import Aggregation, IdeaPipelineRun
from db.session import AsyncSessionLocal
from services.ideation.expert_pipeline import (
    IdeaPipelineError,
    confirm_candidate,
    create_run,
    execute_run,
    get_run,
    list_runs,
)

router = APIRouter(tags=["idea-runs"])
_IDEA_WORKERS: set[asyncio.Task[None]] = set()
_IDEA_WORKER_LOCK = asyncio.Lock()


def _error(code: int, key: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message, "detail": detail})


async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:
        raise _error(503, "database_unavailable", "数据库会话工厂不可用")
    async with AsyncSessionLocal() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(_session)]


class IdeaRunCreate(BaseModel):
    paper_ids: list[int] | None = Field(default=None, min_length=1, max_length=20)
    aggregation_id: int | None = None
    project_id: int | None = None

    @model_validator(mode="after")
    def at_least_one_source(self) -> IdeaRunCreate:
        if not self.paper_ids and self.aggregation_id is None:
            raise ValueError("paper_ids 或 aggregation_id 至少提供一个")
        return self


class IdeaRunConfirm(BaseModel):
    candidate_id: int
    confirmed: bool = True
    note: str | None = Field(default=None, max_length=4000)


async def _run_worker(run_id: int) -> None:
    if AsyncSessionLocal is None:
        return
    # SQLite permits one writer at a time. Serialize run workers so progress
    # checkpoints from separate Idea runs cannot hold each other's locks.
    async with _IDEA_WORKER_LOCK, AsyncSessionLocal() as worker_session:
        await execute_run(worker_session, run_id)


def _track_worker(task: asyncio.Task[None]) -> None:
    _IDEA_WORKERS.discard(task)
    task.exception() if not task.cancelled() else None


def _schedule_worker(run_id: int) -> None:
    task = asyncio.create_task(_run_worker(run_id), name=f"idea-run-{run_id}")
    _IDEA_WORKERS.add(task)
    task.add_done_callback(_track_worker)


async def resume_running_idea_runs() -> int:
    """Resume runs left in ``running`` after an API process restart."""
    if AsyncSessionLocal is None:
        return 0
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(IdeaPipelineRun.id).where(IdeaPipelineRun.status == "running"))).scalars().all()
    scheduled = 0
    for run_id in rows:
        if any(task.get_name() == f"idea-run-{int(run_id)}" for task in _IDEA_WORKERS):
            continue
        _schedule_worker(int(run_id))
        scheduled += 1
    return scheduled


@router.post(
    "/idea-runs",
    summary="创建 20 专家 Idea 运行",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_owner)],
)
async def post_idea_run(body: IdeaRunCreate, background_tasks: BackgroundTasks, session: DbSession) -> dict[str, Any]:
    paper_ids = list(body.paper_ids or [])
    if not paper_ids and body.aggregation_id is not None:
        aggregation = (await session.execute(select(Aggregation).where(Aggregation.id == int(body.aggregation_id)))).scalars().first()
        if aggregation is None:
            raise _error(404, "aggregation_not_found", f"聚合 {body.aggregation_id} 不存在")
        paper_ids = [int(pid) for pid in (aggregation.paper_ids or [])]
    try:
        payload = await create_run(session, paper_ids=paper_ids, aggregation_id=body.aggregation_id, project_id=body.project_id)
    except IdeaPipelineError as exc:
        code = 404 if exc.code in {"unknown_paper_id", "aggregation_not_found"} else 422
        raise _error(code, exc.code, exc.message, exc.detail) from exc
    run_id = int(payload["run"]["id"])
    background_tasks.add_task(_schedule_worker, run_id)
    payload["run"]["status"] = "queued"
    return payload


@router.get("/idea-runs", summary="研究任务的 20 专家运行历史")
async def list_idea_runs(aggregation_id: int, session: DbSession) -> dict[str, Any]:
    """公开只读地返回一个研究任务的运行历史，前端默认使用最新一条。"""
    return {"items": await list_runs(session, aggregation_id=int(aggregation_id)), "aggregation_id": int(aggregation_id)}


@router.get("/idea-runs/{run_id}", summary="Idea 专家运行状态与产物")
async def get_idea_run(run_id: int, session: DbSession) -> dict[str, Any]:
    payload = await get_run(session, run_id)
    if payload is None:
        raise _error(404, "run_not_found", f"Idea run {run_id} 不存在")
    return payload


@router.post(
    "/idea-runs/{run_id}/confirm",
    summary="人工确认候选 Idea",
    dependencies=[Depends(require_owner)],
)
async def post_idea_run_confirm(run_id: int, body: IdeaRunConfirm, session: DbSession) -> dict[str, Any]:
    if not body.confirmed:
        raise _error(422, "confirmation_required", "取消确认不会推进研究流水线；请直接保留运行结果")
    try:
        return await confirm_candidate(session, run_id, body.candidate_id, note=body.note)
    except IdeaPipelineError as exc:
        code = 404 if exc.code in {"run_not_found", "candidate_not_found"} else 409
        raise _error(code, exc.code, exc.message, exc.detail) from exc


__all__ = ["router"]
