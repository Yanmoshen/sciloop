# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""演示模式开关与 API（WP16-T5）。

端点
----
``GET  /demo/status``          三开关 + 快照/fixture/示例项目库存（**公开只读**）
``POST /demo/mode``            Owner：切换 snapshot / replay 并广播 ``demo_mode`` SSE
``POST /demo/projects/seed``   Owner：导入/续跑预置示例 Project（幂等，支持 ``dry_run``）

红线
----
- 两个 POST 都属于 ``contracts.api_contract.owner_only``，一律挂 ``require_owner``
- 状态响应只含布尔、计数与时间戳，**不含** OWNER_TOKEN / API Key / base_url 凭据
- 切换后立刻广播 ``demo_mode {snapshot, replay}``，使前端 DemoBadge 立即反映
  （``services.pipeline.sse`` 已订阅该事件）
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from core.security import require_owner
from services.demo.access import get_replay_limiter, public_demo_summary
from services.demo.state import get_demo_state, state_public_view

logger = logging.getLogger("sciloop.wp16.demo")

router = APIRouter(tags=["demo"])

OwnerDep = Annotated[None, Depends(require_owner)]


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
class DemoModeRequest(BaseModel):
    """演示开关切换请求（两个字段都可省略；``reset=true`` 回到环境变量默认）。"""

    snapshot: bool | None = Field(default=None, description="论文库是否读 demo 快照")
    replay: bool | None = Field(default=None, description="LLM 是否从 demo_fixtures 回放")
    reset: bool = Field(default=False, description="先清除覆盖，回到 DEMO_* 环境变量默认")


class SeedRequest(BaseModel):
    """示例 Project 种子请求。"""

    dry_run: bool = Field(default=False, description="只体检与规划、不写库")
    force: bool = Field(default=False, description="已存在示例项目时也继续执行（幂等续跑）")
    project_slug: str | None = Field(default=None, description="示例项目 slug（默认契约值）")


# --------------------------------------------------------------------------- #
# 状态聚合
# --------------------------------------------------------------------------- #
def _snapshot_inventory() -> dict[str, Any]:
    """快照库存（DB 不可用时如实返回 ok=false，不编造计数）。"""
    try:
        from services.demo.snapshot import snapshot_inventory

        return snapshot_inventory()
    except Exception as exc:  # noqa: BLE001 - 状态端点不得因库存查询失败而 500
        logger.warning("快照库存查询失败：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _fixture_inventory() -> dict[str, Any]:
    try:
        from services.demo.replay import fixture_inventory

        return fixture_inventory()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fixture 库存查询失败：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _demo_project_inventory() -> dict[str, Any]:
    """示例 Project 库存（``projects.is_demo IS TRUE``）。"""
    try:
        from services.demo.snapshot import demo_project_inventory

        return demo_project_inventory()
    except Exception as exc:  # noqa: BLE001
        logger.warning("示例项目库存查询失败：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def build_status(request: Request | None = None) -> dict[str, Any]:
    """``GET /demo/status`` 的响应体（也被 ``POST /demo/mode`` 复用）。"""
    state = state_public_view()
    limiter = get_replay_limiter()
    quota: dict[str, Any] = {"limit_per_hour": limiter.limit}
    if request is not None:
        try:
            from services.demo.access import _request_client_key

            peek = limiter.peek(_request_client_key(request))
            quota.update(
                {
                    "remaining": peek.remaining,
                    "retry_after_seconds": peek.retry_after_seconds,
                    "consumed_by_caller": max(0, peek.limit - peek.remaining),
                }
            )
        except Exception:  # noqa: BLE001 - 限额展示失败不影响状态端点
            quota["remaining"] = None
    return {
        **state,
        "public_demo": {**public_demo_summary(), "quota": quota},
        "inventory": {
            "snapshots": _snapshot_inventory(),
            "fixtures": _fixture_inventory(),
            "demo_projects": _demo_project_inventory(),
        },
        "data_source_expectation": state["demo_mode"],
        "notes": [
            *state["notes"],
            "GET /demo/status 为公开只读；POST /demo/mode 与 POST /demo/projects/seed 属 owner_only",
        ],
    }


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("/demo/status", summary="演示开关、快照与 fixture 库存")
async def demo_status(request: Request) -> dict[str, Any]:
    return build_status(request)


@router.post("/demo/mode", summary="切换 snapshot / replay（Owner）")
async def demo_mode(payload: DemoModeRequest, _: OwnerDep) -> dict[str, Any]:
    current = get_demo_state()
    if payload.reset:
        before = {"snapshot": current.snapshot, "replay": current.replay}
        current.reset()
        changes = {"before": before, "after": {"snapshot": current.snapshot, "replay": current.replay}}
    else:
        changes = current.apply(snapshot=payload.snapshot, replay=payload.replay, actor="owner")

    # 立即广播：SSE 载荷固定为 {snapshot, replay}（contracts.sse_events.demo_mode）
    from llm.events import emit_demo_mode

    await emit_demo_mode(snapshot=current.snapshot, replay=current.replay)
    logger.info(
        "demo_mode 切换 before=%s after=%s", changes["before"], changes["after"]
    )
    return {
        "ok": True,
        "changed": changes,
        "status": build_status(None),
        "sse_event": {"event": "demo_mode", "payload": {"snapshot": current.snapshot, "replay": current.replay}},
    }


@router.post("/demo/projects/seed", summary="导入/续跑预置示例 Project（Owner）")
async def demo_seed(payload: SeedRequest, _: OwnerDep) -> dict[str, Any]:
    """幂等种子任务：重复调用只补齐缺失项，不重复造数据。

    ``dry_run=true`` 只做体检（列出已存在/待创建项）不写库，供演示前预检。
    """
    from tasks.jobs.seed_demo import run_seed_sync

    # 种子任务是同步阻塞 + 内部可能 asyncio.run，放进线程池执行，避免阻塞事件循环
    report = await asyncio.to_thread(
        run_seed_sync,
        dry_run=payload.dry_run,
        force=payload.force,
        project_slug=payload.project_slug,
    )
    logger.info(
        "seed_demo 完成 status=%s dry_run=%s created=%s",
        report.get("status"),
        payload.dry_run,
        report.get("counts", {}).get("created"),
    )
    return {"ok": report.get("status") != "failed", "dry_run": payload.dry_run, **report}


@router.get("/demo/projects", summary="示例项目列表（is_demo 优先，公开只读）")
async def demo_projects(
    include_all: bool = Query(False, description="true=包含非示例项目"),
) -> dict[str, Any]:
    """项目列表口径：``is_demo DESC`` 优先（WP16-T4 的「首屏即见成果」）。"""
    try:
        from services.demo.snapshot import list_projects

        return list_projects(include_all=include_all)
    except Exception as exc:  # noqa: BLE001
        logger.warning("项目列表查询失败：%s", exc)
        return {"ok": False, "items": [], "total": 0, "error": f"{type(exc).__name__}: {exc}"}
