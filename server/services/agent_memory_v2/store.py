"""记忆存储（Agent 1 / WP-06）。

落盘布局（计划书 §4.6）：

    knowledge-base/memories/users/<user_id>/<memory_id>.json
    knowledge-base/memories/projects/<project_id>/<memory_id>.json
    knowledge-base/memories/conversations/<thread_id>/<memory_id>.json

不变量：

1. **三作用域目录严格隔离**——用户记忆绝不会出现在项目作用域的查询结果里；
2. **每条记录都带来源事件、版本、时间与删除状态**（删除是软删除，文件保留以利审计）；
3. **用户编辑过的记忆不能被自动流程静默覆盖**：
   ``origin=auto`` 的写入命中 ``edited_by_user`` 记录时抛
   :class:`MemoryOverwriteDenied`；自动流程想要记录新内容必须新建记录。
4. 每次写入都按冻结的 ``memory.schema.json`` 校验，避免实现与契约漂移。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import MemoryOrigin, MemoryScope
from contracts.agent_v2.errors import MemoryOverwriteDenied
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import MemoryRecord
from contracts.agent_v2.validate import validator_for

SCOPE_DIRS: dict[str, str] = {
    MemoryScope.USER.value: "users",
    MemoryScope.PROJECT.value: "projects",
    MemoryScope.CONVERSATION.value: "conversations",
}


def _safe_segment(value: str, *, label: str) -> str:
    if not value or "/" in value or "\\" in value or value in (".", ".."):
        raise ValueError(f"{label} must be a single safe path segment, got {value!r}")
    return value


class MemoryStore:
    """文件系统记忆存储。"""

    def __init__(self, root: str | Path, *, clock: Clock | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 路径
    def dir_for(self, scope: str | MemoryScope, scope_id: str) -> Path:
        scope_value = scope.value if hasattr(scope, "value") else str(scope)
        if scope_value not in SCOPE_DIRS:
            raise ValueError(f"unknown memory scope {scope!r}")
        return self.root / SCOPE_DIRS[scope_value] / _safe_segment(scope_id, label="scope_id")

    def path_for(self, record: MemoryRecord) -> Path:
        return self.dir_for(record.scope, record.scope_id) / f"{record.memory_id}.json"

    def find_path(self, memory_id: str) -> Path:
        """定位记忆文件；不存在时抛 :class:`FileNotFoundError`（文件存储的自然语义）。"""
        matches = list(self.root.glob(f"*/*/{memory_id}.json"))
        if not matches:
            raise FileNotFoundError(f"memory {memory_id} not found under {self.root}")
        return matches[0]

    # ------------------------------------------------------------------ 读写
    def _write_atomic(self, path: Path, record: MemoryRecord) -> None:
        validator_for("memory").check(record.to_dict(), label="memory record")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        body = json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def get(self, memory_id: str) -> MemoryRecord:
        path = self.find_path(memory_id)
        return MemoryRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list(
        self,
        scope: str | MemoryScope,
        scope_id: str,
        *,
        include_deleted: bool = False,
    ) -> list[MemoryRecord]:
        """按作用域列出记忆（严格隔离：不会跨出该作用域目录）。"""
        directory = self.dir_for(scope, scope_id)
        if not directory.is_dir():
            return []
        out: list[MemoryRecord] = []
        for path in sorted(directory.glob("*.json")):
            record = MemoryRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if record.deleted and not include_deleted:
                continue
            out.append(record)
        return out

    def list_all(self, *, include_deleted: bool = False) -> list[MemoryRecord]:
        out: list[MemoryRecord] = []
        for scope, dirname in SCOPE_DIRS.items():
            base = self.root / dirname
            if not base.is_dir():
                continue
            for scope_dir in sorted(base.iterdir()):
                if not scope_dir.is_dir():
                    continue
                out.extend(self.list(scope, scope_dir.name, include_deleted=include_deleted))
        return out

    # ------------------------------------------------------------------ 写入
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
    ) -> MemoryRecord:
        """写入记忆。

        带 ``memory_id`` 且已存在时是「更新既有记录」；不带则新建。
        自动流程（``origin=auto``）更新到用户编辑过的记录时会被拒绝。
        """
        origin_value = origin.value if hasattr(origin, "value") else str(origin)
        scope_value = scope.value if hasattr(scope, "value") else str(scope)
        now = self.clock.now_iso()

        existing: MemoryRecord | None = None
        if memory_id:
            try:
                existing = self.get(memory_id)
            except FileNotFoundError:
                existing = None

        if existing is not None:
            if origin_value == MemoryOrigin.AUTO.value and (
                existing.edited_by_user or existing.origin is MemoryOrigin.USER
            ):
                raise MemoryOverwriteDenied(existing.memory_id, origin_value)
            record = MemoryRecord(
                memory_id=existing.memory_id,
                scope=scope_value,
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
                scope=scope_value,
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
        self._write_atomic(self.path_for(record), record)
        return record

    def update(
        self,
        memory_id: str,
        text: str,
        *,
        actor: str | MemoryOrigin = MemoryOrigin.AUTO,
        source_event_ids: Iterable[str] = (),
    ) -> MemoryRecord:
        """更新记忆文本；``actor`` 决定是否触发用户编辑保护。"""
        actor_value = actor.value if hasattr(actor, "value") else str(actor)
        existing = self.get(memory_id)
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
        )

    def delete(
        self, memory_id: str, *, actor: str | MemoryOrigin = MemoryOrigin.USER
    ) -> MemoryRecord:
        """软删除（保留文件以便审计）。"""
        actor_value = actor.value if hasattr(actor, "value") else str(actor)
        existing = self.get(memory_id)
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
        self._write_atomic(self.path_for(record), record)
        return record

    # ------------------------------------------------------------------ 便捷查询
    def user(self, user_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        return self.list(MemoryScope.USER, user_id, include_deleted=include_deleted)

    def project(self, project_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        return self.list(MemoryScope.PROJECT, project_id, include_deleted=include_deleted)

    def conversation(self, thread_id: str, *, include_deleted: bool = False) -> list[MemoryRecord]:
        return self.list(MemoryScope.CONVERSATION, thread_id, include_deleted=include_deleted)

    def layout(self) -> dict[str, str]:
        """返回三个作用域根目录（验收用：目录隔离可核对）。"""
        return {scope: str(self.root / dirname) for scope, dirname in SCOPE_DIRS.items()}


__all__ = ["MemoryStore", "SCOPE_DIRS"]
