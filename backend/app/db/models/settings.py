# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""全局设置的 ORM 模型（迁移 ``0005_app_settings``）。

一个 ``scope`` 一行，整块偏好存在 ``value``（JSONB）里：**新增一个设置项不需要迁移**。

不在本表存任何机密
------------------
``OWNER_TOKEN`` 只允许存在于服务端环境变量（见 ``.env.example`` 与
``app/api/v1/owner.py`` 的红线说明）。本表只放界面偏好这类非机密数据。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppSetting(Base):
    """全局设置：``scope`` 唯一，``value`` 为该 scope 的整块设置。"""

    __tablename__ = "app_settings"

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
