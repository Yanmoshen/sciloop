# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""共享契约基类：响应基类、分页与统一错误体。

本包（``schemas``）的定位
----------------------------

**对外契约与文档可引用层**，字段与附录 A（数据库设计）／附录 B（API 契约）对齐：

- 各 WP 既有的 router 仍是各自实现的 ``dict`` / ORM 直出，本包**不改造既有端点**，
  以避免并行开发期的大范围回归；
- 需要「有类型、可校验、可写进文档」的 JSON 口径时（新端点、SSE 载荷、测试断言、
  前端 ``src/api/*.ts`` 的类型对照）统一从本包 import，保证三方口径一致。

口径来源：``assets/tasks/contracts.json`` → ``api_contract.pagination`` /
``api_contract.error_shape``。
"""

from __future__ import annotations

from math import ceil
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DataSource",
    "ErrorBody",
    "PageParams",
    "Paginated",
    "SciLoopModel",
    "TaskAccepted",
]

T = TypeVar("T")

#: 数据来源标记（附录 B.1）：前端据此显示 ``DemoBadge``
DataSource = Literal["live", "snapshot", "replay"]


class SciLoopModel(BaseModel):
    """全包基类。

    - ``from_attributes``：允许直接从 SQLAlchemy 行对象构造（``Model.model_validate(row)``）；
    - ``protected_namespaces=()``：附录 A 里存在 ``model_id`` / ``model_config_id`` /
      ``model_scores`` 等字段，必须关闭 Pydantic v2 对 ``model_`` 前缀的保护；
    - ``extra='ignore'``：落库对象常带派生列（如 ``paper_cards.*`` 之外的版本视图列），
      静默忽略而不报错，避免契约层成为既有端点的阻断点。
    """

    model_config = ConfigDict(
        from_attributes=True,
        protected_namespaces=(),
        populate_by_name=True,
        extra="ignore",
    )


class PageParams(SciLoopModel):
    """分页入参：``?page=1&page_size=20``（contracts.api_contract.pagination）。"""

    page: int = Field(1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(20, ge=1, le=100, description="每页条数，上限 100")

    @property
    def offset(self) -> int:
        """SQL ``OFFSET``（``(page - 1) * page_size``）。"""
        return (self.page - 1) * self.page_size


class Paginated(SciLoopModel, Generic[T]):
    """统一分页响应体：``{items,total,page,page_size}``。"""

    items: list[T] = Field(default_factory=list)
    total: int = Field(0, ge=0, description="过滤后的总条数（非本页条数）")
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)

    @classmethod
    def create(
        cls,
        items: list[T],
        total: int,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> Paginated[T]:
        """便捷构造（端点里可 ``return Paginated[Paper].create(rows, total)``）。"""
        return cls(items=items, total=total, page=page, page_size=page_size)

    @property
    def pages(self) -> int:
        """总页数（``page_size`` 非法时为 0，不抛异常）。"""
        if self.page_size <= 0:
            return 0
        return ceil(self.total / self.page_size)

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


class ErrorBody(SciLoopModel):
    """统一错误体（``main._error_body`` 的同口径类型化版本）。

    HTTP 状态码由响应本身承载，body 只描述错误语义，故 ``code`` 是字符串
    （``validation_error`` / ``http_404`` / ``internal_error`` …）而非数字。
    """

    code: str = Field(description="机器可读错误码，如 validation_error / http_404")
    message: str = Field(description="面向人的一句话说明（中文，可直接展示）")
    detail: Any = Field(default=None, description="结构化补充信息（如校验错误明细）")


class TaskAccepted(SciLoopModel):
    """长任务受理响应（contracts.api_contract.long_task）。

    进度不在这里轮询，走 SSE ``GET /api/v1/stream/{project_id}``。
    """

    task_id: str
    status: Literal["accepted"] = "accepted"
    job: str | None = Field(default=None, description="任务名（与 app/tasks/jobs 下的模块同名）")
