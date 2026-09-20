# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""全局设置服务层。

对外只暴露 :mod:`app.services.settings.store` 里的读写函数与 :class:`SettingsError`。
"""

from __future__ import annotations

from app.services.settings.store import (
    SCOPES,
    SettingsError,
    get_scope,
    list_scopes,
    put_scope,
)

__all__ = ["SCOPES", "SettingsError", "get_scope", "list_scopes", "put_scope"]
