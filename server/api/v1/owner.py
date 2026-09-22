# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Owner 面端点（WP16-T1 / WP16-T5）。

做什么
------
``GET  /owner/session``                当前访问面（**公开只读**，供 OwnerBadge 常驻展示）
``POST /owner/verify``                 Owner 令牌自检（Owner 面；失败即 403）
``GET  /owner/audit/write-endpoints``  写路由审计 + ``contracts.owner_only`` 落实清单（Owner 面）

红线（contracts.forbidden_actions 第 4 条）
------------------------------------------
- 令牌只从环境变量 ``OWNER_TOKEN`` 读取，与请求头用 ``secrets.compare_digest`` 比较
- 本模块**从不回显**令牌（既不返回明文也不返回前后缀），只返回布尔与来源描述
- 审计输出不含任何密钥、不含主机内网地址
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request

from core.security import OWNER_HEADER, is_owner, require_owner
from services.demo.access import audit_routes, public_demo_summary
from services.demo.state import state_public_view

logger = logging.getLogger("sciloop.wp16.owner")

router = APIRouter(tags=["owner"])

OwnerDep = Annotated[None, Depends(require_owner)]

#: 对外公布的令牌来源说明（**永远不含令牌本身**）
TOKEN_SOURCE_NOTE = (
    "OWNER_TOKEN 仅由服务端环境变量注入；写入前端包、数据库或日志均属红线违规"
)


def _write_permission(owner: bool, access_mode: str) -> dict[str, Any]:
    return {
        "writes_allowed": bool(owner),
        "reason": (
            "已携带有效 X-Owner-Token"
            if owner
            else f"匿名会话在 {access_mode} 面只读，写操作返回 403 owner_token_required"
        ),
    }


@router.get("/owner/session", summary="当前访问面（公开只读）")
async def owner_session(
    request: Request,
    x_owner_token: Annotated[str | None, Header(alias=OWNER_HEADER)] = None,
) -> dict[str, Any]:
    """返回当前访问面与写权限判定。

    **刻意公开**：前端 OwnerBadge 需要在未输入令牌时也能显示「public_demo 只读」。
    返回体只含布尔、访问面名称与令牌来源描述，不含令牌、不含任何密钥。
    """
    owner = is_owner(request)
    state = state_public_view()
    access_mode = str(state["access_mode"])
    return {
        "access_mode": access_mode,
        "is_owner": owner,
        "demo_mode": state["demo_mode"],
        "snapshot": state["snapshot"],
        "replay": state["replay"],
        "owner_header": OWNER_HEADER,
        "owner_token_source": TOKEN_SOURCE_NOTE,
        "permissions": _write_permission(owner, access_mode),
        "public_demo_summary": public_demo_summary(),
        "checked_at": state["updated_at"],
    }


@router.post("/owner/verify", summary="Owner 令牌自检（需 X-Owner-Token）")
async def owner_verify(_: OwnerDep) -> dict[str, Any]:
    """令牌有效才会走到这里（无效时由 ``require_owner`` 返回 403）。

    返回体**只**确认「有效」，不回显令牌内容或指纹。
    """
    state = state_public_view()
    return {
        "ok": True,
        "is_owner": True,
        "access_mode": state["access_mode"],
        "message": "研究者身份已启用：现在可以修改配置了（改动会留痕）。",
    }


@router.get(
    "/owner/audit/write-endpoints",
    summary="写路由审计与 owner_only 清单落实（需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def owner_write_endpoints(request: Request) -> dict[str, Any]:
    """静态审计：逐条列出写路由及其 Owner 守卫来源，并给出契约清单落实状态。"""
    audit = audit_routes(request.app)
    return {
        "ok": audit["unguarded_total"] == 0,
        **audit,
        "acceptance": {
            "smoke_case": "public_demo 匿名写操作被拒（401/403）",
            "runtime_evidence": (
                "bash server/fixtures/verify_public_demo_writes.sh "
                "（匿名 curl 逐条打 contracts.owner_only，输出状态码矩阵）"
            ),
        },
    }
