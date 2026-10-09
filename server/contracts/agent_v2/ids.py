"""稳定 ID 生成与校验。

设计要点：
- **前缀区分类型**，便于日志与事件流里肉眼区分；
- **时间前缀固定 13 位十六进制毫秒**，同一类型内按字典序即按时间序，
  便于游标与快照对齐；
- `now_ms` / `rand` 可注入，保证测试可复现。
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Callable

#: 对象类型 -> ID 前缀
ID_PREFIXES: dict[str, str] = {
    "thread": "th",
    "turn": "tu",
    "item": "it",
    "call": "call",
    "event": "ev",
    "approval": "ap",
    "memory": "mem",
    "compaction": "cmp",
    "summary": "sum",
    "snapshot": "snap",
}

_ID_RE = re.compile(r"^(?P<prefix>[a-z]{2,4})_(?P<ts>[0-9a-f]{13})(?P<rand>[0-9a-f]{10})$")


def new_id(
    kind: str,
    *,
    now_ms: int | None = None,
    rand: Callable[[int], str] | None = None,
) -> str:
    """生成一个稳定 ID，形如 ``th_0000018f3c2a4b19a3f0c7d2e1``。

    :param kind: 见 :data:`ID_PREFIXES`。
    :param now_ms: 注入的毫秒时间戳（测试用）。
    :param rand: 注入的随机源，签名 ``(nbytes) -> hex str``。
    """
    prefix = ID_PREFIXES.get(kind)
    if prefix is None:
        raise KeyError(f"unknown id kind: {kind!r}; expected one of {sorted(ID_PREFIXES)}")
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if ts < 0:
        raise ValueError("now_ms must be non-negative")
    rnd = (rand or secrets.token_hex)(5)
    if len(rnd) != 10 or not re.fullmatch(r"[0-9a-f]{10}", rnd):
        raise ValueError("rand must return 10 lowercase hex characters")
    return f"{prefix}_{ts:013x}{rnd}"


def parse_id(value: str) -> tuple[str, int, str]:
    """拆解 ID，返回 ``(prefix, timestamp_ms, random_hex)``。"""
    m = _ID_RE.match(value or "")
    if not m:
        raise ValueError(f"malformed id: {value!r}")
    return m.group("prefix"), int(m.group("ts"), 16), m.group("rand")


def is_valid_id(kind: str, value: object) -> bool:
    """校验 ``value`` 是否为 ``kind`` 类型的合法 ID。"""
    if not isinstance(value, str):
        return False
    prefix = ID_PREFIXES.get(kind)
    if prefix is None:
        return False
    m = _ID_RE.match(value)
    return bool(m) and m.group("prefix") == prefix


def id_sort_key(value: str) -> tuple[str, int, str]:
    """用于稳定排序的键（同类型内等价于时间序）。"""
    return parse_id(value)


__all__ = ["ID_PREFIXES", "new_id", "parse_id", "is_valid_id", "id_sort_key"]
