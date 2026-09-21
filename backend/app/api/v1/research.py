# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排层端点（顶层字面前缀 ``/research``，规避参数路由抢占）。

==================================================================  ==========================
``GET    /research/nodes``                                          节点定义与规则目录（公开）
``GET    /research/projects/{id}/state``                            链状态 + 迁移记录
``POST   /research/projects/{id}/run``                              Owner；执行节点（SSE）
``POST   /research/projects/{id}/revert``                           Owner；研究者发起回退
``GET    /research/projects/{id}/transitions``                      迁移留痕
``GET    /research/projects/{id}/preflight``                        预检记录
``POST   /research/projects/{id}/preflight``                        Owner；执行一次预检
``GET    /research/projects/{id}/access``                           执行授权模式
``PUT    /research/projects/{id}/access``                           Owner；切换授权模式
==================================================================  ==========================

口径（硬约束）
--------------
- **写接口一律 Owner 面**（``require_owner``）：匿名 → 403 ``owner_token_required``；
  只读端点公开。与既有 ``reader`` / ``exports`` 模块一致。
- 路由前缀用**顶层字面段** ``/research``，不用参数起始路径——项目里已被
  ``/papers/feed``、``card-jobs``、``overview`` 三次参数路由抢占坑过。
- **执行授权模式**存在 ``projects.settings.execution_access``（现成 JSONB 列，零迁移）：
  默认 ``ask``（每次预检都要确认），``trusted`` 免确认；无论哪种，装依赖 / 联网
  一律要求 ``approved=true``——规则层先行判定，模型或前端都无法绕过。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_owner
from app.db.session import get_async_db
from app.schemas.research import (
    AccessModeRequest,
    PreflightRunRequest,
    RevertRequest,
    RunNodeRequest,
)
from app.services.research import graph, orchestrator, rules, store
from app.services.research import preflight as preflight_mod

logger = logging.getLogger("sciloop.research.api")

router = APIRouter(tags=["research"])

OwnerDep = Annotated[None, Depends(require_owner)]
SessionDep = Annotated[AsyncSession, Depends(get_async_db)]


def _error(code: int, key: str, message: str, detail: Any = None) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"code": key, "message": message, "detail": detail}
    )


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


async def _require_project(session: AsyncSession, project_id: int) -> None:
    from sqlalchemy import select

    from app.db.models.project import Project

    exists = (
        await session.execute(select(Project.id).where(Project.id == project_id))
    ).scalar_one_or_none()
    if exists is None:
        raise _error(status.HTTP_404_NOT_FOUND, "project_not_found", f"项目 {project_id} 不存在")


# --------------------------------------------------------------------------- #
# 只读：节点定义与规则目录
# --------------------------------------------------------------------------- #
@router.get("/research/nodes", summary="研究节点定义、允许的迁移边与校验规则（公开）")
async def list_nodes() -> dict[str, Any]:
    """节点定义与规则目录。

    规则目录是**面向研究者**的表述（不含内部字段名），界面据此说明
    「这个节点会检查什么」，不必再写一份文案。
    """

    nodes = []
    for node in graph.NODE_ORDER:
        nodes.append(
            {
                "node": node,
                "label": graph.NODE_LABELS.get(node, node),
                "implemented": graph.is_implemented(node),
                "next": graph.next_node(node),
                "revert_targets": list(graph.revert_targets(node)),
                "rules": rules.rule_catalog(node),
                "min_evidence_count": rules.min_evidence_count(node),
            }
        )
    return {
        "nodes": nodes,
        "max_retry": graph.MAX_RETRY_PER_ENTRY,
        "max_revisit": graph.MAX_REVISIT_PER_NODE,
        "max_total_reverts": graph.MAX_TOTAL_REVERTS,
        "gates": list(graph.GATE_ORDER),
        "revert_requirements": graph.REVERT_REQUIREMENTS,
        "status_display": graph.DOC_STATUS_DISPLAY,
        "retrospective": {
            "node": "research_retrospective",
            "label": graph.NODE_LABELS["research_retrospective"],
            "note": "收尾动作：任意节点都能触发，不占节点位",
        },
    }


# --------------------------------------------------------------------------- #
# 只读：链状态
# --------------------------------------------------------------------------- #
@router.get("/research/projects/{project_id}/state", summary="研究链状态（公开只读）")
async def get_state(project_id: int, session: SessionDep) -> dict[str, Any]:
    await _require_project(session, project_id)
    return await orchestrator.chain_state(session, project_id=project_id)


@router.get("/research/projects/{project_id}/transitions", summary="迁移留痕（公开只读）")
async def get_transitions(
    project_id: int,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    await _require_project(session, project_id)
    rows = await store.list_transitions(session, project_id=project_id, limit=limit)
    return {
        "project_id": project_id,
        "count": len(rows),
        "transitions": rows,
        "total_reverts": await store.count_total_reverts(session, project_id=project_id),
        "max_total_reverts": graph.MAX_TOTAL_REVERTS,
    }


@router.get("/research/projects/{project_id}/preflight", summary="预检记录（公开只读）")
async def get_preflights(
    project_id: int,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    await _require_project(session, project_id)
    return {"project_id": project_id, "records": preflight_mod.list_preflights(project_id, limit=limit)}


# --------------------------------------------------------------------------- #
# 写：执行节点（SSE）
# --------------------------------------------------------------------------- #
@router.post(
    "/research/projects/{project_id}/run",
    summary="执行研究节点（SSE：meta/node/attempt/validation/revert/migrated/done，Owner）",
    dependencies=[Depends(require_owner)],
)
async def run_node(project_id: int, payload: RunNodeRequest, session: SessionDep) -> StreamingResponse:
    """执行一个研究节点。

    事件序列：

    - ``meta``       本次判定结果（自动判定到的节点 + 命中词）、授权模式、预算上限
    - ``node``       节点进入（含第几次进入 ``entry_index``）
    - ``attempt``    本轮尝试（第几次 / 共几次）
    - ``validation`` **未通过**：规则号 + 面向研究者的说明（L1 自动重跑，L2 驳回重跑）
    - ``revert``     模型建议回退且三类闸门通过（含必带信息）
    - ``migrated``   通过校验，进入下一节点
    - ``waiting_human`` 修复重试达上限，转人工
    - ``done``       本轮结束（含成本与调用次数）
    - ``error``      LLM 或项目层错误；**不伪造成功**

    生成器的 ``finally`` 会把已产生的状态留在库里：断开连接也不会丢进度。
    """

    await _require_project(session, project_id)

    from app.db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 数据库不可用
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "db_unavailable", "数据库会话不可用"
        )

    async def event_source() -> AsyncIterator[str]:
        yield ": connected\n\n"
        try:
            async for event, data in orchestrator.run_node(
                AsyncSessionLocal, project_id=project_id, node=payload.node, text=payload.text
            ):
                yield _sse(event, data)
        except Exception as exc:  # noqa: BLE001 - 流内异常也要如实上报
            logger.exception("研究节点流异常 project=%s", project_id)
            yield _sse(
                "error",
                {"code": "stream_failed", "message": f"执行中断：{exc}"[:400]},
            )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------- #
# 写：回退（研究者发起，与模型建议同一套闸门）
# --------------------------------------------------------------------------- #
@router.post(
    "/research/projects/{project_id}/revert",
    summary="研究者发起回退（同一套闸门校验，Owner）",
    dependencies=[Depends(require_owner)],
)
async def request_revert(
    project_id: int, payload: RevertRequest, session: SessionDep
) -> dict[str, Any]:
    """回退申请。

    与模型建议的回退走**同一套闸门**：必带信息（重合证据 / 仍存差异 / 是否值得继续）
    不齐则返回 422 并列出缺项；次数达上限则返回 409 并说明是哪个上限。
    通过后无条件写迁移留痕（G3）。
    """

    await _require_project(session, project_id)

    revisit = await store.count_revisits(session, project_id=project_id, node=payload.target)
    total = await store.count_total_reverts(session, project_id=project_id)
    gate = graph.check_revert_gate(
        current_node=payload.from_node,
        target=payload.target,
        carried=payload.carried,
        retry_count=0,
        revisit_count=revisit,
        total_reverts=total,
    )
    if not gate.ok:
        code = status.HTTP_409_CONFLICT if gate.gate == "G2" else status.HTTP_422_UNPROCESSABLE_ENTITY
        raise _error(code, f"gate_{gate.gate.lower()}_failed", gate.message, gate.to_dict())

    row = await store.record_transition(
        session,
        project_id=project_id,
        from_node=payload.from_node,
        to_node=payload.target,
        kind="revert",
        trigger="researcher",
        reason=payload.reason or "研究者发起回退",
        required_carried=payload.carried,
        budget_snapshot={"revisit_count": revisit, "total_reverts": total},
        actor="researcher",
    )
    return {
        "ok": True,
        "gate": gate.to_dict(),
        "transition": row,
        "from_label": graph.NODE_LABELS.get(payload.from_node, payload.from_node),
        "to_label": graph.NODE_LABELS.get(payload.target, payload.target),
    }


# --------------------------------------------------------------------------- #
# 写：预检
# --------------------------------------------------------------------------- #
@router.post(
    "/research/projects/{project_id}/preflight",
    summary="执行一次小规模预检（白名单 + 超时；装依赖/联网需 approved，Owner）",
    dependencies=[Depends(require_owner)],
)
async def run_preflight_endpoint(
    project_id: int, payload: PreflightRunRequest, session: SessionDep
) -> dict[str, Any]:
    """执行预检。

    记录由**程序生成**：退出码、耗时、日志路径都是实际执行的结果。
    节点契约里的 ``preflight`` 字段会被这份真实记录覆盖——模型无法代为声明。
    """

    await _require_project(session, project_id)
    settings = await store.get_project_settings(session, project_id)
    mode = str(settings.get("execution_access") or "ask")
    approved = bool(payload.approved) or mode == "trusted"

    result = await preflight_mod.run_preflight(
        project_id=project_id,
        command=payload.command,
        cwd=payload.cwd,
        timeout_s=payload.timeout_s,
        level=payload.level,
        approved=approved,
    )
    body = result.to_dict()
    body["execution_access"] = mode
    if result.exit_code != 0 and not result.command:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "preflight_rejected", result.note or "预检被拒绝", body)
    return body


# --------------------------------------------------------------------------- #
# 写：执行授权模式
# --------------------------------------------------------------------------- #
@router.get("/research/projects/{project_id}/access", summary="执行授权模式（公开只读）")
async def get_access(project_id: int, session: SessionDep) -> dict[str, Any]:
    await _require_project(session, project_id)
    settings = await store.get_project_settings(session, project_id)
    mode = str(settings.get("execution_access") or "ask")
    return {
        "project_id": project_id,
        "execution_access": mode if mode in ("ask", "trusted") else "ask",
        "note": "ask：每次预检都要确认；trusted：免确认。装依赖 / 联网始终需要显式授权。",
    }


@router.put(
    "/research/projects/{project_id}/access",
    summary="切换执行授权模式（Owner）",
    dependencies=[Depends(require_owner)],
)
async def put_access(
    project_id: int, payload: AccessModeRequest, session: SessionDep
) -> dict[str, Any]:
    await _require_project(session, project_id)
    settings = await store.merge_project_settings(
        session, project_id, {"execution_access": payload.execution_access}
    )
    return {
        "project_id": project_id,
        "execution_access": str(settings.get("execution_access")),
        "note": "已保存到项目设置；对话里可随时切回 ask",
    }
