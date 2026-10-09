"""记忆存储：append-only ``records.jsonl`` + 可审计 tombstone（计划书 WP-07）。

落盘布局（严格按计划书）：

    knowledge-base/memories/user/<user-id>/records.jsonl
    knowledge-base/memories/projects/<project-id>/records.jsonl
    knowledge-base/memories/threads/<thread-id>/records.jsonl

不变量：

1. **三作用域目录严格隔离**——任何查询都不得跨出本作用域目录；
2. **append-only**：每次写入追加一行信封（含创建者、置信度、内容哈希、修订号），
   因此「谁在什么时候改了什么」永远可审计；
3. **删除是可审计 tombstone**——追加一条 ``deleted=true`` 的新修订，历史行保留；
4. **用户编辑过的记忆不能被自动流程静默覆盖**（抛 ``MemoryOverwriteDenied``），
   自动流程要记新内容必须新建记录；
5. 对外仍返回契约 ``MemoryRecord``（Agent 3 的公开 API 不变），
   信封字段通过 :meth:`MemoryStore.envelopes` 暴露。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import MemoryOrigin, MemoryScope
from contracts.agent_v2.errors import MemoryOverwriteDenied
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import MemoryRecord

from .models import MemoryEnvelope, content_hash

#: 作用域 -> 目录名（按计划书原文：user / projects / threads）。
SCOPE_DIRS: dict[str, str] = {
    MemoryScope.USER.value: "user",
    MemoryScope.PROJECT.value: "projects",
    MemoryScope.CONVERSATION.value: "threads",
}

RECORDS_FILENAME = "records.jsonl"


def _safe_segment(value: str, *, label: str) -> str:
    if not value or "/" in value or "\\" in value or value in (".", ".."):
        raise ValueError(f"{label} must be a single safe path segment, got {value!r}")
    return value


class MemoryStore:
    """基于 append-only jsonl 的记忆存储。"""

    def __init__(self, root: str | Path, *, clock: Clock | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 路径
    def dir_for(self, scope: str | MemoryScope, scope_id: str) -> Path:
        scope_value = scope.value if hasattr(scope, "value") else str(scope)
        if scope_value not in SCOPE_DIRS:
            raise ValueError(f"unknown memory scope {scope!r}")
        return self.root / SCOPE_DIRS[scope_value] / _safe_segment(scope_id, label="scope_id")

    def records_path(self, scope: str | MemoryScope, scope_id: str) -> Path:
        return self.dir_for(scope, scope_id) / RECORDS_FILENAME

    def layout(self) -> dict[str, str]:
        """三个作用域根目录（验收用：目录隔离可核对）。"""
        return {scope: str(self.root / dirname) for scope, dirname in SCOPE_DIRS.items()}

    def find_path(self, memory_id: str) -> Path:
        """定位某条记忆所在的 ``records.jsonl``。"""
        for path in self.root.glob(f"*/*/{RECORDS_FILENAME}"):
            if memory_id in self._ids_in(path):
                return path
        raise FileNotFoundError(f"memory {memory_id} not found under {self.root}")

    # ------------------------------------------------------------------ 读
    def _read_lines(self, path: Path) -> Iterator[MemoryEnvelope]:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                # 半行/坏行：明确报错，绝不静默跳过（与事件存储同一条纪律）
                yield MemoryEnvelope.from_dict(json.loads(raw), line_no=line_no)

    @staticmethod
    def _ids_in(path: Path) -> set[str]:
        ids: set[str] = set()
        if not path.is_file():
            return ids
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ids.add(str(json.loads(raw).get("record", {}).get("memory_id")))
                except ValueError:
                    continue
        return ids

    def envelopes(
        self,
        scope: str | MemoryScope,
        scope_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[MemoryEnvelope]:
        """折算出每个 memory_id 的**最新**修订（默认隐藏 tombstone）。"""
        with self._lock:
            latest: dict[str, MemoryEnvelope] = {}
            for envelope in self._read_lines(self.records_path(scope, scope_id)):
                previous = latest.get(envelope.memory_id)
                if previous is None or envelope.revision >= previous.revision:
                    latest[envelope.memory_id] = envelope
            out = [e for e in latest.values() if include_deleted or not e.record.deleted]
            out.sort(key=lambda e: (e.record.created_at, e.memory_id))
            return out

    def history(self, memory_id: str) -> list[MemoryEnvelope]:
        """某条记忆的完整修订历史（审计用，含 tombstone）。"""
        path = self.find_path(memory_id)
        return [e for e in self._read_lines(path) if e.memory_id == memory_id]

    def list(
        self,
        scope: str | MemoryScope,
        scope_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        return [e.record for e in self.envelopes(scope, scope_id, include_deleted=include_deleted)]

    def get(self, memory_id: str) -> MemoryRecord:
        path = self.find_path(memory_id)
        found: MemoryEnvelope | None = None
        for envelope in self._read_lines(path):
            if envelope.memory_id == memory_id and (
                found is None or envelope.revision >= found.revision
            ):
                found = envelope
        if found is None:
            raise FileNotFoundError(f"memory {memory_id} not found")
        return found.record

    def get_envelope(self, memory_id: str) -> MemoryEnvelope:
        path = self.find_path(memory_id)
        found: MemoryEnvelope | None = None
        for envelope in self._read_lines(path):
            if envelope.memory_id == memory_id and (
                found is None or envelope.revision >= found.revision
            ):
                found = envelope
        if found is None:
            raise FileNotFoundError(f"memory {memory_id} not found")
        return found

    def list_all(self, *, include_deleted: bool = False) -> list[MemoryRecord]:
        out: list[MemoryRecord] = []
        for scope, dirname in SCOPE_DIRS.items():
            base = self.root / dirname
            if not base.is_dir():
                continue
            for scope_dir in sorted(base.iterdir()):
                if scope_dir.is_dir():
                    out.extend(self.list(scope, scope_dir.name, include_deleted=include_deleted))
        return out

    # ------------------------------------------------------------------ 写
    def _append(self, scope: str | MemoryScope, scope_id: str, envelope: MemoryEnvelope) -> None:
        path = self.records_path(scope, scope_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(envelope.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def rewrite(self, scope: str | MemoryScope, scope_id: str, envelopes: Iterable[MemoryEnvelope]) -> None:
        """原子重写（压缩/修复）：只保留传入的修订，行号与修订号重排。"""
        with self._lock:
            path = self.records_path(scope, scope_id)
            materialised = list(envelopes)
            rows = []
            for index, envelope in enumerate(materialised, start=1):
                envelope.revision = index
                envelope.line_no = index
                rows.append(json.dumps(envelope.to_dict(), ensure_ascii=False))
            body = ("\n".join(rows) + "\n") if rows else ""
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, body.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)

    def _next_revision(self, scope: str | MemoryScope, scope_id: str, memory_id: str) -> int:
        highest = 0
        for envelope in self._read_lines(self.records_path(scope, scope_id)):
            if envelope.memory_id == memory_id:
                highest = max(highest, envelope.revision)
        return highest + 1

    def _latest_envelope(
        self, scope: str | MemoryScope, scope_id: str, memory_id: str
    ) -> MemoryEnvelope | None:
        found: MemoryEnvelope | None = None
        for envelope in self._read_lines(self.records_path(scope, scope_id)):
            if envelope.memory_id == memory_id and (
                found is None or envelope.revision >= found.revision
            ):
                found = envelope
        return found

    def write(
        self,
        scope: str | MemoryScope,
        scope_id: str,
        text: str,
        *,
        source_event_ids: Iterable[str] = (),
        origin: str | MemoryOrigin = MemoryOrigin.AUTO,
        tags: Iterable[str] = (),
        memory_id: str | None = None,
        creator: str = "system",
        confidence: float = 0.5,
    ) -> MemoryRecord:
        """写入记忆。带 ``memory_id`` 且存在时是更新，否则新建。

        自动流程（``origin=auto``）更新**用户编辑过**的记录会被拒绝。
        """
        origin_value = origin.value if hasattr(origin, "value") else str(origin)
        now = self.clock.now_iso()
        with self._lock:
            previous = (
                self._latest_envelope(scope, scope_id, memory_id) if memory_id else None
            )
            existing = previous.record if previous is not None else None
            if existing is not None:
                if origin_value == MemoryOrigin.AUTO.value and (
                    existing.edited_by_user or existing.origin is MemoryOrigin.USER
                ):
                    raise MemoryOverwriteDenied(existing.memory_id, origin_value)
                record = MemoryRecord(
                    memory_id=existing.memory_id,
                    scope=scope if isinstance(scope, str) else scope.value,
                    scope_id=scope_id,
                    text=text,
                    version=existing.version + 1,
                    created_at=existing.created_at,
                    updated_at=now,
                    origin=origin_value,
                    edited_by_user=existing.edited_by_user
                    or origin_value == MemoryOrigin.USER.value,
                    deleted=existing.deleted,
                    source_event_ids=sorted(
                        {*existing.source_event_ids, *[str(e) for e in source_event_ids]}
                    ),
                    tags=sorted({*existing.tags, *[str(t) for t in tags]}),
                )
            else:
                record = MemoryRecord(
                    memory_id=memory_id or new_id("memory"),
                    scope=scope if isinstance(scope, str) else scope.value,
                    scope_id=scope_id,
                    text=text,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    origin=origin_value,
                    edited_by_user=origin_value == MemoryOrigin.USER.value,
                    source_event_ids=[str(e) for e in source_event_ids],
                    tags=[str(t) for t in tags],
                )
            envelope = MemoryEnvelope(
                record=record,
                creator=creator,
                confidence=confidence,
                revision=self._next_revision(scope, scope_id, record.memory_id),
            )
            self._append(scope, scope_id, envelope)
            return record

    def update(
        self,
        memory_id: str,
        text: str,
        *,
        actor: str | MemoryOrigin = MemoryOrigin.AUTO,
        source_event_ids: Iterable[str] = (),
        creator: str | None = None,
        confidence: float | None = None,
    ) -> MemoryRecord:
        """更新记忆文本；``actor`` 决定是否触发用户编辑保护。"""
        actor_value = actor.value if hasattr(actor, "value") else str(actor)
        envelope = self.get_envelope(memory_id)
        existing = envelope.record
        if actor_value == MemoryOrigin.AUTO.value and (
            existing.edited_by_user or existing.origin is MemoryOrigin.USER
        ):
            raise MemoryOverwriteDenied(existing.memory_id, actor_value)
        return self.write(
            existing.scope,
            existing.scope_id,
            text,
            source_event_ids=source_event_ids,
            origin=actor_value,
            tags=existing.tags,
            memory_id=memory_id,
            creator=creator or envelope.creator,
            confidence=envelope.confidence if confidence is None else confidence,
        )

    def delete(
        self, memory_id: str, *, actor: str | MemoryOrigin = MemoryOrigin.USER
    ) -> MemoryRecord:
        """软删除：追加一条 ``deleted=true`` 的 tombstone，历史行全部保留。"""
        actor_value = actor.value if hasattr(actor, "value") else str(actor)
        envelope = self.get_envelope(memory_id)
        existing = envelope.record
        with self._lock:
            record = MemoryRecord(
                memory_id=existing.memory_id,
                scope=existing.scope,
                scope_id=existing.scope_id,
                text=existing.text,
                version=existing.version + 1,
                created_at=existing.created_at,
                updated_at=self.clock.now_iso(),
                origin=actor_value,
                edited_by_user=existing.edited_by_user
                or actor_value == MemoryOrigin.USER.value,
                deleted=True,
                source_event_ids=list(existing.source_event_ids),
                tags=list(existing.tags),
            )
            self._append(
                existing.scope,
                existing.scope_id,
                MemoryEnvelope(
                    record=record,
                    creator=envelope.creator,
                    confidence=envelope.confidence,
                    revision=self._next_revision(
                        existing.scope, existing.scope_id, existing.memory_id
                    ),
                ),
            )
            return record

    # ------------------------------------------------------------------ 便捷查询
    def user(self, user_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        return self.list(MemoryScope.USER, user_id, include_deleted=include_deleted)

    def project(self, project_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        return self.list(MemoryScope.PROJECT, project_id, include_deleted=include_deleted)

    def conversation(self, thread_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        return self.list(MemoryScope.CONVERSATION, thread_id, include_deleted=include_deleted)

    def stats(self, scope: str | MemoryScope, scope_id: str) -> dict[str, Any]:
        """作用域统计（行数 / 记忆条数 / 是否有被手改过的行）。"""
        with self._lock:
            all_envelopes = list(self._read_lines(self.records_path(scope, scope_id)))
            ids = {e.memory_id for e in all_envelopes}
            return {
                "lines": len(all_envelopes),
                "memories": len(ids),
                "tampered": sum(1 for e in all_envelopes if not e.intact),
                "content_hashes": {e.memory_id: e.content_hash for e in all_envelopes},
            }

    @staticmethod
    def hash_text(text: str) -> str:
        return content_hash(text)


__all__ = ["RECORDS_FILENAME", "SCOPE_DIRS", "MemoryStore"]
