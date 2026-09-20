# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""全局设置端点（迁移 ``0005_app_settings``）。

做什么
------
``GET  /api/v1/settings``              一次拉取全部 scope（设置页首屏用，省 N 次请求）
``GET  /api/v1/settings/{scope}``      读一个 scope（**公开只读**，返回默认值 ⊕ 已存值）
``PUT  /api/v1/settings/{scope}``      部分更新一个 scope（**Owner 面**；匿名 403）

为什么用顶层字面前缀 ``/settings``
-----------------------------------
与四核心模块（``/translate`` ``/reader`` ``/exports``）同一约定：**不挂在 ``/papers/`` 下**，
从根上避免「静态段被 ``/papers/{paper_id}`` 抢占 → 422 int_parsing」这一类缺陷。
``/settings`` 是字面段，且本模块内唯一的参数段在它之后（``/settings/{scope}``），
因此 ``/settings`` 自身不会被参数路由吃掉。

红线
----
- 本模块**不接触任何机密**：返回体只有界面偏好；``OWNER_TOKEN`` 仍只从环境变量读
  （见 ``app/api/v1/owner.py``）。写入要求 Owner，匿名写一律 403 ``owner_token_required``。
- 未知 scope → 404 ``unknown_scope``；未知字段 → 422 ``unknown_setting_key``；
  取值非法 → 422 ``invalid_setting_value``。
  请求体不是 JSON 对象时，**FastAPI 的请求体校验会先拦下**并返回项目统一的
  ``422 validation_error``（实测 ``[1,2]`` 即此路径）；服务层的 ``invalid_body``
  分支只是纵深防御，正常请求走不到。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_owner
from app.db.session import AsyncSessionLocal
from app.services.settings import store
from app.services.settings.store import SettingsError

logger = logging.getLogger("sciloop.settings")

router = APIRouter(tags=["settings"])

OwnerDep = Annotated[None, Depends(require_owner)]


# --------------------------------------------------------------------------- #
# 会话依赖与统一错误体
# --------------------------------------------------------------------------- #
async def _session() -> AsyncIterator[AsyncSession]:
    if AsyncSessionLocal is None:  # pragma: no cover - 部署期驱动缺失
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
        status_code=status_code,
        detail={"code": code, "message": message, "detail": detail},
    )


@contextmanager
def _guard() -> Iterator[None]:
    """把服务层的 :class:`SettingsError` 映射成 ``{code,message,detail}`` + 其自带状态码。"""
    try:
        yield
    except SettingsError as exc:
        raise _error(exc.status_code, exc.code, exc.message, exc.detail) from exc


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("/settings", summary="全部设置分组（公开只读）")
async def list_all_settings(session: DbSession) -> dict[str, Any]:
    """一次拉取全部 scope：设置页首屏用它，避免按页各发一次请求。"""
    with _guard():
        items = await store.list_scopes(session)
    return {"items": items, "total": len(items)}


@router.get("/settings/{scope}", summary="读一个设置分组（公开只读）")
async def read_settings(scope: str, session: DbSession) -> dict[str, Any]:
    """返回该 scope 的 **默认值 ⊕ 已存值**，并附 ``defaults`` 供界面"恢复默认"用。

    ``defaults`` 一并返回是刻意的：前端不必自己维护一份默认值副本（否则两处漂移）。
    """
    with _guard():
        return await store.get_scope(session, scope)


@router.put(
    "/settings/{scope}",
    summary="更新一个设置分组（需 X-Owner-Token）",
    dependencies=[Depends(require_owner)],
)
async def update_settings(scope: str, patch: dict[str, Any], session: DbSession) -> dict[str, Any]:
    """**部分更新**：只覆盖请求体里出现的键，其余保持原值；返回写入后的完整值。

    是部分更新而不是整体替换，是为了支持"界面上只改了一个开关"这种最小提交，
    避免把并发会话里另一个开关的值顺手覆盖掉。
    """
    with _guard():
        return await store.put_scope(session, scope, patch)


__all__ = ["router"]
