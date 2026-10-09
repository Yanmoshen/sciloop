"""Thread / Turn / Item 领域层（Agent 1 / WP-02）。

职责边界：本模块只管**领域状态与不变量**，不含任何模型调用、工具执行或 HTTP 逻辑。

核心设计：

1. **事件是唯一权威**。所有状态变化都先落事件，再由 :func:`_apply_event` 把事件
   应用到内存状态。重放（replay）与增量更新共用同一个函数，因此
   「重启后从事件恢复」与「运行中的内存态」不可能不一致。
2. **同一 Thread 只有一个活动 Turn**：活动态为 ``running / waiting_approval /
   waiting_input``。跨进程由事件存储的租约保证，进程内由仓库锁保证。
3. **状态机封闭**：非法迁移抛 :class:`IllegalTurnTransition`；
   ``interrupted -> completed`` 被禁止，保证中断后不会出现「最终成功」。
4. **幂等**：``start_turn`` 带 ``idempotency_key`` 时重复调用返回既有 Turn，
   不产生第二个 Turn。
5. **索引与状态**：``index.json`` 只存索引（thread_id -> 目录名），
   ``thread.json`` 只存状态缓存；历史只有 events.jsonl 一份。
6. **每个 Turn 事件都自带完整 Turn 负载**，因此单靠事件重放就能重建 Turn，
   不依赖任何外部指针。
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import (
    ACTIVE_TURN_STATUSES,
    TERMINAL_TURN_STATUSES,
    TURN_TRANSITIONS,
    ApprovalStatus,
    EventType,
    ItemType,
    TurnStatus,
    can_transition,
)
from contracts.agent_v2.errors import (
    ApprovalNotFound,
    ConcurrentTurnError,
    ContractViolation,
    IllegalTurnTransition,
    ThreadNotFound,
    TurnNotFound,
)
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import ApprovalRequest, Event, Item, Thread, ToolCall, Turn
from contracts.agent_v2.version import CONTRACT_VERSION, is_readable
from services.agent_events_v2 import EventStore, Lease

INDEX_FILENAME = "index.json"
THREAD_STATE_FILENAME = "thread.json"

TURN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.TURN_QUEUED.value,
        EventType.TURN_STARTED.value,
        EventType.TURN_PAUSED.value,
        EventType.TURN_RESUMED.value,
        EventType.TURN_INTERRUPTED.value,
        EventType.TURN_WAITING_APPROVAL.value,
        EventType.TURN_WAITING_INPUT.value,
        EventType.TURN_COMPLETED.value,
        EventType.TURN_FAILED.value,
    }
)

TOOL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.MODEL_TOOL_CALL_STARTED.value,
        EventType.TOOL_STARTED.value,
        EventType.TOOL_OUTPUT.value,
        EventType.TOOL_COMPLETED.value,
        EventType.TOOL_FAILED.value,
        EventType.TOOL_TIMEOUT.value,
        EventType.TOOL_CANCELLED.value,
        EventType.TOOL_INVALID_ARGUMENTS.value,
    }
)

APPROVAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.APPROVAL_REQUESTED.value,
        EventType.APPROVAL_GRANTED.value,
        EventType.APPROVAL_DENIED.value,
    }
)

COMPACTION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.COMPACTION_STARTED.value,
        EventType.COMPACTION_COMPLETED.value,
        EventType.COMPACTION_FAILED.value,
        EventType.COMPACTION_EDITED.value,
        EventType.COMPACTION_RESTORED.value,
    }
)

#: 线程身份类事件：分叉时不能原样复制（否则会覆盖新线程身份）。
THREAD_IDENTITY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.THREAD_CREATED.value,
        EventType.THREAD_UPDATED.value,
        EventType.THREAD_FORKED.value,
        EventType.THREAD_ARCHIVED.value,
    }
)

_SLUG_KEEP = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def slugify(name: str, *, limit: int = 40) -> str:
    """把线程名转成可读且安全的目录片段。"""
    slug = _SLUG_KEEP.sub("-", (name or "").strip()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        slug = "thread"
    return slug[:limit]


def _with_thread_fields(thread: Thread, **fields: Any) -> Thread:
    data = thread.to_dict()
    data.update(fields)
    return Thread.from_dict(data)


# --------------------------------------------------------------------------------------
# 状态容器
# --------------------------------------------------------------------------------------
@dataclass
class ThreadState:
    """Thread 的全量可重放状态。"""

    thread: Thread
    turns: dict[str, Turn] = field(default_factory=dict)
    turn_order: list[str] = field(default_factory=list)
    items: dict[str, Item] = field(default_factory=dict)
    item_order: list[str] = field(default_factory=list)
    tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)
    mailbox: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    last_sequence: int = 0

    # ---- 查询 ----
    @property
    def active_turn(self) -> Turn | None:
        for tid in self.turn_order:
            turn = self.turns[tid]
            if turn.status in ACTIVE_TURN_STATUSES:
                return turn
        return None

    def turn(self, turn_id: str) -> Turn:
        try:
            return self.turns[turn_id]
        except KeyError as exc:
            raise TurnNotFound(f"turn {turn_id} not found in thread {self.thread.thread_id}") from exc

    def turn_by_idempotency_key(self, key: str | None) -> Turn | None:
        if not key:
            return None
        for tid in self.turn_order:
            if self.turns[tid].idempotency_key == key:
                return self.turns[tid]
        return None

    def queued_turns(self) -> list[Turn]:
        return [self.turns[t] for t in self.turn_order if self.turns[t].status is TurnStatus.QUEUED]

    def items_for_turn(self, turn_id: str) -> list[Item]:
        return [self.items[i] for i in self.item_order if self.items[i].turn_id == turn_id]

    def items_of_type(self, *types: Any) -> list[Item]:
        wanted = {t.value if hasattr(t, "value") else str(t) for t in types}
        return [self.items[i] for i in self.item_order if self.items[i].type in wanted]

    def assistant_text(self, turn_id: str) -> str:
        return "".join(
            str(item.payload.get("text", ""))
            for item in self.items_for_turn(turn_id)
            if item.type is ItemType.ASSISTANT_TEXT
        )

    def completed_compactions(self) -> list[dict[str, Any]]:
        return [e for e in self.compactions if e.get("phase") == "completed"]

    def active_compaction(self) -> dict[str, Any] | None:
        """当前生效的摘要：最后一个 completed，或被 restored 显式指回的摘要。

        返回的是**合并视图**：若该摘要之后有 ``compaction/edited``，
        生效文本取最后一次编辑的内容（否则「编辑摘要」就形同虚设）。
        """
        active: dict[str, Any] | None = None
        active_id: str | None = None
        for entry in self.compactions:
            phase = entry.get("phase")
            if phase == "completed":
                active = entry
                active_id = entry.get("summary_id")
            elif phase == "restored":
                target = entry.get("summary_id")
                matched = [
                    e
                    for e in self.compactions
                    if e.get("phase") == "completed" and e.get("summary_id") == target
                ]
                if matched:
                    active = matched[-1]
                    active_id = target
        if active is None:
            return None
        merged = dict(active)
        edits = [
            e
            for e in self.compactions
            if e.get("phase") == "edited" and e.get("summary_id") == active_id
        ]
        if edits:
            merged["summary"] = edits[-1].get("text", merged.get("summary"))
            merged["edited"] = True
        return merged

    # ---- 序列化（快照用）----
    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION,
            "thread": self.thread.to_dict(),
            "turns": [self.turns[t].to_dict() for t in self.turn_order],
            "items": [self.items[i].to_dict() for i in self.item_order],
            "tool_calls": [tc.to_dict() for tc in self.tool_calls.values()],
            "approvals": [ap.to_dict() for ap in self.approvals.values()],
            "children": list(self.children),
            "mailbox": list(self.mailbox),
            "compactions": list(self.compactions),
            "last_sequence": self.last_sequence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThreadState:
        version = str(data.get("contract", ""))
        if not is_readable(version):
            raise ContractViolation(f"snapshot state has unreadable contract {version!r}")
        state = cls(thread=Thread.from_dict(data["thread"]))
        for raw in data.get("turns", []):
            turn = Turn.from_dict(raw)
            state.turns[turn.turn_id] = turn
            state.turn_order.append(turn.turn_id)
        for raw in data.get("items", []):
            item = Item.from_dict(raw)
            state.items[item.item_id] = item
            state.item_order.append(item.item_id)
        for raw in data.get("tool_calls", []):
            call = ToolCall.from_dict(raw)
            state.tool_calls[call.call_id] = call
        for raw in data.get("approvals", []):
            approval = ApprovalRequest.from_dict(raw)
            state.approvals[approval.approval_id] = approval
        state.children = list(data.get("children", []))
        state.mailbox = list(data.get("mailbox", []))
        state.compactions = list(data.get("compactions", []))
        state.last_sequence = int(data.get("last_sequence", 0))
        return state


# --------------------------------------------------------------------------------------
# 事件 -> 状态
# --------------------------------------------------------------------------------------
def _apply_event(state: ThreadState, event: Event) -> None:
    """把一条事件应用到状态。**重放与增量共用**，因此两者语义不可能漂移。

    这里刻意**不校验状态迁移**：事件是已经过校验并落盘的既成事实，
    重放只负责忠实重建。校验发生在 :meth:`ThreadRepository._transition`。
    """
    state.last_sequence = max(state.last_sequence, event.sequence)
    etype = event.type
    payload = event.payload or {}

    if etype in (EventType.THREAD_CREATED.value, EventType.THREAD_FORKED.value):
        raw = payload.get("thread")
        if isinstance(raw, dict):
            state.thread = Thread.from_dict(raw)
        return

    if etype == EventType.THREAD_UPDATED.value:
        patch = payload.get("patch") or {}
        current = state.thread.to_dict()
        current.update(patch)
        current["updated_at"] = event.created_at
        state.thread = Thread.from_dict(current)
        return

    if etype == EventType.THREAD_ARCHIVED.value:
        state.thread = _with_thread_fields(
            state.thread, status="archived", updated_at=event.created_at
        )
        return

    # ---- Turn ----
    if etype in TURN_EVENT_TYPES:
        raw = payload.get("turn")
        if isinstance(raw, dict):
            turn = Turn.from_dict(raw)
            if turn.turn_id not in state.turns:
                state.turn_order.append(turn.turn_id)
            state.turns[turn.turn_id] = turn
        turn_id = event.turn_id or payload.get("turn_id")
        if not turn_id or turn_id not in state.turns:
            return
        target = payload.get("to")
        if target is None:
            return
        turn = state.turns[turn_id]
        data = turn.to_dict()
        data["status"] = str(target)
        data["updated_at"] = event.created_at
        for key in ("error", "waiting", "cancel_reason", "attempt"):
            if key in payload:
                data[key] = payload[key]
        if data["status"] in {s.value for s in TERMINAL_TURN_STATUSES}:
            data["sequence_end"] = event.sequence
        state.turns[turn_id] = Turn.from_dict(data)
        status = TurnStatus(data["status"])
        if status in ACTIVE_TURN_STATUSES:
            state.thread = _with_thread_fields(
                state.thread, active_turn_id=turn_id, updated_at=event.created_at
            )
        elif state.thread.active_turn_id == turn_id:
            state.thread = _with_thread_fields(
                state.thread, active_turn_id=None, updated_at=event.created_at
            )
        return

    # ---- Item ----
    if etype == EventType.ITEM_ADDED.value:
        raw = payload.get("item")
        if isinstance(raw, dict):
            item = Item.from_dict(raw)
            if item.item_id not in state.items:
                state.items[item.item_id] = item
                state.item_order.append(item.item_id)
            if item.type is ItemType.USER_INPUT and event.turn_id in state.turns:
                turn = state.turns[event.turn_id]
                if item.item_id not in turn.input_item_ids:
                    data = turn.to_dict()
                    data["input_item_ids"] = [*turn.input_item_ids, item.item_id]
                    state.turns[event.turn_id] = Turn.from_dict(data)
        return

    # ---- 工具调用 ----
    if etype in TOOL_EVENT_TYPES:
        raw = payload.get("tool_call")
        if isinstance(raw, dict):
            call = ToolCall.from_dict(raw)
            state.tool_calls[call.call_id] = call
        return

    # ---- 审批 ----
    if etype in APPROVAL_EVENT_TYPES:
        raw = payload.get("approval")
        if isinstance(raw, dict):
            approval = ApprovalRequest.from_dict(raw)
            state.approvals[approval.approval_id] = approval
        return

    # ---- 压缩 ----
    if etype in COMPACTION_EVENT_TYPES:
        entry = dict(payload)
        entry["phase"] = etype.split("/", 1)[1]
        entry["sequence"] = event.sequence
        entry["created_at"] = event.created_at
        state.compactions.append(entry)
        return

    # ---- 子 Agent ----
    if etype == EventType.AGENT_CHILD_CREATED.value:
        child_id = payload.get("child_thread_id")
        if child_id and child_id not in state.children:
            state.children.append(child_id)
        return

    if etype == EventType.AGENT_MESSAGE.value:
        state.mailbox.append(
            {
                "sequence": event.sequence,
                "created_at": event.created_at,
                "from_thread_id": payload.get("from_thread_id"),
                "to_thread_id": payload.get("to_thread_id"),
                "content": payload.get("content"),
                "kind": payload.get("kind", "message"),
            }
        )
        return


# --------------------------------------------------------------------------------------
# 仓库
# --------------------------------------------------------------------------------------
class ThreadRepository:
    """Thread 仓库：持久化、状态机与不变量。"""

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Clock | None = None,
        turn_lease_stale_s: float = 900.0,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock or SystemClock()
        self.turn_lease_stale_s = turn_lease_stale_s
        self._lock = threading.RLock()
        self._leases: dict[str, Lease] = {}
        self._index_path = self.root / INDEX_FILENAME

    # ------------------------------------------------------------------ 索引
    def _load_index(self) -> dict[str, Any]:
        try:
            raw = self._index_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "threads": {}}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"version": 1, "threads": {}}
        data.setdefault("version", 1)
        data.setdefault("threads", {})
        return data

    def _save_index(self, index: dict[str, Any]) -> None:
        tmp = self._index_path.with_name(INDEX_FILENAME + ".tmp")
        body = json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._index_path)

    def list_threads(self) -> list[str]:
        return sorted(self._load_index()["threads"].keys())

    def exists(self, thread_id: str) -> bool:
        return thread_id in self._load_index()["threads"]

    def folder_name_for(self, thread: Thread) -> str:
        return f"{slugify(thread.name)}-{thread.thread_id.rsplit('_', 1)[-1][-8:]}"

    def folder_of(self, thread_id: str) -> str:
        """解析 thread_id 对应的目录名；索引缺失时按目录后缀回退扫描。"""
        entry = self._load_index()["threads"].get(thread_id)
        if entry and entry.get("folder"):
            return str(entry["folder"])
        short = thread_id.rsplit("_", 1)[-1][-8:]
        for path in self.root.iterdir():
            if path.is_dir() and path.name.endswith(f"-{short}"):
                return path.name
        raise ThreadNotFound(f"thread {thread_id} has no directory under {self.root}")

    def store(self, thread_id: str) -> EventStore:
        return EventStore(self.root, self.folder_of(thread_id), thread_id, clock=self.clock)

    # ------------------------------------------------------------------ 状态
    def recover(self, thread_id: str, *, strict: bool = True):
        """原始恢复视图：快照 + 尾部事件 + 损坏清单（不合成领域状态）。"""
        return self.store(thread_id).recover(strict=strict)

    def state(self, thread_id: str, *, strict: bool = True) -> ThreadState:
        """重建线程状态：优先用最新快照作起点，再重放尾部事件。"""
        with self._lock:
            store = self.store(thread_id)
            result = store.recover(strict=strict)
            if result.snapshot_state is not None:
                state = ThreadState.from_dict(result.snapshot_state)
                state.last_sequence = result.snapshot_sequence or 0
                self._reindex(state)
            else:
                state = ThreadState(thread=self._placeholder_thread(thread_id))
            for event in result.events:
                _apply_event(state, event)
            return state

    def _reindex(self, state: ThreadState) -> None:
        """从快照恢复后补齐顺序索引（快照里 turns/items 是列表，顺序已保存）。"""
        for tid in state.turns:
            if tid not in state.turn_order:
                state.turn_order.append(tid)
        for iid in state.items:
            if iid not in state.item_order:
                state.item_order.append(iid)

    def _placeholder_thread(self, thread_id: str) -> Thread:
        now = self.clock.now_iso()
        return Thread(thread_id=thread_id, name="", created_at=now, updated_at=now)

    def snapshot(self, thread_id: str) -> int:
        """把当前状态写成语义快照，返回快照序号。"""
        state = self.state(thread_id)
        return self.store(thread_id).write_snapshot(state.to_dict())

    def _persist_state_cache(self, state: ThreadState) -> None:
        store = self.store(state.thread.thread_id)
        store.ensure_dirs()
        target = store.dir / THREAD_STATE_FILENAME
        tmp = store.dir / (THREAD_STATE_FILENAME + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, json.dumps(state.to_dict(), ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, target)

    # ------------------------------------------------------------------ 创建 / 分叉
    def create_thread(
        self,
        name: str,
        *,
        settings: dict[str, Any] | None = None,
        cwd: str | None = None,
        model: str | None = None,
        permission_summary: dict[str, Any] | None = None,
        parent_thread_id: str | None = None,
        path: list[str] | None = None,
        thread_id: str | None = None,
    ) -> Thread:
        """创建线程。创建本身**不调用模型**（验收书 §8）。"""
        now = self.clock.now_iso()
        thread = Thread(
            thread_id=thread_id or new_id("thread"),
            name=name or "thread",
            created_at=now,
            updated_at=now,
            settings=dict(settings or {}),
            cwd=cwd,
            model=model,
            permission_summary=dict(permission_summary or {}),
            parent_thread_id=parent_thread_id,
            path=list(path or []),
        )
        if not thread.path:
            thread = _with_thread_fields(thread, path=[thread.thread_id])
        with self._lock:
            index = self._load_index()
            index["threads"][thread.thread_id] = {
                "folder": self.folder_name_for(thread),
                "name": thread.name,
                "parent_thread_id": thread.parent_thread_id,
                "created_at": thread.created_at,
            }
            self._save_index(index)
            store = self.store(thread.thread_id)
            store.ensure_dirs()
            event = store.emit(EventType.THREAD_CREATED, payload={"thread": thread.to_dict()})
            state = ThreadState(thread=self._placeholder_thread(thread.thread_id))
            _apply_event(state, event)
            self._persist_state_cache(state)
        return thread

    def _fork_payload(self, event: Event, new_thread_id: str) -> dict[str, Any]:
        """复制历史事件时把嵌套对象里的 thread_id 改写成新线程。"""
        payload = json.loads(json.dumps(event.payload or {}, ensure_ascii=False))
        for key in ("item", "tool_call", "approval"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                nested["thread_id"] = new_thread_id
        for key in ("turn",):
            nested = payload.get(key)
            if isinstance(nested, dict):
                nested["thread_id"] = new_thread_id
        return payload

    def fork_thread(
        self,
        thread_id: str,
        *,
        at_sequence: int | None = None,
        name: str | None = None,
    ) -> Thread:
        """在指定序号处分叉出新线程。

        - 只复制**内容事件**（Turn / Item / 工具 / 审批 / 压缩 / 子 Agent）；
        - 线程身份事件不复制，改为写一条 ``thread/forked``，避免新线程身份被覆盖；
        - 分叉不继承活动 Turn：复制完成后把仍处活动态的 Turn 显式中断，
          保证新线程起点空闲且可审计。
        """
        with self._lock:
            source_state = self.state(thread_id)
            source_store = self.store(thread_id)
            history = [
                e
                for e in source_store.read_all()
                if e.type not in THREAD_IDENTITY_EVENT_TYPES
            ]
            if at_sequence is not None:
                history = [e for e in history if e.sequence <= int(at_sequence)]

            new_thread = Thread(
                thread_id=new_id("thread"),
                name=name or f"{source_state.thread.name} 分叉",
                created_at=self.clock.now_iso(),
                updated_at=self.clock.now_iso(),
                settings=dict(source_state.thread.settings),
                cwd=source_state.thread.cwd,
                model=source_state.thread.model,
                permission_summary=dict(source_state.thread.permission_summary),
                parent_thread_id=source_state.thread.parent_thread_id,
                path=list(source_state.thread.path) or [source_state.thread.thread_id],
                forked_from={
                    "thread_id": thread_id,
                    "sequence": history[-1].sequence if history else 1,
                },
            )
            index = self._load_index()
            index["threads"][new_thread.thread_id] = {
                "folder": self.folder_name_for(new_thread),
                "name": new_thread.name,
                "parent_thread_id": new_thread.parent_thread_id,
                "created_at": new_thread.created_at,
            }
            self._save_index(index)

            target = self.store(new_thread.thread_id)
            target.ensure_dirs()
            target.append(
                Event(
                    event_id=new_id("event"),
                    sequence=1,
                    type=EventType.THREAD_FORKED.value,
                    created_at=self.clock.now_iso(),
                    thread_id=new_thread.thread_id,
                    payload={
                        "thread": new_thread.to_dict(),
                        "source_thread_id": thread_id,
                        "source_sequence": new_thread.forked_from["sequence"],
                    },
                )
            )
            for event in history:
                target.emit_raw(
                    type=event.type,
                    payload=self._fork_payload(event, new_thread.thread_id),
                    turn_id=event.turn_id,
                    item_id=event.item_id,
                    call_id=event.call_id,
                    created_at=event.created_at,
                )

            state = self.state(new_thread.thread_id)
            for stale in [t.turn_id for t in state.turns.values() if t.status in ACTIVE_TURN_STATUSES]:
                store = self.store(new_thread.thread_id)
                store.emit(
                    EventType.TURN_INTERRUPTED,
                    payload={
                        "from": state.turns[stale].status.value,
                        "to": TurnStatus.INTERRUPTED.value,
                        "cancel_reason": "forked",
                    },
                    turn_id=stale,
                )
            state = self.state(new_thread.thread_id)
            self._persist_state_cache(state)
            return state.thread

    # ------------------------------------------------------------------ 迁移
    def _transition(
        self,
        state: ThreadState,
        turn: Turn,
        to: TurnStatus,
        event_type: EventType,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Turn:
        """校验迁移 -> 先落事件 -> 再更新内存状态。"""
        if not can_transition(turn.status, to):
            raise IllegalTurnTransition(
                turn.turn_id, turn.status.value, to.value, TURN_TRANSITIONS[turn.status]
            )
        body: dict[str, Any] = {"from": turn.status.value, "to": to.value}
        body.update(payload or {})
        body["turn"] = turn.to_dict()
        event = self.store(turn.thread_id).emit(
            event_type, payload=body, turn_id=turn.turn_id
        )
        _apply_event(state, event)
        return state.turns[turn.turn_id]

    def _acquire_turn_lease(self, thread_id: str) -> None:
        lease = self.store(thread_id).lease("turn", stale_after_s=self.turn_lease_stale_s)
        lease.acquire_or_raise()
        self._leases[thread_id] = lease

    # ------------------------------------------------------------------ Turn 生命周期
    def start_turn(
        self,
        thread_id: str,
        *,
        inputs: Iterable[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
        model: str | None = None,
    ) -> Turn:
        """启动新 Turn。

        - 带 ``idempotency_key`` 且已存在同键 Turn 时，直接返回既有 Turn（幂等）；
        - 已有活动 Turn 时抛 :class:`ConcurrentTurnError`；
        - 成功时占用该 Thread 的 Turn 租约（由 :meth:`release_turn` 释放）。
        """
        with self._lock:
            state = self.state(thread_id)
            existing = state.turn_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
            active = state.active_turn
            if active is not None:
                raise ConcurrentTurnError(
                    f"thread {thread_id} already has active turn {active.turn_id} "
                    f"in status {active.status.value}"
                )
            self._acquire_turn_lease(thread_id)
            try:
                store = self.store(thread_id)
                turn = Turn(
                    turn_id=new_id("turn"),
                    thread_id=thread_id,
                    status=TurnStatus.RUNNING,
                    created_at=self.clock.now_iso(),
                    updated_at=self.clock.now_iso(),
                    sequence_start=store.last_sequence() + 1,
                    idempotency_key=idempotency_key,
                )
                event = store.emit(
                    EventType.TURN_STARTED,
                    payload={
                        "from": None,
                        "to": TurnStatus.RUNNING.value,
                        "turn": turn.to_dict(),
                        "model": model,
                    },
                    turn_id=turn.turn_id,
                )
                _apply_event(state, event)
                for raw in inputs or []:
                    self._add_item_locked(
                        state, ItemType.USER_INPUT, {"text": raw.get("text", "")}, turn_id=turn.turn_id
                    )
                self._persist_state_cache(state)
                return state.turns[turn.turn_id]
            except Exception:
                self.release_turn(thread_id, reason="start-failed")
                raise

    def queue_turn(
        self,
        thread_id: str,
        *,
        inputs: Iterable[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> Turn:
        """把 Turn 排入队列（状态 queued，不占用活动名额）。"""
        with self._lock:
            state = self.state(thread_id)
            existing = state.turn_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
            store = self.store(thread_id)
            turn = Turn(
                turn_id=new_id("turn"),
                thread_id=thread_id,
                status=TurnStatus.QUEUED,
                created_at=self.clock.now_iso(),
                updated_at=self.clock.now_iso(),
                sequence_start=store.last_sequence() + 1,
                idempotency_key=idempotency_key,
            )
            event = store.emit(
                EventType.TURN_QUEUED,
                payload={
                    "from": None,
                    "to": TurnStatus.QUEUED.value,
                    "turn": turn.to_dict(),
                    "queue_position": len(state.queued_turns()) + 1,
                },
                turn_id=turn.turn_id,
            )
            _apply_event(state, event)
            for raw in inputs or []:
                self._add_item_locked(
                    state, ItemType.USER_INPUT, {"text": raw.get("text", "")}, turn_id=turn.turn_id
                )
            self._persist_state_cache(state)
            return state.turns[turn.turn_id]

    def promote_queued_turn(self, thread_id: str) -> Turn | None:
        """把队列里最早的 Turn 提升为 running（仍受单活动 Turn 约束）。"""
        with self._lock:
            state = self.state(thread_id)
            if state.active_turn is not None:
                raise ConcurrentTurnError(
                    f"thread {thread_id} already has active turn {state.active_turn.turn_id}"
                )
            pending = state.queued_turns()
            if not pending:
                return None
            self._acquire_turn_lease(thread_id)
            try:
                result = self._transition(
                    state, pending[0], TurnStatus.RUNNING, EventType.TURN_STARTED
                )
                self._persist_state_cache(state)
                return result
            except Exception:
                self.release_turn(thread_id, reason="promote-failed")
                raise

    def append_input(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> Item:
        """给已存在的 Turn 追加用户输入（同键幂等）。"""
        with self._lock:
            state = self.state(thread_id)
            state.turn(turn_id)
            item = self._add_item_locked(
                state,
                ItemType.USER_INPUT,
                {"text": text},
                turn_id=turn_id,
                idempotency_key=idempotency_key,
            )
            self._persist_state_cache(state)
            return item

    def continue_turn(self, thread_id: str, turn_id: str, text: str) -> Turn:
        """等待输入中 -> running，并把用户回答落成事件。"""
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            if turn.status is not TurnStatus.WAITING_INPUT:
                raise IllegalTurnTransition(
                    turn_id,
                    turn.status.value,
                    TurnStatus.RUNNING.value,
                    {TurnStatus.WAITING_INPUT},
                )
            store = self.store(thread_id)
            item = self._add_item_locked(state, ItemType.USER_INPUT, {"text": text}, turn_id=turn_id)
            store.emit(
                EventType.INPUT_PROVIDED,
                payload={"text": text, "item_id": item.item_id},
                turn_id=turn_id,
            )
            result = self._transition(state, turn, TurnStatus.RUNNING, EventType.TURN_RESUMED)
            self._persist_state_cache(state)
            return result

    def wait_for_input(self, thread_id: str, turn_id: str, prompt: str) -> Turn:
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            self.store(thread_id).emit(
                EventType.INPUT_REQUESTED, payload={"prompt": prompt}, turn_id=turn_id
            )
            result = self._transition(
                state,
                turn,
                TurnStatus.WAITING_INPUT,
                EventType.TURN_WAITING_INPUT,
                payload={"waiting": {"kind": "input", "prompt": prompt}},
            )
            self._persist_state_cache(state)
            return result

    def wait_for_approval(
        self,
        thread_id: str,
        turn_id: str,
        *,
        action: dict[str, Any],
        risk: str = "unknown",
        call_id: str | None = None,
    ) -> ApprovalRequest:
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            approval = ApprovalRequest(
                approval_id=new_id("approval"),
                thread_id=thread_id,
                turn_id=turn_id,
                call_id=call_id,
                created_at=self.clock.now_iso(),
                status=ApprovalStatus.PENDING,
                action=dict(action),
                risk=risk,
            )
            item = self._add_item_locked(
                state,
                ItemType.APPROVAL,
                {"approval_id": approval.approval_id, "action": action, "risk": risk},
                turn_id=turn_id,
                call_id=call_id,
            )
            store = self.store(thread_id)
            store.emit(
                EventType.APPROVAL_REQUESTED,
                payload={"approval": approval.to_dict()},
                turn_id=turn_id,
                call_id=call_id,
                item_id=item.item_id,
            )
            state.approvals[approval.approval_id] = approval
            self._transition(
                state,
                turn,
                TurnStatus.WAITING_APPROVAL,
                EventType.TURN_WAITING_APPROVAL,
                payload={
                    "waiting": {
                        "kind": "approval",
                        "approval_id": approval.approval_id,
                        "call_id": call_id,
                    }
                },
            )
            self._persist_state_cache(state)
            return state.approvals[approval.approval_id]

    def resolve_approval(
        self,
        thread_id: str,
        turn_id: str,
        approval_id: str,
        *,
        granted: bool,
        scope: str = "once",
        decided_by: str = "user",
    ) -> Turn:
        """审批决策：无论批准还是拒绝都回到 running（拒绝结果回喂模型）。"""
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            approval = state.approvals.get(approval_id)
            if approval is None:
                raise ApprovalNotFound(f"approval {approval_id} not found")
            data = approval.to_dict()
            data["status"] = (
                ApprovalStatus.GRANTED.value if granted else ApprovalStatus.DENIED.value
            )
            data["decided_at"] = self.clock.now_iso()
            data["decided_by"] = decided_by
            data["decision_scope"] = scope if granted else "denied"
            updated = ApprovalRequest.from_dict(data)
            self.store(thread_id).emit(
                EventType.APPROVAL_GRANTED if granted else EventType.APPROVAL_DENIED,
                payload={"approval": updated.to_dict()},
                turn_id=turn_id,
                call_id=approval.call_id,
            )
            state.approvals[approval_id] = updated
            result = self._transition(state, turn, TurnStatus.RUNNING, EventType.TURN_RESUMED)
            self._persist_state_cache(state)
            return result

    def pause_turn(self, thread_id: str, turn_id: str, reason: str = "paused") -> Turn:
        """暂停：进入 interrupted（可恢复）。"""
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            result = self._transition(
                state,
                turn,
                TurnStatus.INTERRUPTED,
                EventType.TURN_PAUSED,
                payload={"cancel_reason": reason},
            )
            self.release_turn(thread_id, reason=reason)
            self._persist_state_cache(state)
            return result

    def interrupt_turn(self, thread_id: str, turn_id: str, reason: str = "interrupted") -> Turn:
        """中断：进入 interrupted，**不产生任何成功事件**。"""
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            result = self._transition(
                state,
                turn,
                TurnStatus.INTERRUPTED,
                EventType.TURN_INTERRUPTED,
                payload={"cancel_reason": reason},
            )
            self.release_turn(thread_id, reason=reason)
            self._persist_state_cache(state)
            return result

    def resume_turn(self, thread_id: str, turn_id: str, reason: str = "resume") -> Turn:
        """恢复：interrupted -> running，重新占用 Turn 租约。"""
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            if turn.status is not TurnStatus.INTERRUPTED:
                raise IllegalTurnTransition(
                    turn_id,
                    turn.status.value,
                    TurnStatus.RUNNING.value,
                    {TurnStatus.INTERRUPTED},
                )
            active = state.active_turn
            if active is not None and active.turn_id != turn_id:
                raise ConcurrentTurnError(
                    f"thread {thread_id} already has active turn {active.turn_id}"
                )
            self._acquire_turn_lease(thread_id)
            result = self._transition(
                state, turn, TurnStatus.RUNNING, EventType.TURN_RESUMED, payload={"reason": reason}
            )
            self._persist_state_cache(state)
            return result

    def complete_turn(
        self, thread_id: str, turn_id: str, *, stop_reason: str | None = None
    ) -> Turn:
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            result = self._transition(
                state,
                turn,
                TurnStatus.COMPLETED,
                EventType.TURN_COMPLETED,
                payload={"stop_reason": stop_reason} if stop_reason else None,
            )
            self.release_turn(thread_id, reason="completed")
            self._persist_state_cache(state)
            return result

    def fail_turn(self, thread_id: str, turn_id: str, *, error: dict[str, Any]) -> Turn:
        with self._lock:
            state = self.state(thread_id)
            turn = state.turn(turn_id)
            self._add_item_locked(state, ItemType.ERROR, dict(error), turn_id=turn_id)
            result = self._transition(
                state,
                turn,
                TurnStatus.FAILED,
                EventType.TURN_FAILED,
                payload={"error": dict(error)},
            )
            self.release_turn(thread_id, reason="failed")
            self._persist_state_cache(state)
            return result

    # ------------------------------------------------------------------ 低层写入
    def emit_event(
        self,
        thread_id: str,
        type: str | EventType,
        *,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        """直接落一条事件（不改变 Turn 领域状态，供模型/工具/记忆事件使用）。"""
        return self.store(thread_id).emit(
            type,
            payload=payload,
            turn_id=turn_id,
            item_id=item_id,
            call_id=call_id,
            idempotency_key=idempotency_key,
        )

    def add_item(
        self,
        thread_id: str,
        type: str | ItemType,
        payload: dict[str, Any] | None = None,
        *,
        turn_id: str | None = None,
        call_id: str | None = None,
        subagent_thread_id: str | None = None,
        idempotency_key: str | None = None,
        persist_cache: bool = True,
    ) -> Item:
        """追加一个 Item（先落事件，再更新缓存）。"""
        with self._lock:
            state = self.state(thread_id)
            item = self._add_item_locked(
                state,
                type,
                payload or {},
                turn_id=turn_id,
                call_id=call_id,
                subagent_thread_id=subagent_thread_id,
                idempotency_key=idempotency_key,
            )
            if persist_cache:
                self._persist_state_cache(state)
            return item

    def _add_item_locked(
        self,
        state: ThreadState,
        type: str | ItemType,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        call_id: str | None = None,
        subagent_thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Item:
        store = self.store(state.thread.thread_id)
        if idempotency_key:
            existing = store.find_by_idempotency_key(idempotency_key)
            if existing is not None and existing.item_id and existing.item_id in state.items:
                return state.items[existing.item_id]
        item = Item(
            item_id=new_id("item"),
            thread_id=state.thread.thread_id,
            turn_id=turn_id,
            type=type.value if hasattr(type, "value") else str(type),
            created_at=self.clock.now_iso(),
            sequence=store.last_sequence() + 1,
            call_id=call_id,
            subagent_thread_id=subagent_thread_id,
            payload=dict(payload),
        )
        event = store.emit(
            EventType.ITEM_ADDED,
            payload={"item": item.to_dict()},
            turn_id=turn_id,
            item_id=item.item_id,
            call_id=call_id,
            idempotency_key=idempotency_key,
        )
        _apply_event(state, event)
        return item

    def upsert_tool_call(
        self,
        thread_id: str,
        call: ToolCall,
        *,
        event_type: str | EventType,
        item_id: str | None = None,
    ) -> ToolCall:
        """写入/更新工具调用（同一结构承载开始、输出、成功、失败、取消）。"""
        with self._lock:
            self.store(thread_id).emit(
                event_type,
                payload={"tool_call": call.to_dict()},
                turn_id=call.turn_id,
                item_id=item_id,
                call_id=call.call_id,
            )
            # 不写状态缓存：thread.json 只是派生缓存，工具事件密集时写它是纯开销，
            # 真实状态由事件重放得到（state() 会读到刚落的这条事件）。
            return call

    # ------------------------------------------------------------------ 租约
    def turn_lock_free(self, thread_id: str) -> bool:
        return self.store(thread_id).lease("turn").is_free()

    def release_turn(self, thread_id: str, *, reason: str = "released") -> None:
        """释放 Turn 租约（幂等）。取消/超时/失败路径必须走到这里。"""
        lease = self._leases.pop(thread_id, None)
        if lease is not None:
            lease.release(reason)
            return
        try:
            self.store(thread_id).lease("turn").release(reason)
        except ThreadNotFound:
            return

    def held_lease_count(self) -> int:
        """当前进程持有的租约数（测试断言：稳定后必须回到 0）。"""
        return len(self._leases)


__all__ = [
    "ThreadRepository",
    "ThreadState",
    "slugify",
    "INDEX_FILENAME",
    "THREAD_STATE_FILENAME",
    "TURN_EVENT_TYPES",
    "TOOL_EVENT_TYPES",
]
