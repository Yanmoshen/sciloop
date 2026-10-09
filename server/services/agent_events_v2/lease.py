"""Thread 级租约（lease）。

用途：保证「同一 Thread 同时只有一个活动 Turn」在**跨进程**下也成立，
并给「取消 / 超时 / 失败后不得遗留锁」提供可断言的载体。

实现要点（刻意**不删除文件**）：
- 租约状态写在文件内容里（``released: true``），而不是靠 unlink 释放——
  避免与宿主侧安全删除策略冲突，也让「锁是否释放」可由外部直接读文件核对；
- 获取/争抢/释放都用 ``os.replace`` 原子替换，不做「读-改-删」的竞态操作；
- 支持陈旧租约接管：持有者进程若已消失或心跳超时，后来者可以接管。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.errors import LeaseError


@dataclass
class LeaseState:
    """租约文件的内容结构。"""

    owner: str
    pid: int
    acquired_at_ms: int
    heartbeat_ms: int
    released: bool = False
    release_reason: str | None = None
    host: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "pid": self.pid,
            "host": self.host,
            "acquired_at_ms": self.acquired_at_ms,
            "heartbeat_ms": self.heartbeat_ms,
            "released": self.released,
            "release_reason": self.release_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LeaseState:
        return cls(
            owner=str(data.get("owner", "")),
            pid=int(data.get("pid", 0) or 0),
            host=str(data.get("host", "")),
            acquired_at_ms=int(data.get("acquired_at_ms", 0) or 0),
            heartbeat_ms=int(data.get("heartbeat_ms", 0) or 0),
            released=bool(data.get("released", False)),
            release_reason=data.get("release_reason"),
        )


class Lease:
    """文件租约。典型用法::

        lease = store.lease("turn")
        lease.acquire_or_raise()
        try:
            ...
        finally:
            lease.release("done")
    """

    def __init__(
        self,
        path: Path,
        *,
        owner: str,
        clock: Clock | None = None,
        stale_after_s: float = 600.0,
    ) -> None:
        self.path = Path(path)
        self.owner = owner
        self.clock = clock or SystemClock()
        self.stale_after_s = float(stale_after_s)
        self._held = False

    # ---- 读取 ----
    def read(self) -> LeaseState | None:
        """读取当前租约内容；文件不存在返回 None。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            return LeaseState.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            # 损坏的租约文件视为陈旧，允许接管
            return LeaseState(
                owner="<corrupt>", pid=0, acquired_at_ms=0, heartbeat_ms=0, released=True
            )

    def is_free(self) -> bool:
        """租约是否空闲（不存在 / 已释放 / 已陈旧）。"""
        state = self.read()
        if state is None or state.released:
            return True
        return self._is_stale(state)

    def holder(self) -> str | None:
        state = self.read()
        if state is None or state.released:
            return None
        return state.owner

    @property
    def held(self) -> bool:
        return self._held

    # ---- 内部 ----
    def _is_stale(self, state: LeaseState) -> bool:
        # 同一台机器：进程已不存在即可判定陈旧
        if state.pid and state.host == socket.gethostname() and not _pid_alive(state.pid):
            return True
        age_ms = self.clock.now_ms() - max(state.heartbeat_ms, state.acquired_at_ms)
        return age_ms > self.stale_after_s * 1000

    def _write(self, state: LeaseState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    # ---- 操作 ----
    def acquire(self) -> bool:
        """尝试获取租约。成功返回 True；已被他人持有返回 False。"""
        state = self.read()
        if state is not None and not state.released and not self._is_stale(state):
            return False
        now = self.clock.now_ms()
        self._write(
            LeaseState(
                owner=self.owner,
                pid=os.getpid(),
                host=socket.gethostname(),
                acquired_at_ms=now,
                heartbeat_ms=now,
            )
        )
        self._held = True
        return True

    def acquire_or_raise(self) -> None:
        if not self.acquire():
            holder = self.holder() or "<unknown>"
            raise LeaseError(f"lease {self.path.name} is held by {holder!r}")

    def heartbeat(self) -> None:
        if not self._held:
            raise LeaseError("cannot heartbeat a lease that is not held")
        state = self.read()
        now = self.clock.now_ms()
        self._write(
            LeaseState(
                owner=self.owner,
                pid=os.getpid(),
                host=socket.gethostname(),
                acquired_at_ms=state.acquired_at_ms if state else now,
                heartbeat_ms=now,
            )
        )

    def release(self, reason: str = "released") -> None:
        """释放租约（写 released 标记，不删除文件）。"""
        now = self.clock.now_ms()
        self._write(
            LeaseState(
                owner=self.owner,
                pid=os.getpid(),
                host=socket.gethostname(),
                acquired_at_ms=now,
                heartbeat_ms=now,
                released=True,
                release_reason=reason,
            )
        )
        self._held = False

    def __enter__(self) -> Lease:
        self.acquire_or_raise()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release("context-exit")


def _pid_alive(pid: int) -> bool:
    """判断同机进程是否存活（跨平台，失败时保守认为存活）。"""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":  # pragma: no cover - Windows 分支
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001 - 判定失败时保守处理
        return True


__all__ = ["Lease", "LeaseState"]
