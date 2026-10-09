# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""客户端幂等键缓存。

用途（验收书 §2「请求含客户端幂等键，断线重试不重复创建 Turn」）：

客户端在断线后重发同一条请求时复用同一个 ``idempotency_key``。服务端只执行一次，
之后的重发直接返回**首次结果**并标记 ``replayed=True``，因此不会产生第二个 Turn、
第二次审批结论或第二条记忆。

键的复用纪律：同一个键配不同载荷属于客户端缺陷，直接报 ``duplicate_request``，
因为「静默接受」会让重放语义变得不可信。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, ProtocolError

GLOBAL_SCOPE = "__global__"


@dataclass
class IdempotentEntry:
    """一条幂等记录。"""

    key: str
    scope: str
    fingerprint: str
    result: dict[str, Any] = field(default_factory=dict)
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "scope": self.scope,
            "fingerprint": self.fingerprint,
            "hits": self.hits,
        }


class IdempotencyStore:
    """线程安全的内存幂等缓存（进程内；服务重启后由事件流本身保证不重复）。"""

    def __init__(self, *, max_entries: int = 4096) -> None:
        self.max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], IdempotentEntry] = {}

    # ------------------------------------------------------------------ 查询
    def get(self, key: str | None, *, scope: str = GLOBAL_SCOPE) -> IdempotentEntry | None:
        if not key:
            return None
        with self._lock:
            return self._entries.get((scope, key))

    def lookup(
        self, key: str | None, *, scope: str = GLOBAL_SCOPE, fingerprint: str
    ) -> IdempotentEntry | None:
        """查既有结果。

        同一个键配不同载荷时抛 ``duplicate_request``——这是客户端缺陷，
        不能当作重放处理，否则会把「新请求」误当成「旧请求的重复」而丢副作用。
        """
        entry = self.get(key, scope=scope)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint:
            raise ProtocolError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"idempotency key {key!r} was already used with a different payload",
                data={
                    "idempotency_key": key,
                    "scope": scope,
                    "hint": "reuse of a key requires an identical payload",
                },
            )
        return entry

    # ------------------------------------------------------------------ 写入
    def record(
        self,
        key: str | None,
        *,
        result: dict[str, Any],
        fingerprint: str,
        scope: str = GLOBAL_SCOPE,
    ) -> IdempotentEntry | None:
        if not key:
            return None
        entry = IdempotentEntry(
            key=key, scope=scope, fingerprint=fingerprint, result=dict(result or {})
        )
        with self._lock:
            self._entries[(scope, key)] = entry
            self._evict_locked()
        return entry

    def hit(self, key: str | None, *, scope: str = GLOBAL_SCOPE) -> None:
        """记一次命中（指标用）。"""
        if not key:
            return
        with self._lock:
            entry = self._entries.get((scope, key))
            if entry is not None:
                entry.hits += 1

    def _evict_locked(self) -> None:
        """超出上限时按插入顺序淘汰最早的记录（不引入额外依赖）。"""
        overflow = len(self._entries) - self.max_entries
        if overflow <= 0:
            return
        for stale in list(self._entries)[:overflow]:
            self._entries.pop(stale, None)

    # ------------------------------------------------------------------ 维护
    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": sum(e.hits for e in self._entries.values()),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["GLOBAL_SCOPE", "IdempotentEntry", "IdempotencyStore"]
