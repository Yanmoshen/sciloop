# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""``/costs`` 路由层：成本双线汇总。

``GET /costs/summary`` 是**只读**接口（``public_demo`` 面可读），契约要求固定返回：

``{used_usd, limit_usd:8.0, quota_usd:3.0, quota_exceeded, breakdown_by_stage, replay_saved_usd}``

语义要点：

- ``used_usd`` **只含真实调用**；桩/验收夹具调用（``provider``/``model``/``purpose`` 命中
  ``stub|mock|fake|replay|acceptance|fixture|synthetic``）与回放调用（``is_replay=true``）
  保留原始 ``llm_call_logs.cost_usd`` 行以便审计，但分别汇入
  ``stub_saved_usd``/``stub_calls`` 与 ``replay_saved_usd``/``replay_calls``，不计入护栏
- ``breakdown_by_stage`` 为真实调用分环节金额；``breakdown_by_provider`` 额外给出
  每个 provider 的 ``used_usd``/``stub_saved_usd``/``replay_saved_usd`` 与调用数，
  用于把 ``used_usd`` 的每个来源追溯到原始行（口径说明见 ``notes``）
- ``quota_usd`` 超限时 ``quota_exceeded = true``，但**接口仍返回 200**（配额只告警不熔断）
- 缺少单价的**真实**调用会让 ``cost_complete=false`` 并给出 warning（禁止估算，如实披露）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.services.cost import accumulate, default_limits

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("/summary", summary="成本汇总（护栏 8.0 vs 演示配额 3.0 双线）")
async def get_costs_summary(project_id: int | None = Query(default=None)) -> dict:
    """返回指定项目的成本汇总；``project_id`` 省略时汇总全库（演示总览用）。"""
    summary = await accumulate(project_id)
    return summary.to_dict()


@router.get("/limits", summary="成本双线阈值（护栏值 / 演示配额）")
async def get_cost_limits() -> dict:
    limit, quota = default_limits()
    return {
        "limit_usd": limit,
        "quota_usd": quota,
        "rule": "limit_usd 为硬熔断线；quota_usd 为演示配额，只告警不熔断",
    }


__all__ = ["router"]
