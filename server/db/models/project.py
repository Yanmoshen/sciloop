# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""项目域 ORM 模型（附录 A.1）—— ``projects`` 是全库第一张表。"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

BIGINT = BigInteger


class Project(Base):
    """项目：全库第一张表。

    ``taskbook_id`` 与 ``idea_id`` 的外键按附录 A.0 在迁移末段以
    ``ALTER TABLE ... ADD CONSTRAINT`` 补齐（``use_alter=True``）。
    """

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    idea_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey(
            "ideas.id", name="fk_projects_idea", ondelete="SET NULL", use_alter=True
        ),
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="DRAFT"
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="manual")
    current_iteration: Mapped[int | None] = mapped_column(Integer, server_default="0")
    settings: Mapped[Any | None] = mapped_column(JSONB)
    is_demo: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    #: 左栏「项目」分组里的归档位：归档后从主列表收起、进「已归档」折叠区（迁移 0007）
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    taskbook_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey(
            "taskbooks.id",
            name="fk_projects_taskbook",
            ondelete="SET NULL",
            use_alter=True,
        ),
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("mode IN ('manual','auto')", name="mode"),
    )
