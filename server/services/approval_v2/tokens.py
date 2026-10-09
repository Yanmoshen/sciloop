# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""审批令牌（Agent 2 / WP-03）。

安全约束（文档 WP-03 明确要求）：

- 令牌只存在**服务端内存或加密持久化**中，不放进模型消息、工具 input、前端可编辑字段或日志正文；
- 令牌必须**绑定 thread / turn / call / 工具 / 过期时间**，四项任一不符即无效；
- 一次性核销（重复使用返回 None）；
- 审计输出默认**打码**，避免日志正文泄漏。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

#: 令牌默认有效期（秒）
DEFAULT_TOKEN_TTL_S = 900.0


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def shift_iso(iso: str, *, seconds: float) -> str:
    """ISO 时间 + 秒数（保持 Z 结尾的毫秒精度格式）。"""
    moment = _parse(iso)
    if moment is None:  # pragma: no cover - 时间格式异常时原样返回
        return iso
    shifted = moment + timedelta(seconds=seconds)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S.") + f"{shifted.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class ApprovalToken:
    """一次审批令牌（绑定四要素 + 过期时间）。"""

    token: str
    approval_id: str
    thread_id: str
    turn_id: str
    call_id: str | None
    tool: str
    issued_at: str
    expires_at: str
    used: bool = False

    def redacted(self) -> str:
        """审计/日志用的打码形式（**绝不输出原值**）。"""
        return f"{self.token[:3]}***{self.token[-2:]}" if len(self.token) > 6 else "***"

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "call_id": self.call_id,
            "tool": self.tool,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "used": self.used,
            "token": self.redacted() if redact else self.token,
        }


class ApprovalTokenStore:
    """令牌的内存存储（服务端私有）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tokens: dict[str, ApprovalToken] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._tokens)

    def issue(
        self,
        *,
        approval_id: str,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
        tool: str,
        now_iso: str,
        ttl_s: float = DEFAULT_TOKEN_TTL_S,
    ) -> ApprovalToken:
        token = ApprovalToken(
            token=secrets.token_urlsafe(24),
            approval_id=approval_id,
            thread_id=thread_id,
            turn_id=turn_id,
            call_id=call_id,
            tool=tool,
            issued_at=now_iso,
            expires_at=shift_iso(now_iso, seconds=ttl_s),
        )
        with self._lock:
            self._tokens[token.token] = token
        return token

    def verify(
        self,
        token: str,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
        tool: str,
        now_iso: str,
    ) -> ApprovalToken | None:
        """四要素 + 过期 + 未使用全部满足才返回令牌，否则 None。"""
        with self._lock:
            record = self._tokens.get(token)
        if record is None or record.used:
            return None
        if record.thread_id != thread_id or record.turn_id != turn_id:
            return None
        if record.call_id != call_id or record.tool != tool:
            return None
        expiry = _parse(record.expires_at)
        moment = _parse(now_iso)
        if expiry is not None and moment is not None and moment > expiry:
            return None
        return record

    def consume(
        self,
        token: str,
        *,
        thread_id: str,
        turn_id: str,
        call_id: str | None,
        tool: str,
        now_iso: str,
    ) -> ApprovalToken | None:
        """核销（一次性）。校验通过才标记 used。"""
        with self._lock:
            record = self.verify(
                token,
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
                tool=tool,
                now_iso=now_iso,
            )
            if record is None:
                return None
            consumed = ApprovalToken(
                token=record.token,
                approval_id=record.approval_id,
                thread_id=record.thread_id,
                turn_id=record.turn_id,
                call_id=record.call_id,
                tool=record.tool,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
                used=True,
            )
            self._tokens[token] = consumed
            return consumed

    def purge_expired(self, now_iso: str) -> int:
        """清理过期令牌，返回清理数量。"""
        moment = _parse(now_iso)
        removed = 0
        with self._lock:
            for key, record in list(self._tokens.items()):
                expiry = _parse(record.expires_at)
                if expiry is not None and moment is not None and moment > expiry:
                    del self._tokens[key]
                    removed += 1
        return removed

    def revoke(self, approval_id: str) -> int:
        with self._lock:
            keys = [key for key, record in self._tokens.items() if record.approval_id == approval_id]
            for key in keys:
                del self._tokens[key]
        return len(keys)

    def audit(self) -> list[dict[str, Any]]:
        """审计视图（令牌打码）。"""
        with self._lock:
            return [record.to_dict(redact=True) for record in self._tokens.values()]


__all__ = ["ApprovalToken", "ApprovalTokenStore", "DEFAULT_TOKEN_TTL_S", "shift_iso"]
