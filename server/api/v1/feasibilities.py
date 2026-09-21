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
"""可行性端点（WP08-T5，附录 B.3）。

==========================================  ==================================================
``POST /feasibility``                       生成可行性报告（Owner）
``GET  /feasibility/{id}``                  取回四维分 + 风险 + MVE
``GET  /feasibility``                       按 ``?idea_id=`` 取最近一份报告
==========================================  ==================================================

响应体保证（WP08-A5 / A6）：

- ``dimensions[*]`` 必含 ``{score, rationale, evidence[], signals, formula}``；
  四维**每维都有打分依据**，``total_score`` 是显式加权和（``scoring.formula`` 可手算）；
- ``risk_list`` 的每条风险都带 ``level_basis``（规则）与 ``trigger``（观测值）；
- ``mve_plan`` 是可照做的步骤清单（真实端点 + 期望产出 + 模板建议 + 人工核对清单）。
- ``dimensions_without_evidence`` 非空时说明有维度未绑定到证据（如实暴露，不静默通过）。
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
from services.feasibility import (
    SAMPLE_SIZE_LIMIT,
    FeasibilityError,
    create_feasibility,
    get_feasibility,
    latest_feasibility_for_idea,
)

logger = logging.getLogger("sciloop.wp08.feasibility_api")

router = APIRouter(tags=["feasibility"])


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


class FeasibilityCreate(BaseModel):
    """``POST /feasibility`` 入参。"""

    idea_id: int = Field(..., description="目标 idea（必须已绑定证据，否则不建议进入执行链）")
    project_id: int | None = Field(
        default=None, description="项目 id；为空则取 idea.project_id（用于成本护栏口径）"
    )
    aggregation_id: int | None = Field(
        default=None, description="覆盖 idea 关联的聚合（手动 idea 无聚合时可显式指定）"
    )
    paper_ids: list[int] | None = Field(
        default=None, description="覆盖取数论文集合（给出了就不用聚合的 paper_ids）"
    )
    sample_size: int = Field(
        default=20, ge=1, le=SAMPLE_SIZE_LIMIT, description=f"MVE 样本量上限 {SAMPLE_SIZE_LIMIT}"
    )
    use_llm: bool = Field(
        default=False,
        description=(
            "是否额外调用 LLM 做四维复核（只写 llm_suggestion，**不影响规则分**）；"
            "默认关闭：四维分可离线复现"
        ),
    )
    model_ref: str | None = Field(default=None, description="use_llm=true 时的显式模型")
    rounds: dict[str, Any] | None = Field(
        default=None,
        description="轮次/预算覆盖：{max_iterations, score_threshold, marginal_gain_threshold, max_retry, max_llm_cost_usd, demo_cost_quota_usd}",
    )


@router.post(
    "/feasibility",
    summary="生成可行性报告（四维分 + 风险 + MVE，Owner）",
    dependencies=[Depends(require_owner)],
)
async def post_feasibility(body: FeasibilityCreate, session: DbSession) -> dict[str, Any]:
    try:
        payload = await create_feasibility(
            session,
            idea_id=body.idea_id,
            project_id=body.project_id,
            aggregation_id=body.aggregation_id,
            paper_ids=body.paper_ids,
            sample_size=body.sample_size,
            rounds=body.rounds,
            use_llm=body.use_llm,
            model_ref=body.model_ref,
        )
    except FeasibilityError as exc:
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code in ("idea_not_found", "aggregation_not_found")
            else status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        raise _error(code, exc.code, exc.message, exc.detail) from exc
    logger.info(
        "feasibility idea=%s total=%s dims_without_evidence=%s",
        body.idea_id,
        payload.get("total_score"),
        payload.get("dimensions_without_evidence"),
    )
    return payload


@router.get("/feasibility", summary="按 idea_id 取最近的可行性报告")
async def get_feasibility_by_idea(
    session: DbSession,
    idea_id: int = Query(..., description="idea id"),
) -> dict[str, Any]:
    payload = await latest_feasibility_for_idea(session, int(idea_id))
    if payload is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "feasibility_not_found",
            f"idea {idea_id} 尚无可行性报告：请先 POST /feasibility",
            {"idea_id": int(idea_id)},
        )
    return payload


@router.get("/feasibility/{feasibility_id}", summary="可行性报告详情（四维分 + 风险 + MVE）")
async def get_feasibility_detail(feasibility_id: int, session: DbSession) -> dict[str, Any]:
    payload = await get_feasibility(session, int(feasibility_id))
    if payload is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "feasibility_not_found",
            f"可行性报告 {feasibility_id} 不存在",
            {"feasibility_id": int(feasibility_id)},
        )
    return payload
