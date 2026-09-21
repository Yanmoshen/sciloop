# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""访问控制：``X-Owner-Token`` 校验（WP01 公共依赖）。

红线（contracts.api_contract.owner_header / forbidden_actions）：

- ``OWNER_TOKEN`` **只能**来自服务端环境变量，禁止写入前端包或数据库
- ``public_demo`` 面一律拒绝写操作；返回统一错误体 ``{code,message,detail}``
- 令牌比较使用 ``secrets.compare_digest``，避免时序侧信道
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

from core.config import get_settings

logger = logging.getLogger("sciloop.security")

OWNER_HEADER = "X-Owner-Token"


def _deny(message: str = "该操作仅限 Owner 面（需要有效的 X-Owner-Token）") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "owner_token_required", "message": message, "detail": None},
    )


def owner_token_matches(provided: str | None) -> bool:
    """常量时间比较令牌；未配置 ``OWNER_TOKEN`` 时一律视为不通过。"""
    expected = (get_settings().owner_token or "").strip()
    if not expected:
        logger.warning("OWNER_TOKEN 未配置：所有写操作将被拒绝（public_demo 面）")
        return False
    if not provided:
        return False
    return secrets.compare_digest(provided.strip(), expected)


def is_owner(request: Request) -> bool:
    """当前请求是否来自 Owner 面（不抛异常，供 UI 状态用）。"""
    return owner_token_matches(request.headers.get(OWNER_HEADER))


async def require_owner(
    x_owner_token: Annotated[str | None, Header(alias=OWNER_HEADER)] = None,
) -> None:
    """FastAPI 依赖：写操作必须携带有效 ``X-Owner-Token``，否则 403。"""
    if not owner_token_matches(x_owner_token):
        raise _deny()


# 兼容别名：其他工作包可能按下列名字引用
require_owner_token = require_owner
owner_required = require_owner


async def require_public_or_owner(
    x_owner_token: Annotated[str | None, Header(alias=OWNER_HEADER)] = None,
) -> bool:
    """返回是否 Owner 面：读操作放行，调用方据此决定是否暴露写能力。"""
    return owner_token_matches(x_owner_token)


__all__ = [
    "OWNER_HEADER",
    "is_owner",
    "owner_required",
    "owner_token_matches",
    "require_owner",
    "require_owner_token",
    "require_public_or_owner",
]
