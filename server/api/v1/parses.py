# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""解析卡片端点（WP06-T5）。

====================================  ==========================================================
``GET  /papers/{id}/card``            单篇 8 字段卡片（``?version=`` 指定版本，默认最新）
``GET  /papers/{id}/cards``           版本列表（force 重解析后旧版本仍可查）
``POST /papers/{id}/card``            生成/重解析卡片（owner 面，长任务，返回 ``task_id``）
``GET  /papers/{id}/card-jobs/{task}`` 查询建卡任务状态（配套轮询口）
``GET  /papers/card-jobs``            最近的建卡任务（**静态字面路径**）
====================================  ==========================================================

挂载顺序（硬约束）
------------------
main.py 的 ``ROUTER_REGISTRY`` 把本模块注册在 ``api.v1.papers`` **之前**：
``/papers/card-jobs`` 是字面静态路径，若晚于 ``/papers/{paper_id}`` 注册会被参数路由
抢先匹配并返回 ``422 int_parsing``（路由虽注册但不可达）。详见 docs/README-工程.md §8
与 tests/.reports/p0_defects.json。

口径（硬约束）
--------------
- 卡片 8 字段全部非空；论文未报告的信息为 ``"unknown"``（如实标注，禁止编造）。
- 结论性条目带 ``evidence_span``（``paper_span`` + ``section`` + 字符区间 + ``quote_sha256``）；
  未定位条目 ``evidence_span=null`` 并计入 ``unlocated_count``。
- ``available_scope``：``fulltext``（``parse_status='ok'`` 且 ``coverage>=0.60``）或
  ``abstract_only``（摘要级降级，定位字段全为 null，前端 CoverageTag 据此显示）。
- 写操作仅 owner 面；错误体统一 ``{code,message,detail}``。

与 ``POST /papers/{id}/parse`` 的关系
------------------------------------
``POST /papers/{id}/parse`` 已由 WP05 的 ``api.v1.documents`` 实现（触发**全文解析**），
同一路径只会命中先注册者（本模块不定义该路径，故与 ``documents`` 无冲突）。
因此 WP06 的**建卡/重解析触发口**落在 ``POST /papers/{id}/card``（语义等价：owner + 长任务 +
``force``），避免同名路径双实现导致行为不一致（详见 ``_progress.json`` 的
``contract_changes_requested``）。标准流程：需要全文证据时先调 ``POST /papers/{id}/parse``，
解析完成后再调 ``POST /papers/{id}/card``。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.session import AsyncSessionLocal
from services.parsing import (
    CARD_BUILDER_VERSION,
    CARD_FIELDS,
    FULLTEXT_GATE_COVERAGE,
    SCOPE_ABSTRACT_ONLY,
    SqlCardRepository,
    get_card_task,
    list_card_tasks,
    submit_card_build,
)

logger = logging.getLogger("sciloop.wp06.parses")

router = APIRouter(tags=["parses"])

DEFAULT_TASK_LIMIT = 20
MAX_TASK_LIMIT = 100


# --------------------------------------------------------------------------------------
# 会话依赖与错误体（统一 {code,message,detail}）
# --------------------------------------------------------------------------------------
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


async def _require_paper(repository: SqlCardRepository, paper_id: int) -> None:
    """论文不存在 → 404（禁止对不存在的论文伪造卡片）。"""
    if await repository.get_paper(paper_id) is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "paper_not_found",
            f"论文 {paper_id} 不存在",
        )


def _card_payload(row: Any, *, computed: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = row.to_api_dict()
    meta = row.evidence_meta
    payload["fields"] = list(CARD_FIELDS)
    payload["evidence_meta"] = meta
    payload["coverage_tag"] = (
        "证据覆盖范围：仅摘要（abstract_only）"
        if payload["available_scope"] == SCOPE_ABSTRACT_ONLY
        else f"全文可用（覆盖 {round(float(payload.get('coverage') or 0) * 100)}%）"
    )
    payload["compliance_note"] = "本内容由 AI 辅助生成，需研究者自行核验"
    if computed:
        payload.update(computed)
    return payload


# --------------------------------------------------------------------------------------
# GET /papers/{id}/card
# --------------------------------------------------------------------------------------
@router.get("/papers/{paper_id}/card", summary="单篇论文的 8 字段解析卡片（默认最新版本）")
async def read_card(
    paper_id: int,
    session: DbSession,
    version: int | None = Query(None, ge=1, description="指定版本号；不传返回最新版本"),
    with_call_log: bool = Query(
        True, description="是否附带 llm_call_logs 详情（A6 卡片↔调用日志可追溯）"
    ),
) -> dict[str, Any]:
    """返回卡片 + 证据覆盖范围 + 定位统计 + LLM 调用追溯信息。"""
    repository = SqlCardRepository(session)
    await _require_paper(repository, paper_id)

    row = await repository.get_card(paper_id, version)
    if row is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "card_not_found",
            f"论文 {paper_id} 尚无解析卡片"
            + (
                f"（version={version}）"
                if version is not None
                else "，请先调用 POST /papers/{paper_id}/card"
            ),
            {"paper_id": paper_id, "version": version},
        )

    computed: dict[str, Any] = {}
    if with_call_log and row.llm_call_log_id is not None:
        log = await repository.get_llm_call_log(int(row.llm_call_log_id))
        computed["llm_call_log"] = log
        computed["llm_call_log_trace_url"] = (
            f"/api/v1/costs/summary?stage=parse&llm_call_log_id={row.llm_call_log_id}"
        )
        if log is not None:
            computed["llm_call_log_traceable"] = (
                log.get("stage") == "parse" and log.get("purpose") == "card_build"
            )
    else:
        computed["llm_call_log"] = None
        computed["llm_call_log_traceable"] = False

    versions = await repository.list_card_versions(paper_id)
    computed["versions"] = versions
    computed["is_latest"] = bool(versions) and int(versions[0]["version"]) == int(row.version)
    computed["fulltext_gate_threshold"] = FULLTEXT_GATE_COVERAGE
    computed["card_builder_version"] = CARD_BUILDER_VERSION
    # 全文总结速览（随卡片一起生成）：直接透出 evidence_meta 里那份，缺失即为 None ——
    # 老卡片（本功能上线前建的）没有它，前端据此显示「生成失败」，而不是编一段出来。
    computed["summary"] = row.evidence_meta.get("summary")
    logger.info(
        "read_card paper_id=%s version=%s scope=%s located=%s unlocated=%s",
        paper_id,
        row.version,
        row.available_scope,
        row.evidence_meta.get("located_count"),
        row.evidence_meta.get("unlocated_count"),
    )
    return _card_payload(row, computed=computed)


# --------------------------------------------------------------------------------------
# GET /papers/{id}/cards（版本列表）
# --------------------------------------------------------------------------------------
@router.get("/papers/{paper_id}/cards", summary="卡片版本列表（旧版本保留）")
async def read_card_versions(paper_id: int, session: DbSession) -> dict[str, Any]:
    repository = SqlCardRepository(session)
    await _require_paper(repository, paper_id)
    versions = await repository.list_card_versions(paper_id)
    return {
        "paper_id": paper_id,
        "items": versions,
        "total": len(versions),
        "latest_version": versions[0]["version"] if versions else None,
        "note": "force 重解析生成 version=MAX+1 的新版本，历史版本永久保留",
    }


# --------------------------------------------------------------------------------------
# POST /papers/{id}/card（长任务，owner 面）
# --------------------------------------------------------------------------------------
class CardBuildRequest(BaseModel):
    """``POST /papers/{id}/card`` 请求体。"""

    force: bool = Field(
        default=False,
        description="true=无视已有卡片生成 version+1 的新版本（旧版本保留）；false=文档版本未变则复用",
    )


@router.post(
    "/papers/{paper_id}/card",
    summary="生成/重解析解析卡片（长任务，owner 面）",
    dependencies=[Depends(require_owner)],
    status_code=status.HTTP_202_ACCEPTED,
)
async def build_card_endpoint(
    paper_id: int,
    session: DbSession,
    body: CardBuildRequest | None = None,
    force: bool = Query(False, description="等价于请求体 force=true"),
) -> dict[str, Any]:
    """后台触发建卡，返回 ``task_id``（结果以 ``GET /papers/{id}/card`` 为准）。"""
    request = body or CardBuildRequest()
    use_force = bool(request.force or force)

    repository = SqlCardRepository(session)
    await _require_paper(repository, paper_id)

    submitted = submit_card_build(paper_id=paper_id, force=use_force)
    task_id = submitted.get("task_id")
    logger.info("card_task_submitted task_id=%s paper_id=%s force=%s", task_id, paper_id, use_force)
    return {
        "paper_id": paper_id,
        "task_id": task_id,
        "job": submitted.get("job"),
        "status": submitted.get("status", "accepted"),
        "force": use_force,
        "poll_url": f"/api/v1/papers/{paper_id}/card-jobs/{task_id}",
        "card_url": f"/api/v1/papers/{paper_id}/card",
        "versions_url": f"/api/v1/papers/{paper_id}/cards",
        "progress_channel": "SSE /api/v1/stream/{project_id}",
        "note": (
            "本响应不含卡片内容（长任务口径）：请在 poll_url 观察任务状态，"
            "完成后用 card_url 读取；卡片内含 available_scope / unlocated_count / llm_call_log_id"
        ),
    }


@router.get("/papers/{paper_id}/card-jobs/{task_id}", summary="查询建卡任务状态")
async def card_job_status(paper_id: int, task_id: str, session: DbSession) -> dict[str, Any]:
    repository = SqlCardRepository(session)
    await _require_paper(repository, paper_id)

    task = get_card_task(task_id)
    if task is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"未找到建卡任务 {task_id}（本进程重启后任务登记会丢失，卡片本身仍可查）",
        )
    if int(task.get("paper_id") or 0) != int(paper_id):
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "task_not_found",
            f"建卡任务 {task_id} 不属于论文 {paper_id}",
        )
    return task


@router.get("/papers/card-jobs", summary="最近的建卡任务（排查用）")
async def recent_card_jobs(
    limit: int = Query(DEFAULT_TASK_LIMIT, ge=1, le=MAX_TASK_LIMIT),
) -> dict[str, Any]:
    items = list_card_tasks(limit)
    return {"items": items, "total": len(items), "note": "进程内任务登记，重启后清空"}


__all__ = [
    "CardBuildRequest",
    "router",
]
