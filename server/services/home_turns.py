"""Durable homepage turn owners; SSE connections are disposable subscribers.

Like Codex's session listener, workers record events before broadcasting them.
Reattaching replays the same journal and never submits another model/tool call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services import conversations

logger = logging.getLogger("sciloop.home_turns")


class PersistenceError(OSError):
    """A classified writer failure; the owner may retain pending memory state."""

    def __init__(self, info: dict[str, Any]):
        super().__init__(info.get("message") or info.get("operation") or "persistence failed")
        self.info = info


def _diagnose_write(operation: str, path: Any, exc: BaseException) -> dict[str, Any]:
    absolute = str(path)
    info = {
        "operation": operation,
        "path": absolute,
        "exception": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
    logger.error("turn persistence failed operation=%s path=%s", operation, absolute, exc_info=exc)
    return info


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class Owner:
    conversation_id: str
    turn_id: str
    kind: str
    base_count: int
    snapshot: dict[str, Any]
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    cancel_reason: str | None = None
    events: list[str] = field(default_factory=list)
    last_stamp: float = 0
    started_monotonic: float = field(default_factory=time.monotonic)
    last_event_at: str = field(default_factory=_now)
    terminal_attached: bool = False


_owners: dict[str, Owner] = {}
_write_locks: dict[str, asyncio.Lock] = {}


def write_lock(conversation_id: str | None) -> asyncio.Lock:
    if not conversation_id:
        return asyncio.Lock()
    return _write_locks.setdefault(conversation_id, asyncio.Lock())


def _paths(conversation_id: str, turn_id: str):
    # IDs are server-generated; validate before deriving any disk path.
    if not conversations.ID_PATTERN.fullmatch(conversation_id):
        raise ValueError("invalid conversation id")
    if len(turn_id) != 32 or any(c not in "0123456789abcdef" for c in turn_id):
        raise ValueError("invalid turn id")
    root = conversations.conversations_dir() / "_turns" / conversation_id
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{turn_id}.json", root / f"{turn_id}.jsonl"


def _save(owner: Owner) -> None:
    path, _ = _paths(owner.conversation_id, owner.turn_id)
    temp = path.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps(owner.snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise PersistenceError(_diagnose_write("home_turns.snapshot.tmp_write", temp, exc)) from exc
    try:
        temp.replace(path)
    except OSError as exc:
        raise PersistenceError(_diagnose_write("home_turns.snapshot.replace", path, exc)) from exc


def active(conversation_id: str) -> Owner | None:
    owner = _owners.get(conversation_id)
    return owner if owner and owner.task and not owner.task.done() and owner.snapshot["status"] == "inProgress" else None


def _stamp(owner: Owner, *, attach: bool = True) -> None:
    record = conversations.read(owner.conversation_id)
    if record is None:
        return
    if owner.snapshot["status"] == "inProgress":
        record["active_turn_id"] = owner.turn_id
    elif record.get("active_turn_id") == owner.turn_id:
        record["active_turn_id"] = None
    candidates = record.get("turns", [])[owner.base_count:] if owner.base_count >= 0 and attach else [
        turn for turn in record.get("turns", []) if turn.get("turn_id") == owner.turn_id
    ]
    for turn in candidates:
        turn["turn_id"] = owner.turn_id
        if turn.get("role") == "assistant":
            turn.update({key: owner.snapshot.get(key) for key in (
                "status", "started_at", "completed_at", "error", "duration_ms"
            )})
            turn["interrupted"] = owner.snapshot["status"] in {"failed", "interrupted"}
            turn["interruption_reason"] = owner.snapshot.get("reason")
    try:
        conversations.write(record)
    except OSError as exc:
        raise PersistenceError(_diagnose_write("home_turns.conversations.write", conversations.conversations_dir(), exc)) from exc


def _publish(owner: Owner, frame: str) -> None:
    event = "message"
    data = []
    for line in frame.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if not data:
        return
    payload = json.loads("\n".join(data))
    if event == "terminal":
        payload.update(owner.snapshot)
    sequence = len(owner.events) + 1
    payload.update(turn_id=owner.turn_id, event_seq=sequence)
    if event == "done" and owner.snapshot["status"] == "inProgress":
        node_status = payload.get("node_status")
        owner.snapshot["status"] = (
            "interrupted" if node_status == "interrupted" else
            "failed" if node_status == "failed" else
            "waitingApproval" if payload.get("awaiting_approval") or node_status == "waiting_human" else "completed"
        )
        if owner.snapshot["status"] == "interrupted":
            owner.snapshot.update(reason=owner.cancel_reason or "user_stop", error=None)
        elif owner.snapshot["status"] == "completed":
            owner.snapshot.update(reason=None, error=None)
        owner.snapshot["completed_at"] = _now()
        owner.snapshot["duration_ms"] = payload.get("duration_ms")
    elif event == "error" and owner.snapshot["status"] == "inProgress":
        # Research streams can emit a diagnostic error before their durable
        # handoff/terminal. Keep the execution owner until source completion.
        owner.snapshot.update(error={"code": payload.get("code", "worker_failed")})
    payload["turn_status"] = owner.snapshot["status"]
    encoded = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    owner.events.append(encoded)
    _, journal = _paths(owner.conversation_id, owner.turn_id)
    try:
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(encoded, ensure_ascii=False) + "\n")
    except OSError as exc:
        owner.snapshot.setdefault("persistence_warnings", []).append(
            _diagnose_write("home_turns.journal.append", journal, exc)
        )
    owner.snapshot["event_seq"] = sequence
    owner.last_event_at = _now()
    owner.snapshot["last_event_at"] = owner.last_event_at
    try:
        _save(owner)
    except PersistenceError as exc:
        owner.snapshot.setdefault("persistence_warnings", []).append(exc.info)
    now = time.monotonic()
    if now - owner.last_stamp >= 1 or event in {"done", "error", "terminal", "approval"}:
        try:
            _stamp(owner, attach=not owner.terminal_attached)
        except PersistenceError as exc:
            owner.snapshot.setdefault("persistence_warnings", []).append(exc.info)
        owner.last_stamp = now
        if owner.snapshot["status"] != "inProgress":
            owner.terminal_attached = True
    owner.changed.set()


async def _run(owner: Owner, source: AsyncIterator[str]) -> None:
    try:
        async for frame in source:
            _publish(owner, frame)
        if owner.snapshot["status"] == "inProgress":
            owner.snapshot.update(status="failed", reason="worker_failed" if owner.snapshot.get("error") else "missing_terminal",
                                  error=owner.snapshot.get("error") or {"code": "missing_terminal"})
    except asyncio.CancelledError:
        if owner.snapshot["status"] == "inProgress":
            owner.snapshot.update(status="interrupted", reason=owner.cancel_reason or "service_shutdown", error=None)
    except Exception as exc:  # worker errors must converge to a durable terminal
        if owner.snapshot["status"] == "inProgress":
            owner.snapshot.update(status="failed", reason="worker_failed", error={"code": type(exc).__name__})
    finally:
        owner.snapshot["completed_at"] = owner.snapshot.get("completed_at") or _now()
        if owner.snapshot["status"] in {"failed", "interrupted"}:
            _publish(owner, "event: terminal\ndata: {}\n\n")
        try:
            _save(owner)
        except PersistenceError as exc:
            logger.error("final turn snapshot persistence failed: %s", exc.info)
        try:
            _stamp(owner, attach=False)
        except PersistenceError as exc:
            logger.error("final conversation persistence failed: %s", exc.info)
        owner.changed.set()


def start(conversation_id: str, source: AsyncIterator[str], *, kind: str, base_count: int) -> Owner:
    if active(conversation_id):
        raise ValueError("conversation already has an active turn")
    turn_id = uuid.uuid4().hex
    owner = Owner(conversation_id, turn_id, kind, base_count, {
        "conversation_id": conversation_id, "turn_id": turn_id, "kind": kind,
        "status": "inProgress", "started_at": _now(), "completed_at": None,
        "error": None, "reason": None, "event_seq": 0,
        "last_event_at": _now(),
    })
    _owners[conversation_id] = owner
    try:
        _save(owner)
    except PersistenceError as exc:
        owner.snapshot.setdefault("persistence_warnings", []).append(exc.info)
    owner.task = asyncio.create_task(_run(owner, source), name=f"home-turn:{turn_id}")
    return owner


def snapshot(conversation_id: str, turn_id: str) -> dict[str, Any] | None:
    owner = _owners.get(conversation_id)
    if owner and owner.turn_id == turn_id:
        return dict(owner.snapshot)
    path, _ = _paths(conversation_id, turn_id)
    if not path.exists():
        return None
    result = json.loads(path.read_text(encoding="utf-8"))
    if result["status"] == "inProgress":
        # A process restart loses the worker, never silently rerun its tools.
        result.update(status="failed", reason="service_restart", completed_at=_now(),
                      error={"code": "owner_unavailable"})
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        recovered = Owner(conversation_id, turn_id, result["kind"], -1, result)
        record = conversations.read(conversation_id)
        if record:
            _stamp(recovered)
    return result


async def subscribe(conversation_id: str, turn_id: str, after: int = 0) -> AsyncIterator[str]:
    owner = _owners.get(conversation_id)
    if owner and owner.turn_id == turn_id:
        cursor = after
        while True:
            owner.changed.clear()
            while cursor < len(owner.events):
                yield owner.events[cursor]
                cursor += 1
            if owner.task and owner.task.done():
                return
            try:
                await asyncio.wait_for(owner.changed.wait(), timeout=15)
            except TimeoutError:
                heartbeat = {
                    "conversation_id": owner.conversation_id,
                    "turn_id": owner.turn_id,
                    "event_seq": owner.snapshot.get("event_seq", 0),
                    "turn_status": owner.snapshot["status"],
                    "last_event_at": owner.last_event_at,
                    "running_ms": max(0, int((time.monotonic() - owner.started_monotonic) * 1000)),
                }
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
    else:
        _, journal = _paths(conversation_id, turn_id)
        if journal.exists():
            for frame in journal.read_text(encoding="utf-8").splitlines()[after:]:
                yield json.loads(frame)
        state = snapshot(conversation_id, turn_id)
        if state:
            yield f"event: terminal\ndata: {json.dumps({**state, 'turn_status': state['status']})}\n\n"


async def interrupt(conversation_id: str, turn_id: str | None = None) -> dict[str, Any] | None:
    owner = active(conversation_id)
    if owner is None or (turn_id and owner.turn_id != turn_id):
        return None
    owner.cancel_reason = "user_stop"
    owner.task.cancel()
    with contextlib.suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(owner.task), timeout=5)
    if owner.task.done() and owner.snapshot["status"] == "inProgress":
        owner.snapshot.update(status="interrupted", reason="user_stop", completed_at=_now(), error=None)
        _publish(owner, "event: terminal\ndata: {}\n\n")
    return dict(owner.snapshot)
