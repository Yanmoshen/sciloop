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
"""实验与指标读取 API（WP11-T7；附录 B.5）。

======== ==============================================  ==========  ==============================
方法      路径                                            鉴权        说明
======== ==============================================  ==========  ==============================
GET      ``/experiments/templates``                      公开        模板注册表（含实现状态与参数 schema）
GET      ``/experiments/{experiment_id}/runs``           公开        某实验定义的全部 Run
GET      ``/experiments/runs/{run_id}/metrics``           公开        单 Run 的真实指标
GET      ``/experiments/runs/{run_id}/passport``          公开        单 Run 的 Experiment Passport
======== ==============================================  ==========  ==============================

全部为 GET，属 public_demo 面允许的读操作（``contracts.api_contract.public_demo_allowed``）。

本模块**同时负责注册 ``experiment`` 环节**：``app.main.ROUTER_REGISTRY`` 已预留
``app.api.v1.experiments``（模块不存在则跳过），因此这里的导入副作用就是环节注入点，
``main.py`` 无需任何改动。

诚实性：接口只回库内真实行；指标缺失即回空数组并给出 ``metric_rows=0``，**不补齐不估算**。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.db.models.pipeline import Experiment, ExperimentMetric, ExperimentRun
from app.db.session import AsyncSessionLocal

logger = logging.getLogger("sciloop.api.experiments")

router = APIRouter(prefix="/experiments", tags=["experiments"])


# --------------------------------------------------------------------------- #
# 环节注册（导入本模块即注入 experiment；main.py 保持零改动）
# --------------------------------------------------------------------------- #
def _register_experiment_stage() -> str:
    """注册 ``experiment`` 环节实现；失败不阻断 API 挂载，但会打警告。"""
    try:
        from app.services.pipeline.stages.experiment import register

        register()
        return "registered"
    except Exception as exc:  # noqa: BLE001 - 注册失败必须可见（环节缺失会被引擎显式报错）
        logger.warning("experiment 环节注册失败：%s", exc, exc_info=True)
        return f"failed:{type(exc).__name__}:{exc}"


STAGE_REGISTRATION = _register_experiment_stage()


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _db_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": "db_unavailable", "message": str(exc), "detail": None},
    )


def _series(value: Any) -> Any:
    """``Decimal`` → ``float``（JSON 友好；None 原样保留）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# --------------------------------------------------------------------------- #
# 模板注册表
# --------------------------------------------------------------------------- #
@router.get("/templates", summary="实验模板注册表（含实现状态与参数 schema）")
async def list_templates(
    implemented_only: Annotated[bool, Query(description="只看已实现模板（T1/T3）")] = False,
) -> dict[str, Any]:
    """模板白名单与 P0 实现状态（``contracts.enums.template_id`` / ``template_p0``）。"""
    from app.services.experiment import registry as registry_mod

    items = registry_mod.list_templates(implemented_only=implemented_only)
    return {
        "items": items,
        "total": len(items),
        "registry": registry_mod.registry_snapshot(),
        "note": "T2/T4/T5 为 P1 占位条目，执行时会显式抛未实现（不静默返回空结果）",
    }


# --------------------------------------------------------------------------- #
# Run 列表
# --------------------------------------------------------------------------- #
@router.get("/runs/{run_id}/metrics", summary="单 Run 的真实指标明细")
async def get_run_metrics(run_id: int) -> dict[str, Any]:
    """按 ``contracts.enums`` 口径回指标行（``experiment_metrics``）。"""
    from app.services.experiment import metrics as M

    try:
        if AsyncSessionLocal is None:
            raise RuntimeError("DATABASE_URL 未就绪")
        async with AsyncSessionLocal() as session:
            exists = (
                await session.execute(select(ExperimentRun.id).where(ExperimentRun.id == int(run_id)))
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "run_not_found", "message": f"Run {run_id} 不存在", "detail": None},
                )
            rows = await M.load_run_metrics(run_id=int(run_id), session=session)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取实验指标失败 run_id=%s", run_id)
        raise _db_unavailable(exc) from exc

    return {
        "run_id": int(run_id),
        "items": rows,
        "total": len(rows),
        "metric_rows": len(rows),
        "note": "指标值取自 experiment_metrics（数值来自真实执行结果，接口不做补齐或估算）",
    }


@router.get("/runs/{run_id}/passport", summary="单 Run 的 Experiment Passport")
async def get_run_passport(run_id: int) -> dict[str, Any]:
    """返回该 Run 最新一条 Passport（一次 Run 至多一条；无则 404）。"""
    from app.services.experiment import passport as P

    try:
        if AsyncSessionLocal is None:
            raise RuntimeError("DATABASE_URL 未就绪")
        async with AsyncSessionLocal() as session:
            row = await P.get_passport_by_run(int(run_id), session=session)
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取 Passport 失败 run_id=%s", run_id)
        raise _db_unavailable(exc) from exc
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "passport_not_found",
                "message": f"Run {run_id} 暂无 Passport",
                "detail": {"run_id": int(run_id)},
            },
        )
    return row


@router.get("/{experiment_id}/runs", summary="某实验定义的全部 Run")
async def list_experiment_runs(
    experiment_id: int,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """分页返回 Run（含指标行数与 Passport 关联，便于前端一跳展示）。"""
    from app.db.models.review import ExperimentPassport

    try:
        if AsyncSessionLocal is None:
            raise RuntimeError("DATABASE_URL 未就绪")
        async with AsyncSessionLocal() as session:
            experiment = (
                await session.execute(select(Experiment).where(Experiment.id == int(experiment_id)))
            ).scalar_one_or_none()
            if experiment is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "experiment_not_found",
                        "message": f"实验定义 {experiment_id} 不存在",
                        "detail": None,
                    },
                )
            total = int(
                (
                    await session.execute(
                        select(func.count(ExperimentRun.id)).where(
                            ExperimentRun.experiment_id == int(experiment_id)
                        )
                    )
                ).scalar_one()
                or 0
            )
            rows = (
                (
                    await session.execute(
                        select(ExperimentRun)
                        .where(ExperimentRun.experiment_id == int(experiment_id))
                        .order_by(ExperimentRun.id)
                        .offset((int(page) - 1) * int(page_size))
                        .limit(int(page_size))
                    )
                )
                .scalars()
                .all()
            )
            run_ids = [int(row.id) for row in rows]
            metric_counts: dict[int, int] = {}
            passports: dict[int, dict[str, Any]] = {}
            if run_ids:
                metric_counts = {
                    int(run_id): int(count)
                    for run_id, count in (
                        await session.execute(
                            select(
                                ExperimentMetric.experiment_run_id, func.count(ExperimentMetric.id)
                            )
                            .where(ExperimentMetric.experiment_run_id.in_(run_ids))
                            .group_by(ExperimentMetric.experiment_run_id)
                        )
                    ).all()
                }
                for passport in (
                    (
                        await session.execute(
                            select(ExperimentPassport)
                            .where(ExperimentPassport.experiment_run_id.in_(run_ids))
                            .order_by(ExperimentPassport.id)
                        )
                    )
                    .scalars()
                    .all()
                ):
                    passports[int(passport.experiment_run_id)] = {
                        "id": int(passport.id),
                        "passport_uid": str(passport.passport_uid),
                        "status": passport.status,
                        "is_replay": bool(passport.is_replay),
                        "parent_passport_id": (
                            int(passport.parent_passport_id) if passport.parent_passport_id else None
                        ),
                    }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取实验 Run 列表失败 experiment_id=%s", experiment_id)
        raise _db_unavailable(exc) from exc

    items = [
        {
            "id": int(row.id),
            "run_id": int(row.id),
            "experiment_id": int(row.experiment_id),
            "attempt": int(row.attempt or 1),
            "status": row.status,
            "executor_task_id": row.executor_task_id,
            "duration_ms": int(row.duration_ms) if row.duration_ms is not None else None,
            "artifact_path": row.artifact_path,
            "error": row.error,
            "created_at": _iso(row.created_at),
            "metric_rows": metric_counts.get(int(row.id), 0),
            "passport": passports.get(int(row.id)),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "page": int(page),
        "page_size": int(page_size),
        "experiment": {
            "id": int(experiment_id),
            "template_id": experiment.template_id,
            "created_at": _iso(experiment.created_at),
        },
    }


__all__ = ["STAGE_REGISTRATION", "router"]
