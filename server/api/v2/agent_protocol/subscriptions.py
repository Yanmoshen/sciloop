# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""线程事件订阅与游标推进。

一个会话可以订阅多个线程；每个订阅各自维护游标（已投递到的最大事件序号）。
断线重连时客户端带上 ``after_sequence``，服务端从该序号之后**原样重放事件**，
不重新调用模型（验收书 §6）。

游标纪律：

- 序号只能单调前进，重复交付由客户端 reducer 再去重（双保险）；
- 订阅取消（``thread/unsubscribe``、线程关闭、连接关闭）都要有明确通知。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Subscription:
    """单个线程的订阅状态。"""

    thread_id: str
    cursor: int = 0
    limit: int | None = None
    delivered: int = 0
    started_at: str = ""

    def advance(self, sequence: int) -> None:
        """把游标推进到 ``sequence``（只前进，不后退）。"""
        if int(sequence) > self.cursor:
            self.cursor = int(sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "cursor": self.cursor,
            "limit": self.limit,
            "delivered": self.delivered,
            "started_at": self.started_at,
        }


@dataclass
class SubscriptionRegistry:
    """连接内的订阅集合。"""

    _subs: dict[str, Subscription] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def subscribe(
        self, thread_id: str, *, after_sequence: int = 0, limit: int | None = None, now: str = ""
    ) -> Subscription:
        with self._lock:
            existing = self._subs.get(thread_id)
            if existing is not None:
                # 重复订阅：保留较大的游标，避免重复投递已经交付过的事件
                existing.advance(after_sequence)
                if limit is not None:
                    existing.limit = limit
                return existing
            sub = Subscription(
                thread_id=thread_id,
                cursor=int(after_sequence),
                limit=limit,
                started_at=now,
            )
            self._subs[thread_id] = sub
            return sub

    def unsubscribe(self, thread_id: str) -> Subscription | None:
        with self._lock:
            return self._subs.pop(thread_id, None)

    def get(self, thread_id: str) -> Subscription | None:
        with self._lock:
            return self._subs.get(thread_id)

    def subscribed(self) -> list[str]:
        with self._lock:
            return sorted(self._subs)

    def clear(self) -> list[str]:
        with self._lock:
            thread_ids = sorted(self._subs)
            self._subs.clear()
            return thread_ids

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._subs[key].to_dict() for key in sorted(self._subs)]

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)

    def __contains__(self, thread_id: object) -> bool:
        with self._lock:
            return thread_id in self._subs


__all__ = ["Subscription", "SubscriptionRegistry"]
