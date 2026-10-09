"""Production runtime assembly for Agent v2.

The API service keeps the offline fixture host for tests, while the running
application uses this host.  It connects the frozen TurnRuntime contract to
the repository's real model router and Agent 2's host tools without changing
the protocol or frontend.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock
from contracts.agent_v2.models import ToolCall
from services.agent_compaction_v2 import CompactionService
from services.agent_memory_v2 import MemoryStore
from services.agent_runtime_v2 import ToolScheduler, TurnRuntime
from services.agent_runtime_v2.production_tools import build_tool_bundle
from services.agent_threads_v2 import AgentTree, ThreadRepository
from services.host_execution_v2 import HostExecutionManager
from services.model_gateway_v2 import ModelGateway, RetryPolicy
from services.model_gateway_v2.provider import RegisteredModelProvider
from services.sandbox_v2 import SandboxPolicy

from .host import RuntimeUnavailable


@dataclass(frozen=True)
class ProductionScenario:
    name: str = "production"
    runtime_available: bool = True


@dataclass
class _Runtime:
    execution: HostExecutionManager
    bundle: Any
    runtime: TurnRuntime
    task: asyncio.Task[Any] | None = None
    token: CancelToken | None = None
    outcome: Any = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class ProductionRuntimeHost:
    """Real model/tool runtime host used by the application process."""

    def __init__(
        self,
        *,
        repo: ThreadRepository,
        tree: AgentTree,
        memory: MemoryStore,
        clock: Clock,
        system_prompt: str | None = None,
        token_budget: int = 6000,
        max_iterations: int = 8,
        records_root: str | Path | None = None,
    ) -> None:
        self.repo = repo
        self.tree = tree
        self.memory = memory
        self.clock = clock
        self.system_prompt = system_prompt
        self.token_budget = int(token_budget)
        self.max_iterations = int(max_iterations)
        self.records_root = Path(records_root or (repo.root.parent / "executions"))
        self._runtimes: dict[str, _Runtime] = {}
        self._closed = False

    def scenario_names(self) -> list[str]:
        return ["production"]

    def scenario_for(self, thread_id: str) -> ProductionScenario:
        del thread_id
        return ProductionScenario()

    def _cwd(self, thread_id: str) -> Path:
        thread = self.repo.state(thread_id).thread
        configured = thread.cwd or (thread.settings or {}).get("cwd")
        return Path(str(configured)).expanduser().resolve() if configured else self.repo.root.resolve()

    def runtime_for(self, thread_id: str) -> _Runtime:
        if self._closed:
            raise RuntimeUnavailable("production runtime host is closed")
        existing = self._runtimes.get(thread_id)
        if existing is not None:
            return existing
        if not self.repo.exists(thread_id):
            raise RuntimeUnavailable(f"thread {thread_id} does not exist")

        cwd = self._cwd(thread_id)
        settings = self.repo.state(thread_id).thread.settings or {}
        full_access = bool(settings.get("full_access") or settings.get("danger_full_access"))
        sandbox_policy = SandboxPolicy.DANGER_FULL_ACCESS if full_access else SandboxPolicy.WORKSPACE_WRITE
        # Each thread owns a sandbox policy; execution records remain in one
        # repository-local directory so restart scans see every process.
        from services.sandbox_v2 import SandboxManager

        sandbox = SandboxManager(cwd, policy=sandbox_policy)
        execution = HostExecutionManager(
            self.records_root,
            clock=self.clock,
            sandbox=sandbox,
        )
        bundle = build_tool_bundle(
            self.repo,
            self.tree,
            self.memory,
            thread_id,
            execution,
            clock=self.clock,
        )
        thread_model = self.repo.state(thread_id).thread.model
        provider = RegisteredModelProvider(model_ref=thread_model or None, stage="agent")
        gateway = ModelGateway(provider, clock=self.clock)
        scheduler = ToolScheduler(bundle.executor, clock=self.clock)
        runtime = TurnRuntime(
            repo=self.repo,
            gateway=gateway,
            tools=scheduler,
            compaction=CompactionService(
                repo=self.repo,
                gateway=gateway,
                clock=self.clock,
                token_budget=self.token_budget,
                system_prompt=self.system_prompt,
            ),
            clock=self.clock,
            system_prompt=self.system_prompt,
            max_iterations=self.max_iterations,
            retry=RetryPolicy(max_attempts=3),
            approval_gate=bundle.approval_gate,
        )
        value = _Runtime(execution=execution, bundle=bundle, runtime=runtime)
        self._runtimes[thread_id] = value
        return value

    def start(self, thread_id: str, turn_id: str, *, register_cancel: bool = True) -> asyncio.Task[Any]:
        value = self.runtime_for(thread_id)
        if value.running:
            raise RuntimeUnavailable(f"thread {thread_id} already has a running turn")
        token = CancelToken()
        value.token = token
        if register_cancel:
            self.tree.register_cancel(thread_id, token)

        async def drive() -> Any:
            try:
                value.outcome = await value.runtime.run(thread_id, turn_id, cancel=token)
                return value.outcome
            except asyncio.CancelledError:
                self.repo.release_turn(thread_id, reason="task-cancelled")
                raise
            finally:
                if register_cancel:
                    self.tree.unregister_cancel(thread_id)

        value.task = asyncio.ensure_future(drive())
        return value.task

    def interrupt(self, thread_id: str, reason: str = "interrupted") -> bool:
        value = self._runtimes.get(thread_id)
        if value is None or not value.running:
            return False
        if value.token is not None:
            value.token.cancel(reason)
        with contextlib.suppress(Exception):
            turn = self.repo.state(thread_id).active_turn
            if turn is not None:
                value.execution.interrupt_turn(thread_id, turn.turn_id, reason=reason)
        return True

    def is_running(self, thread_id: str) -> bool:
        value = self._runtimes.get(thread_id)
        return bool(value and value.running)

    async def await_idle(self, thread_id: str, *, timeout_s: float = 10.0) -> Any:
        value = self._runtimes.get(thread_id)
        if value is None or value.task is None:
            return None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(value.task), timeout=timeout_s)
        return value.outcome

    def approval_gate(self, call: ToolCall) -> str:
        return self.runtime_for(call.thread_id).bundle.approval_gate(call)

    def resolve_approval(self, thread_id: str, approval_id: str, decision: str, *, by: str = "user") -> dict[str, Any]:
        value = self.runtime_for(thread_id)
        # AgentFacade performs the single authoritative repository/Turn
        # transition after this tool-domain state is synchronized.
        return value.bundle.resolve_approval(
            approval_id,
            decision,
            by=by,
            sync_repository=False,
        )

    def runtime_info(self, thread_id: str) -> dict[str, Any]:
        value = self._runtimes.get(thread_id)
        if value is None:
            return {"thread_id": thread_id, "running": False, "scenario": "production"}
        provider = getattr(value.runtime.gateway, "provider", None)
        return {
            "thread_id": thread_id,
            "running": value.running,
            "scenario": "production",
            "model_calls": int(getattr(provider, "call_count", 0)),
            "tools": len(value.bundle.registry.names()),
        }

    async def shutdown(self) -> None:
        self._closed = True
        tasks = [item.task for item in self._runtimes.values() if item.task and not item.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._runtimes.clear()


__all__ = ["ProductionRuntimeHost", "ProductionScenario"]
