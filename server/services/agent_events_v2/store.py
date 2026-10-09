"""追加式事件存储（Agent 1 / WP-01）。

落盘布局严格遵循计划书 §4.2：

    knowledge-base/conversations/<folder>/events.jsonl
    knowledge-base/conversations/<folder>/snapshots/<sequence>.json

设计要点：

1. **序号**：追加前分配，线程内从 1 起单调递增、不跳号。读取时若发现跳号，
   报告结构化损坏错误而不是"补齐"或"跳过"。
2. **幂等**：``idempotency_key`` 命中时返回既有事件，不新增行也不重复副作用。
3. **读**：``read_all`` / ``read_from`` / ``read_cursor`` 三种入口，支持按序号与游标补齐。
4. **恢复**：``recover`` 返回「快照 + 尾部事件」，用于进程重启后的状态重建。
5. **损坏**：截断行、非法 JSON、schema 违规、序号跳号一律作为结构化错误上报；
   默认严格模式直接抛 :class:`CorruptedEventError`（**绝不静默跳过**），
   非严格模式把它们放进 ``corruptions`` 列表并**停止**返回其后的事件。
6. **崩溃保护**：单条事件用 ``O_APPEND`` + ``fsync`` 追加；快照与整体重写用
   「临时文件 + fsync + ``os.replace``」原子替换。写入中断只可能留下
   「最后一行不完整」，读取时必然被识别为损坏。
7. **数据库边界**：本模块不写数据库。索引/状态/租约由调用方保存，
   历史只有 events.jsonl 这一份权威来源。
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import EventType
from contracts.agent_v2.errors import ContractViolation, CorruptedEventError
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import Event
from contracts.agent_v2.version import CONTRACT_VERSION, is_readable

from .lease import Lease

EVENTS_FILENAME = "events.jsonl"
SNAPSHOT_DIRNAME = "snapshots"


@dataclass
class Corruption:
    """一条损坏记录的描述。"""

    line_no: int
    reason: str
    raw_prefix: str = ""
    sequence: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "reason": self.reason,
            "raw_prefix": self.raw_prefix,
            "sequence": self.sequence,
        }


@dataclass
class RecoveryResult:
    """恢复结果：快照（若有）+ 快照之后的尾部事件。"""

    folder: str
    thread_id: str
    last_sequence: int
    snapshot_sequence: int | None
    snapshot_state: dict[str, Any] | None
    events: list[Event] = field(default_factory=list)
    corruptions: list[Corruption] = field(default_factory=list)

    @property
    def resumed_from(self) -> str:
        if self.corruptions:
            return "corrupt"
        if self.snapshot_sequence is not None and self.events:
            return "snapshot+tail"
        if self.snapshot_sequence is not None:
            return "snapshot"
        if self.events:
            return "events"
        return "empty"


class EventStore:
    """单个 Thread 的事件流。"""

    def __init__(
        self,
        root: str | Path,
        folder: str,
        thread_id: str,
        *,
        clock: Clock | None = None,
    ) -> None:
        if not folder or "/" in folder or "\\" in folder:
            raise ValueError(f"folder must be a single path segment, got {folder!r}")
        self.root = Path(root)
        self.folder = folder
        self.thread_id = thread_id
        self.clock = clock or SystemClock()
        self.dir = self.root / folder
        self.events_path = self.dir / EVENTS_FILENAME
        self.snapshots_dir = self.dir / SNAPSHOT_DIRNAME
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 布局
    def ensure_dirs(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def paths(self) -> dict[str, str]:
        """返回计划书约定的布局路径（供验收核对）。"""
        return {
            "thread_dir": str(self.dir),
            "events": str(self.events_path),
            "snapshots": str(self.snapshots_dir),
        }

    # ------------------------------------------------------------------ 读取
    def _read_raw(self) -> tuple[list[tuple[int, bytes]], bool]:
        """返回 ``[(line_no, raw_bytes)]`` 与「末行是否被截断」。"""
        try:
            data = self.events_path.read_bytes()
        except FileNotFoundError:
            return [], False
        if not data:
            return [], False
        truncated_tail = not data.endswith(b"\n")
        parts = data.split(b"\n")
        if parts and parts[-1] == b"":
            parts.pop()
        return [(i + 1, raw) for i, raw in enumerate(parts)], truncated_tail

    def _scan(self, *, strict: bool) -> tuple[list[Event], list[Corruption]]:
        """解析全部事件；发现损坏即停止并上报。"""
        rows, truncated_tail = self._read_raw()
        events: list[Event] = []
        corruptions: list[Corruption] = []
        expected = 1

        for idx, (line_no, raw) in enumerate(rows):
            is_last = idx == len(rows) - 1
            if truncated_tail and is_last:
                corruptions.append(
                    Corruption(
                        line_no,
                        "truncated trailing line (interrupted append: no trailing newline)",
                        raw.decode("utf-8", errors="replace")[:400],
                    )
                )
                break
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - 任何解码失败都算损坏
                corruptions.append(
                    Corruption(
                        line_no,
                        f"invalid JSON: {type(exc).__name__}: {exc}",
                        raw.decode("utf-8", errors="replace")[:400],
                    )
                )
                break
            seq_hint = obj.get("sequence") if isinstance(obj, dict) else None
            try:
                event = Event.from_dict(obj)
            except ContractViolation as exc:
                corruptions.append(
                    Corruption(line_no, f"contract violation: {exc}", raw[:400].decode("utf-8", "replace"), seq_hint)
                )
                break
            if event.thread_id != self.thread_id:
                corruptions.append(
                    Corruption(
                        line_no,
                        f"thread mismatch: event belongs to {event.thread_id}, store is {self.thread_id}",
                        "",
                        event.sequence,
                    )
                )
                break
            if event.sequence != expected:
                corruptions.append(
                    Corruption(
                        line_no,
                        f"sequence gap or duplicate: expected {expected}, found {event.sequence}",
                        "",
                        event.sequence,
                    )
                )
                break
            events.append(event)
            expected += 1

        if corruptions and strict:
            first = corruptions[0]
            raise CorruptedEventError(
                str(self.events_path),
                first.line_no,
                first.reason,
                raw_prefix=first.raw_prefix,
                sequence=first.sequence,
            )
        return events, corruptions

    def read_all(self, *, strict: bool = True) -> list[Event]:
        events, _ = self._scan(strict=strict)
        return events

    def read_from(self, sequence: int, *, strict: bool = True) -> list[Event]:
        """读取 ``sequence >= 给定值`` 的事件（含该序号）。"""
        return [e for e in self.read_all(strict=strict) if e.sequence >= sequence]

    def read_cursor(self, after_sequence: int, *, strict: bool = True) -> list[Event]:
        """游标读取：``sequence > after_sequence``，用于断线补齐。"""
        return [e for e in self.read_all(strict=strict) if e.sequence > after_sequence]

    def last_sequence(self, *, strict: bool = True) -> int:
        events = self.read_all(strict=strict)
        return events[-1].sequence if events else 0

    def find_by_idempotency_key(self, key: str, *, strict: bool = True) -> Event | None:
        for event in self.read_all(strict=strict):
            if event.idempotency_key == key:
                return event
        return None

    def events_by_type(self, *types: Any, strict: bool = True) -> list[Event]:
        wanted = {t.value if hasattr(t, "value") else str(t) for t in types}
        return [e for e in self.read_all(strict=strict) if e.type in wanted]

    # ------------------------------------------------------------------ 写入
    def _write_line(self, line: str) -> None:
        self.ensure_dirs()
        payload = line.encode("utf-8")
        fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def emit_raw(
        self,
        *,
        type: str | EventType,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        created_at: str | None = None,
        strict: bool = True,
    ) -> Event:
        """按给定内容追加事件；**序号由存储分配**，``created_at`` 可指定。

        与 :meth:`emit` 的区别：本方法保留原始时间戳，用于「复制历史」
        （分叉 / 迁移）场景——若重写时间，历史事件的审计时间线就被破坏了。
        """
        with self._lock:
            events, _ = self._scan(strict=strict)
            if idempotency_key:
                for event in events:
                    if event.idempotency_key == idempotency_key:
                        return event
            event = Event(
                event_id=new_id("event"),
                sequence=(events[-1].sequence if events else 0) + 1,
                type=type.value if hasattr(type, "value") else str(type),
                created_at=created_at or self.clock.now_iso(),
                thread_id=self.thread_id,
                turn_id=turn_id,
                item_id=item_id,
                call_id=call_id,
                idempotency_key=idempotency_key,
                payload=dict(payload or {}),
            )
            self._write_line(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            return event

    def emit(
        self,
        type: str | EventType,
        *,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        """构造并追加一条事件；``idempotency_key`` 命中时返回既有事件。"""
        return self.emit_raw(
            type=type,
            payload=payload,
            turn_id=turn_id,
            item_id=item_id,
            call_id=call_id,
            idempotency_key=idempotency_key,
        )

    def append(self, event: Event, *, strict: bool = True) -> Event:
        """追加给定事件。

        序号以存储分配的为准：若调用方给的 ``sequence`` 不等于「下一个可用序号」，
        会被改写成正确值（避免调用方算错序号污染事件流）。
        """
        with self._lock:
            existing_events, _ = self._scan(strict=strict)
            if event.idempotency_key:
                for existing in existing_events:
                    if existing.idempotency_key == event.idempotency_key:
                        return existing
            expected = (existing_events[-1].sequence if existing_events else 0) + 1
            if event.sequence != expected:
                event = dataclasses.replace(event, sequence=expected)
            if event.thread_id != self.thread_id:
                raise ContractViolation(
                    f"refusing to append event for thread {event.thread_id} into {self.thread_id}"
                )
            self._write_line(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            return event

    def append_many(self, events: Iterable[Event]) -> list[Event]:
        return [self.append(e) for e in events]

    def rewrite_atomic(self, events: Iterable[Event]) -> None:
        """整体原子重写（修复/压缩场景）。序号按顺序重排。"""
        with self._lock:
            rows = []
            for idx, event in enumerate(events, start=1):
                if event.sequence != idx:
                    event = dataclasses.replace(event, sequence=idx)
                rows.append(json.dumps(event.to_dict(), ensure_ascii=False))
            body = ("\n".join(rows) + "\n") if rows else ""
            self.ensure_dirs()
            tmp = self.events_path.with_name(EVENTS_FILENAME + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, body.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.events_path)

    # ------------------------------------------------------------------ 快照
    def snapshot_sequences(self) -> list[int]:
        if not self.snapshots_dir.is_dir():
            return []
        out = []
        for path in self.snapshots_dir.glob("*.json"):
            stem = path.stem
            if stem.isdigit():
                out.append(int(stem))
        return sorted(out)

    def write_snapshot(self, state: dict[str, Any], *, sequence: int | None = None) -> int:
        """写入快照（原子替换）。返回快照对应的序号。"""
        seq = self.last_sequence() if sequence is None else int(sequence)
        payload = {
            "contract": CONTRACT_VERSION,
            "thread_id": self.thread_id,
            "folder": self.folder,
            "sequence": seq,
            "created_at": self.clock.now_iso(),
            "state": state,
        }
        self.ensure_dirs()
        target = self.snapshots_dir / f"{seq}.json"
        tmp = self.snapshots_dir / f".{seq}.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        return seq

    def load_snapshot(self, sequence: int) -> dict[str, Any]:
        """读取指定序号的快照。"""
        path = self.snapshots_dir / f"{int(sequence)}.json"
        if not path.is_file():
            raise FileNotFoundError(f"snapshot not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ContractViolation(f"snapshot {path} is not an object")
        version = data.get("contract")
        if not version or not is_readable(str(version)):
            raise ContractViolation(f"snapshot {path} has unreadable contract {version!r}")
        if data.get("thread_id") != self.thread_id:
            raise ContractViolation(f"snapshot {path} belongs to another thread")
        return data

    def latest_snapshot(self) -> tuple[int, dict[str, Any]] | None:
        """返回 ``(sequence, snapshot_dict)``，无快照返回 None。"""
        seqs = self.snapshot_sequences()
        if not seqs:
            return None
        seq = seqs[-1]
        return seq, self.load_snapshot(seq)

    def latest_snapshot_at_or_before(self, sequence: int) -> tuple[int, dict[str, Any]] | None:
        candidates = [s for s in self.snapshot_sequences() if s <= int(sequence)]
        if not candidates:
            return None
        seq = candidates[-1]
        return seq, self.load_snapshot(seq)

    # ------------------------------------------------------------------ 恢复
    def recover(self, *, strict: bool = True) -> RecoveryResult:
        """快照 + 尾部恢复。

        严格模式遇到损坏直接抛 :class:`CorruptedEventError`；
        非严格模式返回 ``corruptions``，且**不返回**损坏行之后的事件
        （宁可少给，也不静默跳过——调用方必须显式处理损坏）。
        """
        snapshot = self.latest_snapshot()
        events, corruptions = self._scan(strict=strict)
        snapshot_seq = snapshot[0] if snapshot else None
        snapshot_state = snapshot[1]["state"] if snapshot else None
        tail = [e for e in events if snapshot_seq is None or e.sequence > snapshot_seq]
        reference = tail[-1].sequence if tail else (events[-1].sequence if events else 0)
        last = max(reference, snapshot_seq or 0)
        return RecoveryResult(
            folder=self.folder,
            thread_id=self.thread_id,
            last_sequence=last,
            snapshot_sequence=snapshot_seq,
            snapshot_state=snapshot_state,
            events=tail,
            corruptions=corruptions,
        )

    # ------------------------------------------------------------------ 租约
    def lease(self, name: str = "turn", *, owner: str | None = None, stale_after_s: float = 600.0) -> Lease:
        """取得该 Thread 的写租约（默认是「活动 Turn」锁）。"""
        return Lease(
            self.dir / f"{name}.lease",
            owner=owner or f"{self.thread_id}:{os.getpid()}",
            clock=self.clock,
            stale_after_s=stale_after_s,
        )


__all__ = [
    "EventStore",
    "RecoveryResult",
    "Corruption",
    "EVENTS_FILENAME",
    "SNAPSHOT_DIRNAME",
]
