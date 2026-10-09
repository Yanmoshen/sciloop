"""时钟抽象：让运行时的时间戳与退避等待可注入、可复现。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


def iso_ms(timestamp_ms: int) -> str:
    """毫秒时间戳 -> UTC ISO-8601（毫秒精度，Z 结尾）。"""
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Clock(Protocol):
    """运行时唯一的时间来源。"""

    def now_ms(self) -> int: ...

    def now_iso(self) -> str: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """真实时钟。"""

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def now_iso(self) -> str:
        return iso_ms(self.now_ms())

    async def sleep(self, seconds: float) -> None:
        if seconds and seconds > 0:
            await asyncio.sleep(seconds)
        else:
            await asyncio.sleep(0)


class FakeClock:
    """测试时钟：``sleep`` 不真实等待，只推进虚拟时间。"""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._ms = int(start_ms)
        self.slept: list[float] = []

    def now_ms(self) -> int:
        return self._ms

    def now_iso(self) -> str:
        return iso_ms(self._ms)

    def advance(self, seconds: float) -> None:
        self._ms += int(seconds * 1000)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self.advance(seconds)
        await asyncio.sleep(0)


__all__ = ["Clock", "SystemClock", "FakeClock", "iso_ms"]
