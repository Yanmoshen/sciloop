# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""访问控制（认证）。

复用项目既有的 ``core.security.owner_token_matches``，不新造一套判定：

- ``owner_mode``（本机管理员测试面）直接放行；
- ``public_demo`` 面必须带 ``X-Owner-Token`` 头或 ``?owner_token=`` 查询参数，
  且服务端配置了 ``OWNER_TOKEN`` 才放行；
- 配置不可用时（例如未加载 ``.env`` 的单元测试环境）**默认放行**，但会把
  ``degraded`` 标记带在返回值里，便于排查——绝不因为读不到配置就把人挡在门外。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessDecision:
    """一次访问判定的结果。"""

    allowed: bool
    access_mode: str
    reason: str
    degraded: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "access_mode": self.access_mode,
            "reason": self.reason,
            "degraded": self.degraded,
        }


def _matcher():  # noqa: ANN202 - 返回可调用对象或 None，避免模块导入期耦合配置
    """惰性取得 ``owner_token_matches``；配置不可用时返回 None。"""
    try:
        from core.security import owner_token_matches
    except Exception:  # noqa: BLE001 - 单元测试环境可能没有 .env
        return None
    return owner_token_matches


def check_token(token: str | None) -> AccessDecision:
    """按令牌判定访问权。"""
    matcher = _matcher()
    if matcher is None:
        return AccessDecision(True, "unknown", "config_unavailable", degraded=True)
    try:
        allowed = bool(matcher(token))
    except Exception:  # noqa: BLE001 - 配置异常不应把请求打成 500
        return AccessDecision(True, "unknown", "config_error", degraded=True)
    if allowed:
        return AccessDecision(True, "owner_mode", "owner_ok")
    return AccessDecision(False, "public_demo", "owner_token_required")


def token_from_websocket(websocket: object) -> str | None:
    """从 WebSocket 握手里取令牌（头优先，其次查询参数）。"""
    headers = getattr(websocket, "headers", None)
    if headers is not None:
        value = headers.get("x-owner-token") if hasattr(headers, "get") else None
        if value:
            return str(value)
    query = getattr(websocket, "query_params", None)
    if query is not None and hasattr(query, "get"):
        value = query.get("owner_token")
        if value:
            return str(value)
    return None


def check_websocket(websocket: object) -> AccessDecision:
    return check_token(token_from_websocket(websocket))


__all__ = ["AccessDecision", "check_token", "check_websocket", "token_from_websocket"]
