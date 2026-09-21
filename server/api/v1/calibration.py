# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""盲评校准 API（WP12-T5；附录 B.4）。

======== ==================================================  ==========  ==========================
方法      路径                                                鉴权        说明
======== ==================================================  ==========  ==========================
GET      ``/pipelines/{project_id}/review-calibration``      公开        候选顺序 + 匿名评审结果
                                                                        + 人工标签 + 一致率
POST     ``/pipelines/{project_id}/human-labels``             Owner       录入人工标签（≥3 条）
======== ==================================================  ==========  ==========================

``{project_id}`` 与 WP09 的 ``/pipelines/{pid}`` 口径一致（pid 即 project_id）。

本模块**同时负责注册 ``plan_review`` 环节**：``main.ROUTER_REGISTRY`` 已预留
``api.v1.calibration``（模块不存在则跳过），因此这里的导入副作用就是环节注入点，
``main.py`` 无需任何改动。

SSE：录入标签并算出一致率后发 ``review_calibrated``
（载荷 ``{sample_size, metric, value}``，与 ``contracts.sse_events`` 一致）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from core.security import require_owner
from services.review import calibration as calibration_mod

logger = logging.getLogger("sciloop.api.calibration")

router = APIRouter(prefix="/pipelines", tags=["calibration"])

OwnerDep = Annotated[None, Depends(require_owner)]


# --------------------------------------------------------------------------- #
# 环节注册（导入本模块即注入 plan_review；main.py 保持零改动）
# --------------------------------------------------------------------------- #
def _register_plan_review() -> str:
    """注册 ``plan_review`` 环节实现；失败不阻断 API 挂载，但会打警告。"""
    try:
        from services.pipeline.stages.plan_review import register

        register()
        return "registered"
    except Exception as exc:  # noqa: BLE001 - 注册失败必须可见（环节缺失会被引擎显式报错）
        logger.warning("plan_review 环节注册失败：%s", exc, exc_info=True)
        return f"failed:{type(exc).__name__}:{exc}"


STAGE_REGISTRATION = _register_plan_review()


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _http_error(exc: Exception) -> HTTPException:
    """异常 → 契约错误体 ``{code,message,detail}``。"""
    if isinstance(exc, calibration_mod.CalibrationError):
        http_status = {
            "calibration_not_found": status.HTTP_404_NOT_FOUND,
            "not_found": status.HTTP_404_NOT_FOUND,
            "invalid_human_label": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "db_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
        }.get(exc.code, status.HTTP_400_BAD_REQUEST)
        return HTTPException(status_code=http_status, detail=exc.to_dict())
    logger.exception("校准接口未预期异常")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "internal_error", "message": str(exc), "detail": None},
    )


# --------------------------------------------------------------------------- #
# GET /pipelines/{project_id}/review-calibration
# --------------------------------------------------------------------------- #
@router.get(
    "/{project_id}/review-calibration",
    summary="盲评校准详情（候选顺序 / 匿名评审结果 / 人工标签 / 一致率）",
)
async def review_calibration(project_id: int) -> dict[str, Any]:
    """返回 WP12 的全部可审计事实（公开面可读，含样本量与小样本说明）。"""
    try:
        row = await calibration_mod.latest_calibration_for_project(project_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    if row is None:
        return {
            "project_id": int(project_id),
            "status": calibration_mod.STATUS_PENDING,
            "sample_size": 0,
            "agreement_metric": None,
            "agreement_value": None,
            "confidence_interval": None,
            "candidate_order": [],
            "model_scores": [],
            "human_labels": [],
            "human_label_min": _settings_int("calibration_human_label_min", 3),
            "small_sample_threshold": calibration_mod.SMALL_SAMPLE_THRESHOLD,
            "stage_registration": STAGE_REGISTRATION,
            "note": (
                "该项目尚无 plan_review 盲评记录（尚未运行到该环节）。"
                "盲评要求 plan 与 plan_review 配置两个不同的模型；"
                "人工标签需 ≥3 条才会展示一致率。"
            ),
        }

    try:
        report = await calibration_mod.compute(int(row.id))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    report["project_id"] = int(project_id)
    report["human_label_min"] = _settings_int("calibration_human_label_min", 3)
    report["stage_registration"] = STAGE_REGISTRATION
    report["disclosure"] = _disclosure(report)
    return report


# --------------------------------------------------------------------------- #
# POST /pipelines/{project_id}/human-labels
# --------------------------------------------------------------------------- #
@router.post("/{project_id}/human-labels", summary="录入人工标签（Owner，≥3 条起算一致率）")
async def submit_human_labels(
    project_id: int,
    _owner: OwnerDep,
    payload: Annotated[dict[str, Any], Body(...)],
) -> dict[str, Any]:
    """人工标签录入：``{labels:[{candidate_alias|method_index, decision, 五维分…}], replace?}``。

    ``decision`` ∈ ``approve|revise|reject``；五维分可选（0-20 整数），给了才算 MAE。
    """
    labels = payload.get("labels")
    if not isinstance(labels, list) or not labels:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_human_label",
                "message": "labels 必须是非空数组："
                "[{candidate_alias|method_index, decision, novelty, feasibility, rigor, "
                "cost_reasonableness, risk_control}]",
                "detail": {"received": type(labels).__name__},
            },
        )

    try:
        report = await calibration_mod.submit_human_labels(
            project_id, labels, replace=bool(payload.get("replace"))
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc

    report["project_id"] = int(project_id)
    report["human_label_min"] = _settings_int("calibration_human_label_min", 3)
    report["disclosure"] = _disclosure(report)
    _publish_calibrated(project_id, report)
    return report


def _publish_calibrated(project_id: int, report: dict[str, Any]) -> None:
    """仅当状态为 ``calibrated`` 时发 ``review_calibrated``（契约要求三字段齐全）。"""
    if report.get("status") != calibration_mod.STATUS_CALIBRATED:
        logger.info(
            "一致率未达展示条件（status=%s, sample_size=%s），不发 review_calibrated 事件",
            report.get("status"),
            report.get("sample_size"),
        )
        return
    try:
        from services.pipeline import sse as sse_mod

        sse_mod.publish(
            "review_calibrated",
            {
                "sample_size": int(report.get("sample_size") or 0),
                "metric": report.get("agreement_metric"),
                "value": report.get("agreement_value"),
            },
            project_id=int(project_id),
        )
    except Exception as exc:  # noqa: BLE001 - 事件失败不影响已完成的计算与落库
        logger.warning("review_calibrated 事件发布失败：%s", exc, exc_info=True)


def _disclosure(report: dict[str, Any]) -> dict[str, Any]:
    """小样本披露（契约：必须展示 sample_size，小样本不得宣称统计显著）。"""
    sample_size = int(report.get("sample_size") or 0)
    threshold = calibration_mod.SMALL_SAMPLE_THRESHOLD
    return {
        "sample_size": sample_size,
        "small_sample_threshold": threshold,
        "is_small_sample": sample_size < threshold,
        "significance": (
            "not_established" if sample_size < threshold else report.get("significance") or "approximate"
        ),
        "statement": report.get("note")
        or (
            calibration_mod.NON_SIGNIFICANT_NOTE.format(n=sample_size, threshold=threshold)
            if sample_size < threshold
            else f"样本量 {sample_size}，一致率仅供参考，不等同于评审正确率。"
        ),
    }


def _settings_int(name: str, default: int) -> int:
    try:
        from core.config import get_settings

        return int(getattr(get_settings(), name, default) or default)
    except Exception:  # noqa: BLE001
        return default


__all__ = ["STAGE_REGISTRATION", "router"]
