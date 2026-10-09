"""Production assembly for the v2 tool runtime.

This module is intentionally an adapter.  The tool declarations and their
validation remain in :mod:`services.tool_registry_v2`; this file supplies the
objects that are only known by the application process (thread repository,
agent tree, memory store and host executor).

``build_tool_bundle`` is the integration seam used by a production runtime:
the returned ``executor`` implements the Agent 1 ``ToolExecutor`` protocol and
``approval_gate`` implements the Agent 1 approval gate protocol.  Approval
requests written by the repository are adopted by ``ApprovalManager`` lazily
by call id, so the repository and the tool domain always operate on one
approval id.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ApprovalStatus, EventType, ToolCallStatus
from contracts.agent_v2.models import ToolCall, ToolResult
from services.agent_memory_v2 import MemoryStore
from services.agent_threads_v2 import AgentTree, ThreadRepository
from services.approval_v2 import ApprovalManager
from services.host_execution_v2 import HostExecutionManager
from services.sandbox_v2 import SandboxManager, SandboxPolicy
from services.tool_registry_v2 import ToolGateway, ToolRegistry
from services.tool_registry_v2.builtin import build_default_registry
from services.tool_registry_v2.builtin.host_tools import host_command_definition
from services.tool_registry_v2.models import ToolContext


def _awaitable(value: Any) -> bool:
    return inspect.isawaitable(value)


async def _call(value: Any, *args: Any, **kwargs: Any) -> Any:
    result = value(*args, **kwargs)
    return await result if _awaitable(result) else result


def _setting(repo: ThreadRepository, thread_id: str, *keys: str, default: Any = None) -> Any:
    """Read a thread setting without requiring a particular settings schema."""
    try:
        thread = repo.state(thread_id).thread
    except Exception:  # noqa: BLE001 - a malformed/removed thread is handled by callers
        return default
    settings = dict(thread.settings or {})
    for key in keys:
        if key in settings:
            return settings[key]
    for key in keys:
        value = getattr(thread, key, None)
        if value is not None:
            return value
    return default


def _thread_cwd(repo: ThreadRepository, thread_id: str, host: Any) -> Path:
    configured = _setting(repo, thread_id, "cwd", "working_directory", "workspace", default=None)
    if configured:
        return Path(str(configured)).expanduser().resolve()
    sandbox = getattr(host, "sandbox", None)
    root = getattr(sandbox, "workspace_root", None)
    if root:
        return Path(root).resolve()
    return Path(repo.root).resolve()


def _full_access(repo: ThreadRepository, thread_id: str) -> bool:
    return bool(
        _setting(
            repo,
            thread_id,
            "full_access",
            "danger_full_access",
            "danger-full-access",
            default=False,
        )
    )


def _emit_repo(repo: ThreadRepository, call: ToolCall, chunk: str) -> None:
    if not chunk:
        return
    repo.emit_event(
        call.thread_id,
        EventType.TOOL_OUTPUT,
        payload={"name": call.name, "chunk": str(chunk)},
        turn_id=call.turn_id,
        call_id=call.call_id,
    )


async def _run_host_command(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Production command handler with incremental event emission."""
    argv = [str(item) for item in args.get("argv") or []]
    if not argv:
        return {"__error__": {"code": "invalid_arguments", "message": "argv 不能为空"}}
    if ctx.host is None or not callable(getattr(ctx.host, "execute", None)):
        return {"__error__": {"code": "backend_unavailable", "message": "宿主执行器未接入"}}
    cwd = str(args.get("cwd") or ctx.cwd)
    if ctx.sandbox is not None:
        verdict = ctx.sandbox.check_command_paths(argv, cwd=cwd)
        if verdict.verdict.denied:
            return {
                "__error__": {
                    "code": "sandbox_denied",
                    "message": verdict.verdict.reason,
                    "verdict": verdict.verdict.to_dict(),
                }
            }
        if verdict.verdict.needs_approval:
            return {
                "__error__": {
                    "code": "approval_required",
                    "message": verdict.verdict.reason,
                    "verdict": verdict.verdict.to_dict(),
                }
            }

    def output(channel: str, chunk: str) -> None:
        del channel
        emitter = ctx.service("emit_tool_output")
        if emitter is not None:
            with contextlib.suppress(Exception):
                emitter(ctx, chunk)

    timeout = args.get("timeout_ms")
    record = await ctx.host.execute(
        argv,
        thread_id=ctx.thread_id,
        turn_id=ctx.turn_id,
        call_id=ctx.call_id,
        cwd=cwd,
        env=dict(args.get("env") or {}) or None,
        timeout_s=float(timeout) / 1000.0 if timeout else None,
        cancel=ctx.service("cancel_token"),
        on_output=output,
        sandbox_check=False,
    )
    result = record.to_tool_output()
    if record.status.value not in {"succeeded"}:
        error = dict(record.error or {})
        error.setdefault("code", record.status.value)
        error.setdefault("message", f"命令未成功完成（{record.status.value}）")
        result["__error__"] = error
    return result


def _service_from(obj: Any, *names: str) -> Any:
    for name in names:
        candidate = getattr(obj, name, None)
        if callable(candidate):
            return candidate
    return None


async def _default_search(query: str, mode: str = "web", limit: int = 10) -> dict[str, Any]:
    from services.agent.web_search import search_academic, search_web

    fn = search_academic if mode == "academic" else search_web
    return await fn(query, limit=limit)


async def _default_fetch(url: str, max_bytes: int = 512 * 1024) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        raw = response.content[:max_bytes]
        return {
            "url": str(response.url),
            "status_code": response.status_code,
            "bytes": len(response.content),
            "text": raw.decode("utf-8", errors="replace"),
            "truncated": len(response.content) > max_bytes,
        }


def _memory_search(memory: MemoryStore, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").casefold()
    limit = max(1, min(int(payload.get("limit") or 50), 200))
    rows = memory.conversation(thread_id)
    if query:
        rows = [row for row in rows if query in row.text.casefold()]
    return {"count": min(len(rows), limit), "records": [row.to_dict() for row in rows[:limit]]}


def _memory_write(memory: MemoryStore, thread_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = memory.write(
        "conversation",
        thread_id,
        str(payload.get("content") or ""),
        origin=str(payload.get("origin") or "auto"),
        creator=str(payload.get("creator") or "agent"),
        confidence=float(payload.get("confidence") or 0.5),
        tags=[str(item) for item in payload.get("tags") or []],
    )
    return {"memory_id": record.memory_id, "record": record.to_dict()}


async def _skill_list(folder: str | None = None) -> dict[str, Any]:
    from services.skills.registry import catalog, scan

    entries = catalog(scan(folder) if folder else scan())
    return {"skills": [item.as_dict() for item in entries], "count": len(entries)}


@dataclass
class ProductionToolBundle:
    """Objects consumed by Agent 1 and exposed to the API integration."""

    registry: ToolRegistry
    gateway: ToolGateway
    approvals: ApprovalManager
    sandbox: SandboxManager
    executor: Any
    approval_gate: Callable[[ToolCall], str]

    async def execute(self, call: ToolCall, cancel: CancelToken | None = None) -> ToolResult:
        return await self.executor.execute(call, cancel)

    def cancel(self, call_id: str, *, reason: str = "cancelled") -> dict[str, Any]:
        self.gateway.cancel(call_id, reason=reason)
        view = self.approvals.by_call(call_id)
        if view is None:
            try:
                state = self._repo.state(self._thread_id)  # type: ignore[attr-defined]
                raw = next((item for item in state.approvals.values() if item.call_id == call_id), None)
                if raw is not None:
                    view = self.approvals.adopt(raw)
            except Exception:
                view = None
        if view is not None and view.pending:
            with contextlib.suppress(Exception):
                view = self.approvals.cancel(view.approval_id, reason=reason)
        if view is not None:
            with contextlib.suppress(Exception):
                state = self._repo.state(view.thread_id)  # type: ignore[attr-defined]
                if view.approval_id in state.approvals:
                    self._repo.resolve_approval(  # type: ignore[attr-defined]
                        view.thread_id,
                        view.turn_id,
                        view.approval_id,
                        granted=False,
                        scope=view.decision_scope or "cancelled",
                        decided_by="system",
                    )
        return {"ok": True, "call_id": call_id, "reason": reason}

    def set_full_access(self, enabled: bool) -> None:
        self.approvals.set_full_access(enabled)
        self.sandbox.set_policy(
            SandboxPolicy.DANGER_FULL_ACCESS if enabled else SandboxPolicy.WORKSPACE_WRITE
        )
        # Persist the per-thread choice as a normal thread update.  Rebuilding
        # a bundle after a process restart must not silently revert access.
        try:
            state = self._repo.state(self._thread_id)  # type: ignore[attr-defined]
            settings = dict(state.thread.settings or {})
            settings["full_access"] = bool(enabled)
            self._repo.emit_event(  # type: ignore[attr-defined]
                self._thread_id,
                EventType.THREAD_UPDATED,
                payload={"patch": {"settings": settings}},
            )
        except Exception:
            pass

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        by: str = "owner",
        sync_repository: bool = True,
    ) -> dict[str, Any]:
        """Resolve the tool approval and optionally mirror it to the event store.

        The API facade owns the authoritative Turn transition.  Production host
        calls set ``sync_repository=False`` so that the same approval cannot be
        resolved twice when the facade resumes the turn.
        """
        view = self.approvals.view(approval_id)
        if view is None:
            # Agent 1 creates the repository approval after the gate returns
            # ``require``.  Adopt that object on the UI resolution path so
            # there is never a second approval id in the tool domain.
            try:
                state = self._repo.state(self._thread_id)  # type: ignore[attr-defined]
                raw = state.approvals.get(approval_id)
            except Exception:
                raw = None
            if raw is None:
                return {"ok": False, "error": "approval_not_found", "approval_id": approval_id}
            view = self.approvals.adopt(raw)
        try:
            if decision == "approve_once":
                updated = self.approvals.approve_once(approval_id, by=by)
                granted = True
            elif decision == "approve_for_thread":
                updated, _ = self.approvals.approve_for_thread(approval_id, by=by)
                granted = True
            elif decision == "deny":
                updated = self.approvals.deny(approval_id, by=by)
                granted = False
                self._denied_calls.add(updated.call_id)
            elif decision == "cancel":
                updated = self.approvals.cancel(approval_id, by=by)
                granted = False
                self._denied_calls.add(updated.call_id)
            elif decision == "expire":
                updated = self.approvals.expire(approval_id)
                granted = False
                self._denied_calls.add(updated.call_id)
            elif decision == "escalate_full_access":
                updated = self.approvals.escalate_full_access(approval_id, by=by, sandbox=self.sandbox)
                self.set_full_access(True)
                granted = True
            else:
                return {"ok": False, "error": f"unknown_decision:{decision}"}
        except Exception as exc:  # noqa: BLE001 - API boundary is structured
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # The repository owns Turn state; adopting first keeps the exact id and
        # makes a denied result eligible for execution/feed-back rather than a
        # second approval suspension.
        if sync_repository:
            try:
                state = self._repo.state(updated.thread_id)  # type: ignore[attr-defined]
                if updated.approval_id in state.approvals:
                    self._repo.resolve_approval(  # type: ignore[attr-defined]
                        updated.thread_id,
                        updated.turn_id,
                        updated.approval_id,
                        granted=granted,
                        scope=updated.decision_scope or decision,
                        decided_by=by,
                    )
            except Exception:
                pass
        return {"ok": True, "approval": updated.to_dict(), "decision": decision}


class _ProductionExecutor:
    """ToolExecutor adapter adding denial feed-back and cancel-token context."""

    def __init__(self, registry: ToolRegistry, bundle: ProductionToolBundle) -> None:
        self.registry = registry
        self.bundle = bundle

    def specs(self) -> list[Any]:
        return self.registry.specs()

    def spec(self, name: str) -> Any:
        return self.registry.spec(name)

    async def execute(self, call: ToolCall, cancel: CancelToken | None = None) -> ToolResult:
        denied = self.bundle._denied_calls  # type: ignore[attr-defined]
        if call.call_id in denied:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.FAILED,
                error={"code": "approval_denied", "message": "研究者拒绝了该操作"},
            )
        services = self.registry.services
        services["cancel_token"] = cancel
        return await self.registry.execute(call, cancel)


def build_tool_bundle(
    repo: ThreadRepository,
    tree: AgentTree,
    memory: MemoryStore,
    thread_id: str,
    host: HostExecutionManager,
    clock: Clock | None = None,
) -> ProductionToolBundle:
    """Build a production registry/gateway for one thread.

    A bundle is intentionally thread-scoped: cwd, sandbox policy and full
    access are read from that thread and cannot leak into another conversation.
    """
    clock = clock or getattr(repo, "clock", None) or SystemClock()
    cwd = _thread_cwd(repo, thread_id, host)
    full_access = _full_access(repo, thread_id)
    sandbox = SandboxManager(
        cwd,
        policy=SandboxPolicy.DANGER_FULL_ACCESS if full_access else SandboxPolicy.WORKSPACE_WRITE,
    )
    approval_root = Path(getattr(repo, "root", cwd)) / ".agent-v2" / "approvals"
    approvals = ApprovalManager(clock=clock, sandbox=sandbox, full_access=full_access, approvals_path=approval_root)

    # Shared mutable set is deliberately private to the bundle.  A denied call
    # is released by the gate exactly once and then fed back as a tool result.
    denied_calls: set[str] = set()
    services: dict[str, Any] = {
        "emit_tool_output": lambda ctx, chunk: _emit_repo(repo, _call_from_context(ctx), chunk),
        "knowledge_search": lambda payload: _memory_search(memory, thread_id, payload),
        "knowledge_write": lambda payload: _memory_write(memory, thread_id, payload),
        "skill_list": _skill_list,
    }
    services["search"] = _default_search
    services["fetch"] = _default_fetch

    async def skill_run(name: str, args: list[str], skill_cwd: str | None = None) -> dict[str, Any]:
        from services.skills.registry import scan

        pack = next((item for item in scan() if item.name == name), None)
        if pack is None or not pack.path:
            return {"__error__": {"code": "skill_not_found", "message": name}}
        if not pack.steps:
            return {"name": name, "runnable": False, "message": "该技能是说明型技能，没有可执行脚本"}
        script = pack.path / pack.steps[0].script
        record = await host.execute(
            [str(script), *args],
            thread_id=thread_id,
            cwd=skill_cwd or str(cwd),
            sandbox_check=True,
        )
        return record.to_tool_output()

    services["skill_run"] = skill_run
    mcp = _service_from(host, "mcp_call", "call_mcp") or _service_from(repo, "mcp_call", "call_mcp")
    if mcp is not None:
        services["mcp_call"] = mcp

    registry = build_default_registry(
        clock=clock,
        sandbox=sandbox,
        host=host,
        approvals=approvals,
        agent_tree=tree,
        cwd=str(cwd),
        services=services,
    )
    command = host_command_definition().with_handler(_run_host_command)
    registry.register(command, replace=True)

    bundle = ProductionToolBundle.__new__(ProductionToolBundle)
    bundle.registry = registry
    bundle.approvals = approvals
    bundle.sandbox = sandbox
    bundle._repo = repo  # type: ignore[attr-defined]
    bundle._thread_id = thread_id  # type: ignore[attr-defined]
    bundle._denied_calls = denied_calls  # type: ignore[attr-defined]
    bundle.gateway = ToolGateway(registry, sandbox=sandbox, approvals=approvals, host=host)

    # Keep the existing facade API usable by callers that only retain the
    # gateway.  Its normal resolver updates the ApprovalManager; this adapter
    # additionally resumes the repository Turn with the same approval id.
    def gateway_resolve(command: dict[str, Any]) -> dict[str, Any]:
        return bundle.resolve_approval(
            str(command.get("approval_id") or ""),
            str(command.get("decision") or ""),
            by=str(command.get("by") or "owner"),
        )

    bundle.gateway.resolve_approval = gateway_resolve  # type: ignore[method-assign]

    def gate(call: ToolCall) -> str:
        # Adopt the repository object if the runtime has just created it.
        try:
            state = repo.state(call.thread_id)
            approval = next((item for item in state.approvals.values() if item.call_id == call.call_id), None)
            if approval is not None:
                adopted = approvals.adopt(approval)
                if adopted.cwd is None:
                    # Agent 1's generic wait event may omit cwd when the
                    # command used the thread default.  Preserve that fact in
                    # the tool-domain view so approve_for_thread can create a
                    # correctly scoped grant.
                    adopted = replace(adopted, cwd=str(cwd))
                    approvals.adopt(adopted)
                if adopted.status == ApprovalStatus.DENIED.value:
                    denied_calls.add(call.call_id)
                    return "allow"
                if adopted.status == ApprovalStatus.GRANTED.value:
                    return "allow"
        except Exception:
            pass
        return registry.approval_gate(thread_id)(call)

    bundle.approval_gate = gate
    bundle.executor = _ProductionExecutor(registry, bundle)

    # Give child-agent tools a real scheduling hook when the host exposes the
    # runtime start API.  The hook is optional and never fabricates completion.
    async def agent_runner(child_thread_id: str, task: str) -> dict[str, Any]:
        start = getattr(host, "start", None)
        if not callable(start):
            return {"status": "spawned", "thread_id": child_thread_id}
        turn = repo.start_turn(child_thread_id, inputs=[{"text": task}])
        result = start(child_thread_id, turn.turn_id)
        if _awaitable(result):
            await result
        return {"status": "scheduled", "thread_id": child_thread_id, "turn_id": turn.turn_id}

    registry.bind(agent_runner=agent_runner)
    return bundle


def _call_from_context(ctx: ToolContext) -> ToolCall:
    """Reconstruct the small call identity needed by the output emitter."""
    from contracts.agent_v2.enums import ToolCallStatus, ToolKind

    return ToolCall(
        call_id=ctx.call_id,
        name="host.command",
        kind=ToolKind.SIDE_EFFECT,
        status=ToolCallStatus.RUNNING,
        thread_id=ctx.thread_id,
        turn_id=ctx.turn_id,
    )


ToolBundle = ProductionToolBundle

__all__ = ["ProductionToolBundle", "ToolBundle", "build_tool_bundle"]
