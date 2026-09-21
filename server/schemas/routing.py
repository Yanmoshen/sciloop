# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""模型路由契约：``stage_model_routing``（附录 A.6 + 附录 B.5 ``/models/routing``）。

路由是**盲评隔离**的落地基础（附录 D.7）：``plan`` 与 ``plan_review`` 必须指向
不同 ``model_config_id``／不同模型，否则盲评环节判失败。API Key 一律不在此处出现
（只读环境/加密列，响应必须脱敏）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.base import SciLoopModel

__all__ = ["ModelConfigBrief", "Routing", "RoutingUpdateRequest"]


class ModelConfigBrief(SciLoopModel):
    """``model_configs`` 中对外的脱敏视图（**绝不含 api_key / api_key_enc**）。"""

    id: int
    name: str
    base_url: str
    models: list[dict[str, object]] = Field(
        default_factory=list,
        description="[{model_id, label, context_window, pricing.input/output.{currency, perMillionTokens}}]",
    )
    is_default: bool = False
    last_tested_at: datetime | None = None
    test_ok: bool | None = None
    created_at: datetime | None = None
    type: str | None = None


class Routing(SciLoopModel):
    """``stage_model_routing``：``project_id=null`` 表示全局默认。"""

    id: int | None = None
    project_id: int | None = None
    stage: str = Field(
        description="survey | plan | plan_review | experiment | writing | review | decide"
    )
    purpose: str | None = Field(
        default=None, description="同名 stage 下的细分用途（如 card_build）"
    )
    model_config_id: int
    model_id: str
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = None
    created_at: datetime | None = None


class RoutingUpdateRequest(SciLoopModel):
    """``PUT /models/routing``（Owner）。"""

    items: list[dict[str, object]] = Field(
        default_factory=list,
        description="[{stage, purpose?, model_config_id, model_id, temperature?, max_tokens?}]",
    )
