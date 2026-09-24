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
"""idea 端点（WP08-T4，附录 B.2）。

==========================================  ==================================================
``POST /ideas/generate``                    基于聚合生成 idea（Owner）→ **丢弃无证据条目**
``POST /ideas``                             手动创建 idea（Owner，``origin='user_input'``）
``GET  /ideas``                             idea 列表（``?project_id=``）
``GET  /ideas/{id}``                        单条 idea（含已绑定证据）
``GET  /ideas/{id}/evidences``              证据列表（附录 B.2 同名端点）
``POST /ideas/{id}/evidences``              为 idea 绑定证据（Owner；WP08-A4 手动流程需要）
``POST /ideas/{id}/select``                 选中 idea（Owner；同项目内互斥）
==========================================  ==================================================

**硬校验（服务端，非前端过滤）**：``/ideas/generate`` 返回的每条 idea 必然
``evidences.length >= 1``；无证据的条目会被删除并以 ``logger.warning`` 记录，
响应体 ``discarded`` 字段如实回显被丢弃的 idea_id 与原因（见
:func:`services.ideation.evidence_binder.enforce_evidence_or_drop`）。

路由顺序：``/ideas/generate`` 必须声明在 ``/ideas/{idea_id}`` 之前，
否则 ``generate`` 会被当作路径参数尝试解析成整数并返回 422。
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
from services.ideation import (
    MAX_IDEAS,
    MECHANISMS,
    MODE_AUTO,
    MODE_LLM,
    MODE_TEMPLATE,
    IdeaError,
    IdeaGenerationError,
    bind_idea_evidences,
    create_manual_idea,
    generate_ideas,
    idea_evidences,
    list_ideas,
    load_idea,
    select_idea,
)

logger = logging.getLogger("sciloop.wp08.ideas_api")

router = APIRouter(tags=["ideas"])

MODES = (MODE_AUTO, MODE_LLM, MODE_TEMPLATE)
MODE_HELP = (
    "auto=优先 LLM，失败自动降级为规则模板并在 llm_error 中披露；"
    "llm=必须用 LLM，不可用即失败（拒绝冒充）；template=纯规则合成（不调用 LLM）"
)


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


class GenerateRequest(BaseModel):
    """``POST /ideas/generate`` 入参。"""

    aggregation_id: int = Field(..., description="聚合 id（其空白清单是 idea 的素材与证据来源）")
    count: int = Field(default=5, ge=1, le=MAX_IDEAS, description=f"期望条数 1–{MAX_IDEAS}")
    project_id: int | None = Field(default=None, description="可选：关联项目（用于成本护栏口径）")
    mode: str = Field(default=MODE_AUTO, description=MODE_HELP)
    model_ref: str | None = Field(
        default=None, description="显式模型 ``provider_slug:model_id``；为空按 stage 路由"
    )
    temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    # 默认不设上限（原先 default=2000 / le=8192，两处都是"悄悄截断"的来源）
    max_tokens: int | None = Field(default=None, gt=0)


class ManualIdeaRequest(BaseModel):
    """``POST /ideas`` 入参（附录 B.2：``{project_id, title, content}``）。"""

    title: str = Field(..., min_length=1, max_length=2000)
    content: str = Field(..., min_length=1)
    project_id: int | None = None
    aggregation_id: int | None = None
    mechanism: str | None = Field(default=None, description=" / ".join(MECHANISMS))
    novelty_note: str | None = None
    evidences: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "可选：创建时一并绑定证据（Word 白名单字段同 WP13 CANDIDATE_FIELDS）；"
            "为空则该 idea 以 needs_evidence=true 落库，需后续绑定后才能进入可行性"
        ),
    )


class BindEvidenceRequest(BaseModel):
    """``POST /ideas/{id}/evidences`` 入参。"""

    evidences: list[dict[str, Any]] = Field(..., min_length=1)
    replace: bool = Field(default=False, description="true = 先清空该 idea 既有证据再绑定")


@router.post(
    "/ideas/generate",
    summary="基于聚合生成 idea（无 Evidence 的条目一律丢弃，Owner）",
    dependencies=[Depends(require_owner)],
)
async def post_generate(body: GenerateRequest, session: DbSession) -> dict[str, Any]:
    if body.mode not in MODES:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "invalid_mode",
            f"mode='{body.mode}' 不在 {list(MODES)} 内",
            {"allowed": list(MODES), "help": MODE_HELP},
        )
    try:
        payload = await generate_ideas(
            session,
            aggregation_id=body.aggregation_id,
            count=body.count,
            project_id=body.project_id,
            mode=body.mode,
            model_ref=body.model_ref,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
    except IdeaGenerationError as exc:
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
        if exc.code in ("llm_unavailable", "cost_guardrail"):
            code = status.HTTP_503_SERVICE_UNAVAILABLE
        raise _error(code, exc.code, exc.message, exc.detail) from exc
    logger.info(
        "ideas generate aggregation=%s mode=%s generated=%s discarded=%s",
        body.aggregation_id,
        payload.get("generation_mode"),
        payload.get("generated_count"),
        payload.get("discarded_count"),
    )
    return payload


@router.post(
    "/ideas",
    summary="手动创建 idea（Owner；与 AI idea 走同一套证据契约）",
    dependencies=[Depends(require_owner)],
)
async def post_idea(body: ManualIdeaRequest, session: DbSession) -> dict[str, Any]:
    try:
        payload = await create_manual_idea(
            session,
            title=body.title,
            content=body.content,
            project_id=body.project_id,
            aggregation_id=body.aggregation_id,
            mechanism=body.mechanism,
            novelty_note=body.novelty_note,
            evidences=body.evidences,
        )
    except IdeaError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message, exc.detail) from exc
    return payload


@router.get("/ideas", summary="idea 列表")
async def get_ideas(
    session: DbSession,
    project_id: int | None = Query(None),
    aggregation_id: int | None = Query(None),
    origin: str | None = Query(None, description="ai_generated | user_input"),
    only_with_evidence: bool = Query(False, description="只返回已绑定证据的 idea"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return await list_ideas(
            session,
            project_id=project_id,
            aggregation_id=aggregation_id,
            origin=origin,
            only_with_evidence=only_with_evidence,
            page=page,
            page_size=page_size,
        )
    except IdeaError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, exc.code, exc.message, exc.detail) from exc


@router.get("/ideas/{idea_id}", summary="idea 详情（含已绑定证据）")
async def get_idea(idea_id: int, session: DbSession) -> dict[str, Any]:
    item = await load_idea(session, int(idea_id))
    if item is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "idea_not_found",
            f"idea {idea_id} 不存在",
            {"idea_id": int(idea_id)},
        )
    return {
        "item": item,
        "next_step": (
            "POST /feasibility {idea_id} 生成可行性报告"
            if item["has_evidence"]
            else "该 idea 尚无证据：POST /ideas/{id}/evidences 绑定后再进入可行性"
        ),
    }


@router.get("/ideas/{idea_id}/evidences", summary="idea 的证据列表")
async def get_idea_evidences(idea_id: int, session: DbSession) -> dict[str, Any]:
    try:
        return await idea_evidences(session, int(idea_id))
    except IdeaError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, exc.code, exc.message, exc.detail) from exc


@router.post(
    "/ideas/{idea_id}/evidences",
    summary="为 idea 绑定证据（Owner；走 WP13 哈希优先校验）",
    dependencies=[Depends(require_owner)],
)
async def post_idea_evidences(
    idea_id: int, body: BindEvidenceRequest, session: DbSession
) -> dict[str, Any]:
    try:
        payload = await bind_idea_evidences(
            session, int(idea_id), body.evidences, replace=body.replace
        )
    except IdeaError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "idea_not_found"
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise _error(code, exc.code, exc.message, exc.detail) from exc
    binding = payload["binding"]
    payload["bound_count"] = len(binding.get("bound_ids") or [])
    payload["rejected_count"] = binding.get("rejected_total")
    payload["gate"] = {
        "rule": "无 Evidence 的 idea 必须丢弃，不允许输出",
        "ok": bool(binding.get("bound_ids")),
    }
    return payload


@router.post(
    "/ideas/{idea_id}/select",
    summary="选中 idea（Owner；同项目内互斥）",
    dependencies=[Depends(require_owner)],
)
async def post_select(idea_id: int, session: DbSession) -> dict[str, Any]:
    try:
        return await select_idea(session, int(idea_id))
    except IdeaError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, exc.code, exc.message, exc.detail) from exc
