# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
"""假运行时宿主（Agent 3 的离线执行后端）。

Agent 1 提供执行内核（``TurnRuntime``），但**不提供任何具体工具**；Agent 2 提供真实工具、
审批与宿主机执行。本模块在两者之间补上一层**可离线运行**的宿主：

- 模型供应商固定为 :class:`~contracts.agent_v2.fake.FakeProvider`，脚本来自 JSONL fixture；
- 工具执行器是本文件内的 :class:`ScenarioToolExecutor`，同样由 fixture 驱动，
  只实现 ``read_file`` / ``list_dir`` / ``write_file`` / ``run_command`` / ``spawn_agent``
  这几个**假**能力，绝不触碰宿主机文件系统（验收书 §7「测试不依赖真实模型、真实网络
  或宿主机破坏性命令」）；
- 审批门 :meth:`FakeRuntimeHost.approval_gate` 读取线程上已生效的授权规则，实现
  「批准一次 / 本对话始终批准（规范化前缀）/ 完全访问」三种语义。

它同时服务两个用途：一是测试，二是离线演示（前端可以对着它跑完整交互）。
接入真实运行时后，只需把 :class:`FakeRuntimeHost` 换成协调 Agent 的宿主实现，
API 层与前端**一行都不用改**。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ApprovalStatus, EventType, ItemType, ToolCallStatus, ToolKind
from contracts.agent_v2.errors import ThreadNotFound
from contracts.agent_v2.models import ToolCall, ToolResult, ToolSpec
from services.agent_compaction_v2 import CompactionService
from services.agent_runtime_v2 import ALLOW, REQUIRE, ToolScheduler, TurnRuntime
from services.agent_threads_v2 import AgentTree, ThreadRepository
from services.model_gateway_v2 import ModelGateway, RetryPolicy

from .fixtures import Scenario, available_scenarios, load_scenario, scenario_dir

#: 线程设置里指定场景的键（``thread/start`` 的 ``settings.scenario``）。
SCENARIO_SETTING = "scenario"

#: 授权规则存放位置：线程 ``settings`` 下的一个列表。
PERMISSION_RULES_KEY = "permission_rules"

#: 完全访问模式开关：线程 ``settings`` 下的布尔值。
FULL_ACCESS_KEY = "full_access"

#: 规范化命令前缀匹配时忽略的参数（这些参数每次都不一样，纳入前缀会让规则永不命中）。
_PREFIX_IGNORED_ARGS = frozenset({"timeout_s", "cwd", "workdir", "env"})

#: 子 Agent 工具名（Agent 2 的正式入口同名；这里给出可离线运行的假实现）。
SPAWN_TOOL_NAME = "spawn_agent"

#: 计划工具名：把模型给出的计划落成 ``item/added`` 的 PLAN 项。
PLAN_TOOL_NAME = "update_plan"

SPAWN_TOOL_SPEC = ToolSpec(
    name=SPAWN_TOOL_NAME,
    kind=ToolKind.SIDE_EFFECT,
    description="创建子 Agent 执行一个子任务，并把结果回传父 Agent",
)

PLAN_TOOL_SPEC = ToolSpec(
    name=PLAN_TOOL_NAME,
    kind=ToolKind.SIDE_EFFECT,
    description="更新任务计划（计划只是事件流里的一个 Item，不阻塞执行）",
)


class RuntimeUnavailable(RuntimeError):
    """运行时不可用（例如宿主已关闭）。"""


def normalize_command_prefix(arguments: dict[str, Any], *, max_items: int = 3) -> list[str]:
    """把工具参数规范化成稳定前缀（用于「当前对话始终批准当前命令」规则）。

    取前 ``max_items`` 个「稳定」参数，按参数名排序后用 ``name=value`` 表示。
    只做字符串化与截断，不做任何猜测性归一（避免把不同命令折叠成同一条规则）。
    """
    stable = {
        key: str(value)
        for key, value in arguments.items()
        if key not in _PREFIX_IGNORED_ARGS and not isinstance(value, (dict, list))
    }
    return [f"{key}={stable[key]}" for key in sorted(stable)[:max_items]]


def prefix_matches(rule_prefix: list[str], arguments: dict[str, Any]) -> bool:
    """判断一次调用是否被某条规范化前缀规则覆盖（前缀是「以这些 name=value 开头」）。"""
    if not rule_prefix:
        return True
    return normalize_command_prefix(arguments, max_items=len(rule_prefix)) == list(rule_prefix)


#: 未指定场景时的首选 fixture（最简单、无工具、无审批）。
PREFERRED_DEFAULT_SCENARIO = "text_multi_turn"


@dataclass
class ScenarioToolExecutor:
    """fixture 驱动的假工具执行器（实现 ``ToolExecutor`` 协议）。

    - 结果来自 ``scenario.tool_results``；未声明的工具返回成功空输出；
    - ``tool_delays`` 制造时间窗，用于观测「只读并行 / 副作用串行」；
    - ``spawn_agent`` 走 :meth:`FakeRuntimeHost.spawn_child`，让 Agent Tree 真正长出来。
    """

    scenario: Scenario
    host: Any = None
    clock: Clock = field(default_factory=SystemClock)
    calls: list[ToolCall] = field(default_factory=list)
    max_concurrency: int = 0
    _active: int = 0

    # ---- 协议实现 ----
    def specs(self) -> list[ToolSpec]:
        specs = self.scenario.specs()
        names = {spec.name for spec in specs}
        if SPAWN_TOOL_NAME not in names:
            specs = [*specs, SPAWN_TOOL_SPEC]
        if PLAN_TOOL_NAME not in names:
            specs = [*specs, PLAN_TOOL_SPEC]
        return specs

    def spec(self, name: str) -> ToolSpec | None:
        for spec in self.specs():
            if spec.name == name:
                return spec
        return None

    def calls_for(self, name: str) -> list[ToolCall]:
        return [call for call in self.calls if call.name == name]

    async def execute(self, call: ToolCall, cancel: Any = None) -> ToolResult:
        self._active += 1
        self.max_concurrency = max(self.max_concurrency, self._active)
        started = self.clock.now_iso()
        try:
            if cancel is not None and getattr(cancel, "cancelled", False):
                return self._result(
                    call,
                    ToolCallStatus.CANCELLED,
                    error={"code": "cancelled", "message": "cancelled before execution"},
                    started=started,
                )
            delay = float(self.scenario.tool_delays.get(call.name, 0.0))
            if delay:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
            if cancel is not None and getattr(cancel, "cancelled", False):
                return self._result(
                    call,
                    ToolCallStatus.CANCELLED,
                    error={"code": "cancelled", "message": "cancelled during execution"},
                    started=started,
                )
            self.calls.append(call)
            denied = self.host.denial_for(call) if self.host is not None else None
            if denied is not None:
                # 被拒绝的调用必须回喂真实原因，绝不能报「成功」
                return self._result(call, ToolCallStatus.FAILED, error=dict(denied), started=started)
            if call.name == SPAWN_TOOL_NAME:
                return self._run_spawn(call, started=started)
            if call.name == PLAN_TOOL_NAME:
                return self._run_plan(call, started=started)
            if self.host is not None:
                for chunk in self.scenario.tool_outputs.get(call.name, []):
                    self.host.emit_tool_output(call, chunk)
            status, output = self.scenario.tool_results.get(
                call.name, (ToolCallStatus.SUCCEEDED.value, {"echo": dict(call.arguments)})
            )
            status_enum = ToolCallStatus(status)
            if status_enum is ToolCallStatus.SUCCEEDED:
                return self._result(call, status_enum, output=dict(output), started=started)
            return self._result(
                call,
                status_enum,
                error={"code": status_enum.value, "message": f"scripted {status_enum.value}"},
                started=started,
            )
        finally:
            self._active -= 1

    # ---- 内部 ----
    def _result(
        self,
        call: ToolCall,
        status: ToolCallStatus,
        *,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        started: str,
    ) -> ToolResult:
        finished = self.clock.now_iso()
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            status=status,
            output=output,
            error=error,
            started_at=started,
            finished_at=finished,
            duration_ms=_elapsed_ms(started, finished),
        )

    def _run_spawn(self, call: ToolCall, *, started: str) -> ToolResult:
        if self.host is None:
            return self._result(
                call,
                ToolCallStatus.FAILED,
                error={"code": "runtime_unavailable", "message": "spawn_agent needs a runtime host"},
                started=started,
            )
        try:
            output = self.host.spawn_child(call)
        except ThreadNotFound as exc:
            return self._result(
                call,
                ToolCallStatus.FAILED,
                error={"code": "thread_not_found", "message": str(exc)},
                started=started,
            )
        return self._result(call, ToolCallStatus.SUCCEEDED, output=output, started=started)

    def _run_plan(self, call: ToolCall, *, started: str) -> ToolResult:
        if self.host is None:
            return self._result(
                call,
                ToolCallStatus.FAILED,
                error={"code": "runtime_unavailable", "message": "update_plan needs a runtime host"},
                started=started,
            )
        output = self.host.record_plan(call)
        return self._result(call, ToolCallStatus.SUCCEEDED, output=output, started=started)


def _elapsed_ms(started: str, finished: str) -> int:
    """由两个 ISO 时间戳求毫秒差（解析失败返回 0，绝不抛）。"""
    try:
        from datetime import datetime

        begin = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((end - begin).total_seconds() * 1000))


@dataclass
class ThreadRuntime:
    """一个线程的运行时装配（provider / gateway / scheduler / runtime / task）。"""

    thread_id: str
    scenario: Scenario
    executor: ScenarioToolExecutor
    gateway: ModelGateway
    scheduler: ToolScheduler
    runtime: TurnRuntime
    task: asyncio.Task[Any] | None = None
    token: Any = None
    outcome: Any = None

    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "scenario": self.scenario.name,
            "running": self.is_running(),
            "tool_calls": len(self.executor.calls),
            "max_concurrency": self.executor.max_concurrency,
            "model_calls": self.gateway.call_count,
        }


class FakeRuntimeHost:
    """离线运行时宿主：按线程装配 fake 运行时，并用 asyncio 任务驱动 Turn。"""

    def __init__(
        self,
        *,
        repo: ThreadRepository,
        tree: AgentTree,
        clock: Clock | None = None,
        fixtures_dir: Path | None = None,
        default_scenario: str | None = None,
        system_prompt: str | None = None,
        token_budget: int = 6000,
        max_iterations: int = 8,
    ) -> None:
        self.repo = repo
        self.tree = tree
        self.clock = clock or repo.clock
        self.fixtures_dir = fixtures_dir or scenario_dir()
        self.default_scenario = default_scenario
        self.system_prompt = system_prompt
        self.token_budget = token_budget
        self.max_iterations = max_iterations
        self._runtimes: dict[str, ThreadRuntime] = {}
        self._closed = False
        self._spawned: dict[str, str] = {}
        self._overrides: dict[str, Scenario] = {}

    # ------------------------------------------------------------------ 场景
    def scenario_names(self) -> list[str]:
        return available_scenarios(directory=self.fixtures_dir)

    def load(self, name: str) -> Scenario:
        return load_scenario(name, directory=self.fixtures_dir)

    def scenario_for(self, thread_id: str) -> Scenario:
        """按线程设置挑场景；未指定时用默认场景，再退回第一个可用 fixture。"""
        override = self._overrides.get(thread_id)
        if override is not None:
            return override
        try:
            settings = self.repo.state(thread_id).thread.settings or {}
        except ThreadNotFound:
            settings = {}
        name = str(settings.get(SCENARIO_SETTING) or self.default_scenario or "")
        if name:
            return self.load(name)
        names = self.scenario_names()
        if PREFERRED_DEFAULT_SCENARIO in names:
            return self.load(PREFERRED_DEFAULT_SCENARIO)
        if not names:
            return Scenario(name="empty")
        return self.load(names[0])

    def bind_scenario(self, thread_id: str, scenario: Scenario | str) -> Scenario:
        """给线程绑定场景（测试与离线演示的入口）。

        绑定会**丢弃已装配的运行时**，下次 :meth:`runtime_for` 用新场景重建——
        否则场景换了、执行内核手里还是旧 provider，行为会不一致。
        """
        resolved = self.load(scenario) if isinstance(scenario, str) else scenario
        self._overrides[thread_id] = resolved
        self._runtimes.pop(thread_id, None)
        return resolved

    # ------------------------------------------------------------------ 装配
    def runtime_for(self, thread_id: str) -> ThreadRuntime:
        if self._closed:
            raise RuntimeUnavailable("runtime host is closed")
        existing = self._runtimes.get(thread_id)
        if existing is not None:
            return existing
        if not self.repo.exists(thread_id):
            raise ThreadNotFound(f"thread {thread_id} not found")
        scenario = self.scenario_for(thread_id)
        provider = scenario.provider()
        executor = ScenarioToolExecutor(scenario=scenario, host=self, clock=self.clock)
        gateway = ModelGateway(provider)
        scheduler = ToolScheduler(executor, clock=self.clock)
        runtime = ThreadRuntime(
            thread_id=thread_id,
            scenario=scenario,
            executor=executor,
            gateway=gateway,
            scheduler=scheduler,
            runtime=TurnRuntime(
                repo=self.repo,
                gateway=gateway,
                tools=scheduler,
                compaction=CompactionService(
                    repo=self.repo, gateway=gateway, token_budget=self.token_budget
                ),
                clock=self.clock,
                system_prompt=self.system_prompt,
                max_iterations=self.max_iterations,
                retry=RetryPolicy(max_attempts=3),
                approval_gate=self.approval_gate,
            ),
        )
        self._runtimes[thread_id] = runtime
        return runtime

    # ------------------------------------------------------------------ 审批门
    def approval_gate(self, call: ToolCall) -> str:
        """审批门：先看已生效授权（一次 / 本对话 / 完全访问），再看是否已被拒绝。

        已被拒绝的调用**放行到执行器**，由执行器返回 ``approval_denied`` 错误结果回喂模型——
        这正是「拒绝结果必须回喂模型」的落地方式：如果这里再次要求审批，就会变成死循环。
        """
        if self.is_granted(call):
            return ALLOW
        if self.denial_for(call) is not None:
            return ALLOW
        try:
            scenario = self.scenario_for(call.thread_id)
        except ThreadNotFound:
            return ALLOW
        if call.name in scenario.approval_required:
            return REQUIRE
        return ALLOW

    def denial_for(self, call: ToolCall) -> dict[str, Any] | None:
        """该调用是否已被研究者拒绝（按 ``call_id`` 精确匹配）。"""
        try:
            state = self.repo.state(call.thread_id)
        except ThreadNotFound:
            return None
        for approval in state.approvals.values():
            if approval.call_id == call.call_id and approval.status is ApprovalStatus.DENIED:
                return {
                    "code": "approval_denied",
                    "message": "研究者拒绝了该操作",
                    "approval_id": approval.approval_id,
                }
        return None

    def is_granted(self, call: ToolCall) -> bool:
        """判断该调用是否已被授权（一次批准按 call_id 命中，会话规则按工具名前缀命中）。"""
        try:
            state = self.repo.state(call.thread_id)
        except ThreadNotFound:
            return False
        for approval in state.approvals.values():
            if approval.call_id == call.call_id and approval.status is ApprovalStatus.GRANTED:
                return True
        settings = state.thread.settings or {}
        if settings.get(FULL_ACCESS_KEY):
            return True
        rules = settings.get(PERMISSION_RULES_KEY) or []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if rule.get("tool") != call.name:
                continue
            if prefix_matches(list(rule.get("prefix") or []), call.arguments):
                return True
        return False

    # ------------------------------------------------------------------ 驱动
    def start(self, thread_id: str, turn_id: str, *, register_cancel: bool = True) -> asyncio.Task[Any]:
        """把 Turn 交给执行内核，返回后台任务。"""
        runtime = self.runtime_for(thread_id)
        if runtime.is_running():
            raise RuntimeUnavailable(f"thread {thread_id} already has a running turn task")
        from contracts.agent_v2.cancellation import CancelToken

        token = CancelToken()
        runtime.token = token
        if register_cancel:
            self.tree.register_cancel(thread_id, token)

        async def _drive() -> Any:
            try:
                runtime.outcome = await runtime.runtime.run(thread_id, turn_id, cancel=token)
                return runtime.outcome
            except asyncio.CancelledError:
                self.repo.release_turn(thread_id, reason="task-cancelled")
                raise
            finally:
                if register_cancel:
                    self.tree.unregister_cancel(thread_id)

        runtime.task = asyncio.ensure_future(_drive())
        return runtime.task

    def interrupt(self, thread_id: str, reason: str = "interrupted") -> bool:
        """向正在运行的 Turn 发取消信号（领域状态由调用方负责落事件）。"""
        runtime = self._runtimes.get(thread_id)
        if runtime is None or not runtime.is_running():
            return False
        if runtime.token is not None:
            runtime.token.cancel(reason)
        return True

    def is_running(self, thread_id: str) -> bool:
        runtime = self._runtimes.get(thread_id)
        return bool(runtime and runtime.is_running())

    async def await_idle(self, thread_id: str, *, timeout_s: float = 10.0) -> Any:
        """等待线程的运行时任务收敛（测试与 HTTP 同步路径使用）。"""
        runtime = self._runtimes.get(thread_id)
        if runtime is None or runtime.task is None:
            return None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(runtime.task), timeout=timeout_s)
        return runtime.outcome

    def runtime_info(self, thread_id: str) -> dict[str, Any]:
        runtime = self._runtimes.get(thread_id)
        if runtime is None:
            return {"thread_id": thread_id, "running": False, "scenario": None}
        return runtime.to_dict()

    # ------------------------------------------------------------------ 子 Agent
    def spawn_child(self, call: ToolCall) -> dict[str, Any]:
        """``spawn_agent`` 的假实现：建子线程、跑子 Turn、按 ``outcome`` 收敛并回传结果。

        子 Agent 是**独立 Thread + 独立 Turn**（计划书 §7），因此这里真的在子线程里
        ``start_turn`` 并把子 Turn 走到对应终态；父线程只拿到一条结构化的
        ``subagent_result`` Item 与 ``agent/child_*`` 事件。真实实现（Agent 2 的工具入口
        接 Agent 1 的运行时）同形，只是子 Turn 由真实的模型循环驱动。
        """
        arguments = dict(call.arguments or {})
        name = str(arguments.get("name") or "子 Agent")
        task_text = str(arguments.get("task") or "")
        outcome = str(arguments.get("outcome") or "completed")
        summary = str(arguments.get("summary") or f"{name} 已完成")
        error = {
            "code": "child_failed",
            "message": str(arguments.get("error") or "子任务未完成"),
        }

        parent = call.thread_id
        child = self.tree.create_child(parent, name, model=arguments.get("model"))
        self._spawned[call.call_id] = child.thread_id
        self.tree.send_message(parent, child.thread_id, task_text or f"请完成：{name}", kind="task")

        child_turn = self.repo.start_turn(child.thread_id, inputs=[{"text": task_text or name}])
        if outcome != "interrupted":
            self.repo.add_item(
                child.thread_id,
                ItemType.ASSISTANT_TEXT,
                {"text": summary},
                turn_id=child_turn.turn_id,
            )

        if outcome == "running":
            return {
                "child_thread_id": child.thread_id,
                "status": "running",
                "turn_id": child_turn.turn_id,
                "summary": None,
            }
        if outcome == "interrupted":
            self.tree.interrupt_child(child.thread_id, reason="parent_request")
            self.tree.report_result(
                child.thread_id, summary=summary, status="interrupted", error=error
            )
            return {"child_thread_id": child.thread_id, "status": "interrupted", "summary": summary}
        if outcome == "failed":
            self.repo.fail_turn(child.thread_id, child_turn.turn_id, error=error)
            self.tree.report_result(
                child.thread_id, summary=summary, status="failed", error=error
            )
            return {"child_thread_id": child.thread_id, "status": "failed", "summary": summary}
        self.repo.complete_turn(child.thread_id, child_turn.turn_id, stop_reason="end_turn")
        self.tree.report_result(child.thread_id, summary=summary, status="completed")
        return {"child_thread_id": child.thread_id, "status": "completed", "summary": summary}

    def spawned_child(self, call_id: str) -> str | None:
        return self._spawned.get(call_id)

    # ------------------------------------------------------------------ 工具增量 / 计划
    def emit_tool_output(self, call: ToolCall, chunk: str) -> None:
        """工具增量输出（``tool/output`` 事件）：前端据此做流式展示。"""
        self.repo.emit_event(
            call.thread_id,
            EventType.TOOL_OUTPUT,
            payload={"name": call.name, "chunk": chunk},
            turn_id=call.turn_id,
            call_id=call.call_id,
        )

    def record_plan(self, call: ToolCall) -> dict[str, Any]:
        """把模型给出的计划落成一个 PLAN Item（计划不阻塞执行，只是可审计记录）。"""
        arguments = dict(call.arguments or {})
        raw_steps = arguments.get("steps") or arguments.get("plan") or []
        if not isinstance(raw_steps, list):
            raise ValueError("update_plan: steps must be an array")
        steps: list[dict[str, Any]] = []
        for index, step in enumerate(raw_steps, start=1):
            if isinstance(step, dict):
                steps.append(
                    {
                        "index": index,
                        "title": str(step.get("title") or step.get("text") or f"步骤 {index}"),
                        "status": str(step.get("status") or "pending"),
                    }
                )
            else:
                steps.append({"index": index, "title": str(step), "status": "pending"})
        item = self.repo.add_item(
            call.thread_id,
            ItemType.PLAN,
            {
                "title": str(arguments.get("title") or "任务计划"),
                "steps": steps,
                "note": arguments.get("note"),
            },
            turn_id=call.turn_id,
            call_id=call.call_id,
        )
        return {"plan_item_id": item.item_id, "steps": len(steps)}

    # ------------------------------------------------------------------ 关闭
    async def shutdown(self) -> None:
        """取消所有运行中的任务并标记不可用（服务关闭路径）。"""
        self._closed = True
        tasks = [rt.task for rt in self._runtimes.values() if rt.task is not None and not rt.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @property
    def closed(self) -> bool:
        return self._closed

    def describe(self) -> dict[str, Any]:
        return {
            "host": "fake",
            "closed": self._closed,
            "scenarios": self.scenario_names(),
            "threads": {tid: rt.to_dict() for tid, rt in sorted(self._runtimes.items())},
        }


__all__ = [
    "SCENARIO_SETTING",
    "PERMISSION_RULES_KEY",
    "FULL_ACCESS_KEY",
    "PREFERRED_DEFAULT_SCENARIO",
    "SPAWN_TOOL_NAME",
    "SPAWN_TOOL_SPEC",
    "PLAN_TOOL_NAME",
    "PLAN_TOOL_SPEC",
    "RuntimeUnavailable",
    "ScenarioToolExecutor",
    "ThreadRuntime",
    "FakeRuntimeHost",
    "normalize_command_prefix",
    "prefix_matches",
]
