# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排层端点（顶层字面前缀 ``/research``，规避参数路由抢占）。

==================================================================  ==========================
``GET    /research/nodes``                                          节点定义与规则目录（公开）
``GET    /research/conversations/{cid}/state``                      **本对话**的研究链状态
``POST   /research/conversations/{cid}/run``                        Owner；执行节点（SSE）
``POST   /research/conversations/{cid}/revert``                     Owner；研究者发起回退
``GET    /research/conversations/{cid}/transitions``                迁移留痕
``GET    /research/conversations/{cid}/preflight``                  预检记录
``POST   /research/conversations/{cid}/preflight``                  Owner；执行一次预检
``GET    /research/conversations/{cid}/access``                     执行授权模式
``PUT    /research/conversations/{cid}/access``                     Owner；切换授权模式
==================================================================  ==========================

口径（硬约束）
--------------
- **链挂在对话上**（一个对话一条链）。所以路由键是 ``conversation_id``，不再是 ``project_id``。
- **对话可以没有项目**：这时 ``project_id`` 为 ``None``，节点照样能跑；
  只是落在项目作用域的业务表（如 ``taskbooks``）会如实跳过并说明。
- **写接口一律 Owner 面**（``require_owner``）：匿名 → 403 ``owner_token_required``；只读端点公开。
- 路由前缀用**顶层字面段** ``/research``：项目里已被 ``/papers/feed``、``card-jobs``、
  ``overview`` 三次参数路由抢占坑过。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.security import require_owner
from db.session import get_async_db
from schemas.research import (
    AccessModeRequest,
    PreflightRunRequest,
    RevertRequest,
    RunNodeRequest,
)
from services import conversations as conversations_service
from services.research import graph, orchestrator, rules, store
from services.research import preflight as preflight_mod

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


def _conversation_project(conversation_id: str) -> int | None:
    """从会话文件里读出它归属的项目（可能没有）。

    不查库、不猜：会话文件就是对话的既成事实。读不到会话也返回 ``None``
    —— 链挂在对话上，节点不该因为「拿不到项目」而跑不了。
    """

    try:
        record = conversations_service.read(conversation_id)
    except Exception as exc:  # noqa: BLE001 - 会话文件不可读时按「无项目」处理
        logger.warning("读取会话失败 %s：%s", conversation_id, exc)
        return None
    if not record:
        return None
    value = record.get("project_id")
    return int(value) if isinstance(value, int) and value > 0 else None


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
# 只读：本对话的链状态
# --------------------------------------------------------------------------- #
@router.get(
    "/research/conversations/{conversation_id}/state", summary="本对话的研究链状态（公开只读）"
)
async def get_state(conversation_id: str, session: SessionDep) -> dict[str, Any]:
    project_id = _conversation_project(conversation_id)
    state = await orchestrator.chain_state(
        session, conversation_id=conversation_id, project_id=project_id
    )
    state["execution_access"] = (
        await _read_access(session, project_id) if project_id else "ask"
    )
    return state


@router.get(
    "/research/conversations/{conversation_id}/transitions", summary="迁移留痕（公开只读）"
)
async def get_transitions(
    conversation_id: str, session: SessionDep, limit: int = 50
) -> dict[str, Any]:
    rows = await store.list_transitions(session, conversation_id=conversation_id, limit=limit)
    return {
        "conversation_id": conversation_id,
        "count": len(rows),
        "transitions": rows,
        "total_reverts": await store.count_total_reverts(
            session, conversation_id=conversation_id
        ),
        "max_total_reverts": graph.MAX_TOTAL_REVERTS,
    }


@router.get(
    "/research/conversations/{conversation_id}/preflight", summary="预检记录（公开只读）"
)
async def get_preflights(
    conversation_id: str, session: SessionDep, limit: int = 10
) -> dict[str, Any]:
    project_id = _conversation_project(conversation_id)
    return {
        "conversation_id": conversation_id,
        "records": preflight_mod.list_preflights(conversation_id, project_id, limit=limit),
    }


# --------------------------------------------------------------------------- #
# 写：执行节点（SSE）
# --------------------------------------------------------------------------- #
@router.post(
    "/research/conversations/{conversation_id}/run",
    summary="执行研究节点（SSE：meta/node/attempt/validation/revert/migrated/done，Owner）",
    dependencies=[Depends(require_owner)],
)
async def run_node(
    conversation_id: str, payload: RunNodeRequest, session: SessionDep
) -> StreamingResponse:
    """执行一个研究节点。

    事件序列：

    - ``meta``       本次判定结果（自动判定到的节点 + 命中词）、授权模式、预算上限
    - ``node``       节点进入（含第几次进入 ``entry_index``）
    - ``attempt``    本轮尝试（第几次 / 共几次）
    - ``notice``     降级说明（如上游不接受结构化输出档位）
    - ``validation`` **未通过**：规则号 + 面向研究者的说明（L1 自动重跑，L2 驳回重跑）
    - ``revert``     模型建议回退且三类闸门通过（含必带信息）
    - ``migrated``   通过校验，进入下一节点
    - ``waiting_human`` 修复重试达上限，转人工
    - ``done``       本轮结束（含成本与调用次数）
    - ``error``      LLM 或会话层错误；**不伪造成功**

    **链挂在对话上**，所以这里不需要项目存在；项目只作为元信息随链记录。
    """

    from db.session import AsyncSessionLocal

    if AsyncSessionLocal is None:  # pragma: no cover - 数据库不可用
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "db_unavailable", "数据库会话不可用"
        )

    project_id = _conversation_project(conversation_id)

    async def event_source() -> AsyncIterator[str]:
        yield ": connected\n\n"
        try:
            async for event, data in orchestrator.run_node(
                AsyncSessionLocal,
                conversation_id=conversation_id,
                node=payload.node,
                text=payload.text,
                project_id=project_id,
            ):
                yield _sse(event, data)
        except Exception as exc:  # noqa: BLE001 - 流内异常也要如实上报
            logger.exception("研究节点流异常 conversation=%s", conversation_id)
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
    "/research/conversations/{conversation_id}/revert",
    summary="研究者发起回退（同一套闸门校验，Owner）",
    dependencies=[Depends(require_owner)],
)
async def request_revert(
    conversation_id: str, payload: RevertRequest, session: SessionDep
) -> dict[str, Any]:
    """回退申请。

    与模型建议的回退走**同一套闸门**：必带信息（重合证据 / 仍存差异 / 是否值得继续）
    不齐则返回 422 并列出缺项；次数达上限则返回 409 并说明是哪个上限。
    通过后无条件写迁移留痕（G3）。
    """

    revisit = await store.count_revisits(
        session, conversation_id=conversation_id, node=payload.target
    )
    total = await store.count_total_reverts(session, conversation_id=conversation_id)
    gate = graph.check_revert_gate(
        current_node=payload.from_node,
        target=payload.target,
        carried=payload.carried,
        retry_count=0,
        revisit_count=revisit,
        total_reverts=total,
    )
    if not gate.ok:
        code = (
            status.HTTP_409_CONFLICT
            if gate.gate == "G2"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise _error(code, f"gate_{gate.gate.lower()}_failed", gate.message, gate.to_dict())

    row = await store.record_transition(
        session,
        conversation_id=conversation_id,
        project_id=_conversation_project(conversation_id),
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
    "/research/conversations/{conversation_id}/preflight",
    summary="执行一次小规模预检（白名单 + 超时；装依赖/联网需 approved，Owner）",
    dependencies=[Depends(require_owner)],
)
async def run_preflight_endpoint(
    conversation_id: str, payload: PreflightRunRequest, session: SessionDep
) -> dict[str, Any]:
    """执行预检。

    记录由**程序生成**：退出码、耗时、日志路径都是实际执行的结果。
    节点契约里的 ``preflight`` 字段会被这份真实记录覆盖——模型无法代为声明。
    """

    project_id = _conversation_project(conversation_id)
    mode = await _read_access(session, project_id) if project_id else "ask"
    approved = bool(payload.approved) or mode == "trusted"

    result = await preflight_mod.run_preflight(
        conversation_id=conversation_id,
        project_id=project_id,
        command=payload.command,
        cwd=payload.cwd,
        timeout_s=payload.timeout_s,
        level=payload.level,
        approved=approved,
    )
    body = result.to_dict()
    body["execution_access"] = mode
    body["conversation_id"] = conversation_id
    if result.exit_code != 0 and not result.command:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "preflight_rejected",
            result.note or "预检被拒绝",
            body,
        )
    return body


# --------------------------------------------------------------------------- #
# 写：执行授权模式（项目级；无项目时固定 ask）
# --------------------------------------------------------------------------- #
async def _read_access(session: AsyncSession, project_id: int | None) -> str:
    if not project_id:
        return "ask"
    settings = await store.get_project_settings(session, project_id)
    mode = str(settings.get("execution_access") or "ask")
    return mode if mode in ("ask", "trusted") else "ask"


@router.get(
    "/research/conversations/{conversation_id}/access", summary="执行授权模式（公开只读）"
)
async def get_access(conversation_id: str, session: SessionDep) -> dict[str, Any]:
    project_id = _conversation_project(conversation_id)
    mode = await _read_access(session, project_id)
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "execution_access": mode,
        "note": "ask：每次预检都要确认；trusted：免确认。装依赖 / 联网始终需要显式授权。"
        + ("" if project_id else "本对话未挂项目，固定为 ask。"),
    }


@router.put(
    "/research/conversations/{conversation_id}/access",
    summary="切换执行授权模式（Owner；需对话已挂项目）",
    dependencies=[Depends(require_owner)],
)
async def put_access(
    conversation_id: str, payload: AccessModeRequest, session: SessionDep
) -> dict[str, Any]:
    project_id = _conversation_project(conversation_id)
    if not project_id:
        raise _error(
            status.HTTP_409_CONFLICT,
            "no_project",
            "本对话未挂项目，执行授权固定为「每次确认」；如需免确认请先把对话挂到项目下",
        )
    settings = await store.merge_project_settings(
        session, project_id, {"execution_access": payload.execution_access}
    )
    return {
        "conversation_id": conversation_id,
        "project_id": project_id,
        "execution_access": str(settings.get("execution_access")),
        "note": "已保存到项目设置；对话里可随时切回 ask",
    }


@router.get("/research/search-status", summary="联网检索服务状态（公开只读）")
async def search_status() -> dict[str, Any]:
    """搜索服务当前状态，给界面那个常驻标识用。

    **只看服务本身**（活着 / 没起来）：上游限流是常态，不该由这个接口表达，
    那种情况由每次检索的回报负责说清（服务没起 / 没外网 / 被挡，三种分开）。
    """

    from services.agent import web_search

    return await web_search.search_status()
