"""Codex-style session orchestration for the local v2 runtime.

Codex keeps the thread/session owner separate from the per-turn worker.  The
repository is the durable owner of state here as well; this class is the
small process-local coordinator that starts workers, joins them, and exposes
the same context/tool/child-agent surfaces to API adapters.

The adapter deliberately depends on protocols instead of a concrete host. It
therefore works with both ``ProductionRuntimeHost`` and the offline host used
by the protocol tests without copying either implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from contracts.agent_v2.enums import ACTIVE_TURN_STATUSES, TurnStatus
from contracts.agent_v2.errors import IllegalTurnTransition
from contracts.agent_v2.models import Turn

from .context import build_context, estimate_tokens


class RuntimeHost(Protocol):
    """The subset of a runtime host required by the session coordinator."""

    def runtime_for(self, thread_id: str) -> Any: ...

    def start(self, thread_id: str, turn_id: str) -> asyncio.Task[Any]: ...

    def interrupt(self, thread_id: str, reason: str = "interrupted") -> bool: ...

    def is_running(self, thread_id: str) -> bool: ...

    async def await_idle(self, thread_id: str, *, timeout_s: float = 10.0) -> Any: ...


@dataclass(frozen=True)
class SessionTurn:
    """A stable, serializable view returned by session controls."""

    thread_id: str
    turn_id: str
    status: TurnStatus
    created: bool
    mode: str = "start"
    retried_from: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "status": self.status.value,
            "created": self.created,
            "mode": self.mode,
            "retried_from": self.retried_from,
        }


class CodexSession:
    """One session/turn owner with Codex-like control semantics.

    ``ThreadRepository`` remains the source of truth.  A task is only a
    worker handle and is safe to lose on process restart; a later ``resume``
    reconstructs the turn from the event log before launching a new worker.
    """

    def __init__(
        self,
        *,
        repo: Any,
        host: RuntimeHost,
        tree: Any | None = None,
        compaction: Any | None = None,
        system_prompt: str | None = None,
        on_outcome: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.repo = repo
        self.host = host
        self.tree = tree
        self.compaction = compaction
        self.system_prompt = system_prompt
        self.on_outcome = on_outcome
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    # ------------------------------------------------------------------ turns
    async def submit(
        self,
        thread_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        model: str | None = None,
    ) -> SessionTurn:
        """Create (or replay) one user turn and schedule its worker."""
        if not str(text).strip():
            raise ValueError("turn input must not be empty")
        # Codex's ``start_or_steer_turn`` appends fresh input to an active
        # running turn instead of creating a competing turn.  This keeps a
        # fast follow-up usable while the model is still sampling.
        current = self.repo.state(thread_id).active_turn
        if current is not None:
            if current.status is not TurnStatus.RUNNING:
                raise IllegalTurnTransition(
                    current.turn_id,
                    current.status.value,
                    TurnStatus.RUNNING.value,
                    {TurnStatus.RUNNING},
                )
            item = self.repo.append_input(
                thread_id, current.turn_id, str(text), idempotency_key=idempotency_key
            )
            self.repo.emit_event(
                thread_id,
                "input/provided",
                payload={"text": str(text), "item_id": item.item_id},
                turn_id=current.turn_id,
                idempotency_key=idempotency_key,
            )
            return SessionTurn(thread_id, current.turn_id, current.status, False, mode="steer")
        before = set(self.repo.state(thread_id).turns)
        turn = self.repo.start_turn(
            thread_id,
            inputs=[{"text": str(text)}],
            idempotency_key=idempotency_key,
            model=model,
        )
        created = turn.turn_id not in before
        if created and turn.status is TurnStatus.RUNNING:
            try:
                self.start(thread_id, turn.turn_id)
            except Exception as exc:
                # A worker that cannot be admitted must never strand a running
                # turn.  Codex records the admission failure before returning
                # the error so the next user message remains usable.
                with contextlib.suppress(Exception):
                    self.repo.fail_turn(
                        thread_id,
                        turn.turn_id,
                        error={
                            "code": "runtime_unavailable",
                            "error_class": "fatal",
                            "message": str(exc),
                        },
                    )
                raise
        return SessionTurn(thread_id, turn.turn_id, turn.status, created)

    def start(self, thread_id: str, turn_id: str) -> asyncio.Task[Any]:
        """Start exactly one worker for a running turn (idempotent while live)."""
        existing = self._tasks.get(thread_id)
        if existing is not None and not existing.done():
            return existing
        task = self.host.start(thread_id, turn_id)
        self._tasks[thread_id] = task
        return task

    async def wait(self, thread_id: str, *, timeout_s: float | None = None) -> Any:
        """Join the current worker without turning a model failure into a crash."""
        task = self._tasks.get(thread_id)
        if task is None:
            return None
        try:
            if timeout_s is None:
                return await asyncio.shield(task)
            return await asyncio.wait_for(asyncio.shield(task), timeout=float(timeout_s))
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            # A cancelled host task must never leave its durable Turn running.
            # Do not swallow cancellation of the caller itself; this branch is
            # only reached when the worker task was cancelled while shielded.
            if task.cancelled() and not asyncio.current_task().cancelling():
                self._settle_worker_failure(thread_id, "task cancelled", code="cancelled")
                state = self.repo.state(thread_id)
                return state.turns[state.turn_order[-1]] if state.turn_order else None
            raise
        except Exception as exc:  # noqa: BLE001 - worker errors are durable state
            # A host implementation may surface a transport ReadError before
            # it reaches TurnRuntime.  Convert it to a terminal Turn instead
            # of propagating an exception that strands the conversation.
            code = "model_stream_error" if _is_stream_read_error(exc) else "runtime_error"
            self._settle_worker_failure(thread_id, str(exc), code=code)
            state = self.repo.state(thread_id)
            if state.turn_order:
                return state.turns[state.turn_order[-1]]
            return None

    async def interrupt(self, thread_id: str, *, reason: str = "interrupted") -> Turn:
        """Cancel the worker, then converge the durable turn to interrupted."""
        state = self.repo.state(thread_id)
        turn = state.active_turn
        if turn is None:
            # Return the last terminal turn for an idempotent stop button.
            if state.turn_order:
                return state.turns[state.turn_order[-1]]
            raise IllegalTurnTransition("", "idle", "interrupted", set())
        self.host.interrupt(thread_id, reason)
        task = self._tasks.get(thread_id)
        await self.wait(thread_id, timeout_s=5.0)
        # Token cancellation is cooperative.  A broken transport can ignore
        # it indefinitely, so cancel the worker after the bounded grace period
        # and still converge the durable state below.
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        current = self.repo.state(thread_id).turn(turn.turn_id)
        if current.status in ACTIVE_TURN_STATUSES:
            current = self.repo.interrupt_turn(thread_id, turn.turn_id, reason=reason)
        return current

    def _settle_worker_failure(self, thread_id: str, message: str, *, code: str) -> None:
        state = self.repo.state(thread_id)
        current = state.active_turn
        if current is None or current.status not in ACTIVE_TURN_STATUSES:
            return
        with contextlib.suppress(Exception):
            self.repo.fail_turn(
                thread_id,
                current.turn_id,
                error={
                    "code": code,
                    "error_class": "retryable" if code == "model_stream_error" else "fatal",
                    "message": message or code,
                },
            )

    async def continue_input(self, thread_id: str, turn_id: str, text: str) -> SessionTurn:
        """Resume a turn waiting for user input, then schedule its worker."""
        turn = self.repo.continue_turn(thread_id, turn_id, text)
        if not self.host.is_running(thread_id):
            self.start(thread_id, turn_id)
        return SessionTurn(thread_id, turn_id, turn.status, True, mode="continue")

    async def recover(self, thread_id: str, turn_id: str, *, reason: str = "resume") -> SessionTurn:
        """Resume interrupted work or retry failed work in a fresh turn."""
        state = self.repo.state(thread_id)
        turn = state.turn(turn_id)
        if turn.status is TurnStatus.INTERRUPTED:
            resumed = self.repo.resume_turn(thread_id, turn_id, reason=reason)
            if not self.host.is_running(thread_id):
                self.start(thread_id, turn_id)
            return SessionTurn(thread_id, turn_id, resumed.status, True, mode="resume")
        if turn.status is TurnStatus.FAILED:
            inputs = [
                {"text": str(item.payload.get("text") or "")}
                for item in state.items_for_turn(turn_id)
                if item.type.value == "user_input"
            ]
            retried = self.repo.start_turn(thread_id, inputs=inputs or [{"text": ""}])
            self.start(thread_id, retried.turn_id)
            return SessionTurn(
                thread_id, retried.turn_id, retried.status, True,
                mode="retry", retried_from=turn_id,
            )
        raise IllegalTurnTransition(
            turn_id,
            turn.status.value,
            "running",
            {TurnStatus.INTERRUPTED, TurnStatus.FAILED},
        )

    # -------------------------------------------------------------- model view
    def context(self, thread_id: str) -> list[dict[str, Any]]:
        """Build the exact summary+tail context sent to the next model call."""
        return build_context(self.repo.state(thread_id), system_prompt=self.system_prompt)

    def context_info(self, thread_id: str) -> dict[str, Any]:
        messages = self.context(thread_id)
        return {"messages": messages, "estimated_tokens": estimate_tokens(messages)}

    def tool_specs(self, thread_id: str) -> list[dict[str, Any]]:
        runtime = self.host.runtime_for(thread_id)
        specs = runtime.runtime.tools.specs()
        return [item.to_dict() for item in specs]

    async def compact(self, thread_id: str, *, trigger: str = "manual") -> Any:
        service = self.compaction
        if service is None:
            runtime = self.host.runtime_for(thread_id)
            service = getattr(runtime.runtime, "compaction", None)
        if service is None:
            raise RuntimeError("compaction is not configured")
        return await service.compact(thread_id, trigger=trigger)

    # --------------------------------------------------------------- children
    def send_message(self, from_thread_id: str, to_thread_id: str, content: str, **kwargs: Any) -> dict[str, Any]:
        if self.tree is None:
            raise RuntimeError("agent tree is not configured")
        return self.tree.send_message(from_thread_id, to_thread_id, content, **kwargs)

    async def wait_children(self, thread_ids: list[str], *, timeout_s: float | None = None) -> dict[str, Any]:
        if self.tree is None:
            raise RuntimeError("agent tree is not configured")
        return await self.tree.wait_for(thread_ids, timeout_s=timeout_s)

    # --------------------------------------------------------------- snapshots
    def snapshot(self, thread_id: str) -> dict[str, Any]:
        state = self.repo.state(thread_id)
        return {
            "thread": state.thread.to_dict(),
            "active_turn": state.active_turn.to_dict() if state.active_turn else None,
            "turns": [state.turns[item].to_dict() for item in state.turn_order],
            "items": [state.items[item].to_dict() for item in state.item_order],
            "context": self.context_info(thread_id),
            "running": self.host.is_running(thread_id),
        }

    async def shutdown(self) -> None:
        for thread_id in list(self._tasks):
            with contextlib.suppress(Exception):
                await self.interrupt(thread_id, reason="session_shutdown")
        self._tasks.clear()


def _is_stream_read_error(exc: BaseException) -> bool:
    """Recognize transport read failures without importing a provider library."""
    names = {type(exc).__name__.lower()}
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        names.add(type(cause).__name__.lower())
    return any(name in {"readerror", "incompleteread", "endofstream", "brokenresourceerror"} for name in names)


__all__ = ["CodexSession", "SessionTurn", "RuntimeHost"]
