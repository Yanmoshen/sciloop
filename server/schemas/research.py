# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排层的 API 请求模型（节点产出的契约在
``app/services/research/contracts.py``，这里只放 HTTP 边界）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from schemas.base import SciLoopModel

__all__ = [
    "AccessModeRequest",
    "PreflightRequest",
    "PreflightRunRequest",
    "RevertRequest",
    "RunNodeRequest",
]


class RunNodeRequest(SciLoopModel):
    """``POST /research/projects/{id}/run``。

    ``node`` 为空时按 ``text`` 自动判定（判定结果会在事件流里回显，
    研究者可据回显改为显式指定）。
    """

    node: str | None = Field(default=None, description="显式指定节点；为空则自动判定")
    text: str = Field(default="", description="研究者本轮输入，用于判定节点与检索")


class RevertRequest(SciLoopModel):
    """``POST /research/projects/{id}/revert``（研究者发起的回退）。

    与模型建议的回退走**同一套闸门**：必带信息不齐同样会被拒绝，
    并返回缺了哪几项。
    """

    from_node: str
    target: str
    reason: str = ""
    carried: dict[str, Any] = Field(default_factory=dict)


class PreflightRunRequest(SciLoopModel):
    """``POST /research/projects/{id}/preflight``。"""

    command: str
    cwd: str | None = None
    timeout_s: int = Field(default=120, ge=1, le=3600)
    level: Literal["template_smoke", "researcher_script", "isolated_runner"] = (
        "researcher_script"
    )
    approved: bool = Field(
        default=False,
        description="装依赖 / 联网类命令必须显式授权，否则拒绝执行",
    )


class PreflightRequest(SciLoopModel):
    """预检记录列表的查询（保留给后续过滤用）。"""

    limit: int = Field(default=10, ge=1, le=50)


class AccessModeRequest(SciLoopModel):
    """``PUT /research/projects/{id}/access``。

    ``ask``（默认）每次执行都要确认；``trusted`` 免确认（对话里可随时切回）。
    """

    execution_access: Literal["ask", "trusted"]
