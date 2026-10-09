"""TurnRuntime：不依赖 Web 请求的执行循环（Agent 1 / WP-03）。

计划书 §4.3 的九步在这里逐条落地：

1. 接收输入并追加事件（由 :meth:`ThreadRepository.start_turn` / ``append_input`` 完成）；
2. 生成模型请求上下文（:mod:`services.agent_runtime_v2.context`）；
3. 消费统一模型 Item 流（:class:`ModelGateway`）；
4. 把文本与工具调用追加为事件；
5. 把工具调用交给注入的 :class:`ToolExecutor`；
6. 等待工具结果、审批或用户输入（审批/输入会**挂起** Turn，不阻塞进程）；
7. 按 ``call_id`` 回写结果并继续模型循环；
8. 处理重试、压缩、中断与完成；
9. **每个状态变化先持久化事件，再更新内存状态**（由仓库保证）。

硬约束：
- 本文件**不得出现任何供应商专有分支**（有静态测试守卫）；
- 取消 / 超时 / 失败路径必须释放 Turn 租约，且**不得**产生 ``turn/completed``。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from contracts.agent_v2.cancellation import CancelledError, CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import (
    TERMINAL_TURN_STATUSES,
    ErrorClass,
    EventType,
    ItemType,
    ModelStreamKind,
    StopReason,
    ToolCallStatus,
    ToolKind,
    TurnStatus,
)
from contracts.agent_v2.errors import ModelStreamError, TurnNotFound
from contracts.agent_v2.models import (
    ModelRequest,
    ToolCall,
    ToolResult,
    Turn,
)
from services.model_gateway_v2 import ModelGateway, RetryPolicy

from .context import build_context
from .tools import ToolScheduler

#: 审批门返回值：allow 直接执行 / require 挂起等待研究者决定。
ApprovalDecision = str
ALLOW: ApprovalDecision = "allow"
REQUIRE: ApprovalDecision = "require"

ApprovalGate = Callable[[ToolCall], ApprovalDecision]


@dataclass
class TurnOutcome:
    """一次 ``run`` 的结果（不抛异常地表达终态）。"""

    turn_id: str
    status: TurnStatus
    stop_reason: str | None = None
    iterations: int = 0
    tool_results: list[ToolResult] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    retries: int = 0
    compactions: int = 0
    suspended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "status": self.status.value,
            "stop_reason": self.stop_reason,
            "iterations": self.iterations,
            "tool_results": len(self.tool_results),
            "usage": dict(self.usage),
            "error": self.error,
            "retries": self.retries,
            "compactions": self.compactions,
            "suspended": self.suspended,
        }


class TurnRuntime:
    """执行循环。构造后可直接 ``asyncio.run(runtime.run(thread_id, turn_id))``。"""

    def __init__(
        self,
        *,
        repo: Any,
        gateway: ModelGateway,
        tools: ToolScheduler,
        compaction: Any | None = None,
        clock: Clock | None = None,
        system_prompt: str | None = None,
        max_iterations: int = 8,
        retry: RetryPolicy | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> None:
        self.repo = repo
        self.gateway = gateway
        self.tools = tools
        self.compaction = compaction
        self.clock = clock or SystemClock()
        self.system_prompt = system_prompt
        self.max_iterations = max_iterations
        self.retry = retry or RetryPolicy()
        self.approval_gate = approval_gate

    # ------------------------------------------------------------------ 入口
    async def run(
        self,
        thread_id: str,
        turn_id: str,
        *,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
    ) -> TurnOutcome:
        """执行一个 Turn，直到完成 / 失败 / 中断 / 挂起等待。"""
        token = cancel or CancelToken()
        try:
            if timeout_s is not None:
                return await asyncio.wait_for(
                    self._run(token, thread_id, turn_id), timeout=float(timeout_s)
                )
            return await self._run(token, thread_id, turn_id)
        except TimeoutError:
            return self._interrupt(thread_id, turn_id, "runtime timeout")
        except CancelledError as exc:
            return self._interrupt(thread_id, turn_id, f"cancelled: {exc}")
        except asyncio.CancelledError:
            # 外部取消本任务：先把 Turn 收尾（中断 + 释放租约），再尊重 asyncio 语义向外抛
            self._interrupt(thread_id, turn_id, "task cancelled")
            raise
        except ModelStreamError as exc:
            return self._fail(
                thread_id,
                turn_id,
                {
                    "code": "model_stream_error",
                    "error_class": str(ErrorClass(exc.error_class))
                    if _is_error_class(exc.error_class)
                    else "fatal",
                    "message": str(exc),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 未预期异常必须落成失败事件，不能吞
            return self._fail(
                thread_id,
                turn_id,
                {
                    "code": "runtime_error",
                    "error_class": ErrorClass.FATAL.value,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            )

    # ------------------------------------------------------------------ 主循环
    async def _run(self, token: CancelToken, thread_id: str, turn_id: str) -> TurnOutcome:
        outcome = TurnOutcome(turn_id=turn_id, status=TurnStatus.RUNNING)
        tool_cache: dict[str, ToolResult] = {}
        compacted_once = False

        for iteration in range(1, self.max_iterations + 1):
            token.raise_if_cancelled()
            outcome.iterations = iteration
            state = self.repo.state(thread_id)
            turn = _turn_or_raise(state, turn_id)

            # 外部中断（研究者点「停止」/ 父 Agent 取消）会先把 Turn 置为 interrupted。
            # 此时必须识别出来并返回中断结果，而不是继续跑完再撞上
            # 「interrupted -> completed 非法」被兜成 failed。
            if turn.status is TurnStatus.INTERRUPTED:
                self.repo.release_turn(thread_id, reason=turn.cancel_reason or "interrupted")
                return TurnOutcome(
                    turn_id=turn_id,
                    status=TurnStatus.INTERRUPTED,
                    error={
                        "code": "interrupted",
                        "message": turn.cancel_reason or "turn interrupted externally",
                    },
                )

            # 6+7：先把挂起（已请求但未执行）的工具跑完，再决定是否请求模型。
            # 这条路径同时承担「审批通过后继续」与「工具结果回写后继续」。
            pending = self._pending_calls(state, turn_id, tool_cache)
            if pending:
                suspended = await self._execute_batch(
                    thread_id, turn_id, pending, token, tool_cache, outcome
                )
                if suspended is not None:
                    return suspended
                continue

            # 2：装配上下文
            messages = build_context(state, system_prompt=self.system_prompt)
            request = ModelRequest(
                request_id=f"{turn_id}#{iteration}",
                messages=messages,
                model=state.thread.model,
                tools=[spec.to_dict() for spec in self.tools.specs()],
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self.repo.emit_event(
                thread_id,
                EventType.MODEL_REQUESTED,
                payload={
                    "iteration": iteration,
                    "message_count": len(messages),
                    "request_id": request.request_id,
                },
                turn_id=turn_id,
            )

            # 3+4+8：消费流，含重试与上下文超限触发的压缩
            attempt = 0
            sink: dict[str, Any] = {"text": "", "reasoning": "", "calls": [], "usage": {}}
            while True:
                attempt += 1
                sink.update({"text": "", "reasoning": "", "calls": [], "usage": {}})
                try:
                    stop_reason = await self._consume_stream(
                        token, thread_id, turn_id, request, sink
                    )
                    break
                except ModelStreamError as exc:
                    error_class = _as_error_class(exc.error_class)
                    produced_output = bool(sink["text"]) or bool(sink["calls"])
                    if (
                        error_class is ErrorClass.RETRYABLE
                        and attempt <= self.retry.max_attempts
                        and not produced_output
                    ):
                        delay = self.retry.delay_for(attempt)
                        self.repo.emit_event(
                            thread_id,
                            EventType.MODEL_RETRY_SCHEDULED,
                            payload={
                                "attempt": attempt,
                                "error_class": error_class.value,
                                "delay_s": delay,
                                "message": str(exc),
                            },
                            turn_id=turn_id,
                        )
                        outcome.retries += 1
                        await self.clock.sleep(delay)
                        continue
                    if (
                        error_class is ErrorClass.CONTEXT_OVERFLOW
                        and self.compaction is not None
                        and not compacted_once
                    ):
                        compacted_once = True
                        self.repo.emit_event(
                            thread_id,
                            EventType.MODEL_FAILED,
                            payload={"error_class": error_class.value, "message": str(exc)},
                            turn_id=turn_id,
                        )
                        await self.compaction.compact(
                            thread_id, trigger="auto", cancel=token, turn_id=turn_id
                        )
                        outcome.compactions += 1
                        # 压缩成功后必须**重新发起**模型请求（用压缩后的上下文），
                        # 而不是直接结束 Turn；compacted_once 保证最多压缩一轮。
                        continue
                    raise

            if sink["usage"]:
                outcome.usage = dict(sink["usage"])

            # 4：文本与推理落成 Item（工具调用 Item 由批量执行阶段生成）
            if sink["reasoning"]:
                self.repo.add_item(
                    thread_id,
                    ItemType.REASONING,
                    {"text": sink["reasoning"]},
                    turn_id=turn_id,
                    persist_cache=False,
                )
            if sink["text"]:
                self.repo.add_item(
                    thread_id,
                    ItemType.ASSISTANT_TEXT,
                    {"text": sink["text"]},
                    turn_id=turn_id,
                    persist_cache=False,
                )

            # 5+6：有工具调用则执行并继续循环；否则收尾
            if sink["calls"]:
                batch = self._materialize_calls(thread_id, turn_id, sink["calls"])
                suspended = await self._execute_batch(
                    thread_id, turn_id, batch, token, tool_cache, outcome
                )
                if suspended is not None:
                    return suspended
                continue

            # 收尾前再确认一次：工具执行期间可能被外部中断
            latest = self.repo.state(thread_id).turns.get(turn_id)
            if latest is not None and latest.status is TurnStatus.INTERRUPTED:
                self.repo.release_turn(thread_id, reason="interrupted")
                return TurnOutcome(
                    turn_id=turn_id,
                    status=TurnStatus.INTERRUPTED,
                    error={"code": "interrupted", "message": "turn interrupted externally"},
                )

            self.repo.complete_turn(thread_id, turn_id, stop_reason=stop_reason)
            outcome.status = TurnStatus.COMPLETED
            outcome.stop_reason = stop_reason
            return outcome

        # 超出迭代上限：必须显式失败，不能留下活动 Turn
        return self._fail(
            thread_id,
            turn_id,
            {
                "code": "max_iterations",
                "error_class": ErrorClass.FATAL.value,
                "message": f"turn exceeded max_iterations={self.max_iterations}",
            },
        )

    # ------------------------------------------------------------------ 流消费
    async def _consume_stream(
        self,
        token: CancelToken,
        thread_id: str,
        turn_id: str,
        request: ModelRequest,
        sink: dict[str, Any],
    ) -> str:
        """消费一次模型流，返回 stop_reason；错误以 :class:`ModelStreamError` 抛出。"""
        stop_reason = StopReason.END_TURN.value
        # 用 aclosing 保证提前退出（重试/中断/错误）时流被正确关闭，
        # 避免异步生成器悬空、测试进程结束时留下未回收任务。
        async with contextlib.aclosing(self.gateway.stream(request, token)) as stream:
            async for item in stream:
                token.raise_if_cancelled()
                kind = getattr(item, "kind", None)

                if kind == ModelStreamKind.TEXT_DELTA.value:
                    sink["text"] += item.text
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_DELTA,
                        payload={"text": item.text},
                        turn_id=turn_id,
                    )
                elif kind == ModelStreamKind.REASONING_DELTA.value:
                    sink["reasoning"] += item.text
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_REASONING_DELTA,
                        payload={"text": item.text},
                        turn_id=turn_id,
                    )
                elif kind == ModelStreamKind.TOOL_CALL_DELTA.value:
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_TOOL_CALL_DELTA,
                        payload={
                            "call_id": item.call_id,
                            "name": item.name,
                            "arguments_delta": item.arguments_delta,
                        },
                        turn_id=turn_id,
                        call_id=item.call_id,
                    )
                elif kind == ModelStreamKind.TOOL_CALL_COMPLETED.value:
                    sink["calls"].append(item)
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_TOOL_CALL_COMPLETED,
                        payload={
                            "call_id": item.call_id,
                            "name": item.name,
                            "arguments": item.arguments,
                        },
                        turn_id=turn_id,
                        call_id=item.call_id,
                    )
                elif kind == ModelStreamKind.USAGE.value:
                    sink["usage"] = {
                        "input_tokens": item.input_tokens,
                        "output_tokens": item.output_tokens,
                        "cached_tokens": item.cached_tokens,
                    }
                    if getattr(item, "cost_usd", None) is not None:
                        sink["usage"]["cost_usd"] = item.cost_usd
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_USAGE,
                        payload=dict(sink["usage"]),
                        turn_id=turn_id,
                    )
                elif kind == ModelStreamKind.COMPLETED.value:
                    stop_reason = (
                        item.stop_reason.value
                        if hasattr(item.stop_reason, "value")
                        else str(item.stop_reason)
                    )
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_COMPLETED,
                        payload={"stop_reason": stop_reason, "usage": dict(sink["usage"])},
                        turn_id=turn_id,
                    )
                elif kind == ModelStreamKind.ERROR.value:
                    error_class = getattr(item, "error_class", ErrorClass.FATAL)
                    message = getattr(item, "message", "model error")
                    self.repo.emit_event(
                        thread_id,
                        EventType.MODEL_FAILED,
                        payload={"error_class": str(error_class), "message": message},
                        turn_id=turn_id,
                    )
                    raise ModelStreamError(
                        str(error_class),
                        message,
                        retry_after_s=float(getattr(item, "retry_after_s", 0.0)),
                    )
        return stop_reason

    # ------------------------------------------------------------------ 工具
    def _materialize_calls(
        self, thread_id: str, turn_id: str, stream_calls: list[Any]
    ) -> list[ToolCall]:
        """把模型流里的工具调用落成 ToolCall 记录（状态 requested）。"""
        out: list[ToolCall] = []
        for item in stream_calls:
            spec = self.tools.spec_for(item.name)
            kind = spec.kind if spec is not None else ToolKind.SIDE_EFFECT
            call = ToolCall(
                call_id=item.call_id,
                name=item.name,
                kind=kind,
                status=ToolCallStatus.REQUESTED,
                thread_id=thread_id,
                turn_id=turn_id,
                arguments=dict(item.arguments or {}),
                requested_at=self.clock.now_iso(),
            )
            self.repo.upsert_tool_call(
                thread_id, call, event_type=EventType.MODEL_TOOL_CALL_STARTED
            )
            self.repo.add_item(
                thread_id,
                ItemType.TOOL_CALL,
                {
                    "name": call.name,
                    "arguments": call.arguments,
                    "kind": kind.value,
                    "status": ToolCallStatus.REQUESTED.value,
                },
                turn_id=turn_id,
                call_id=call.call_id,
                persist_cache=False,
            )
            out.append(call)
        return out

    def _pending_calls(
        self, state: Any, turn_id: str, cache: dict[str, ToolResult]
    ) -> list[ToolCall]:
        """已请求但尚未执行完成的工具调用（审批通过后继续、断点续跑都走这条路径）。"""
        out: list[ToolCall] = []
        for call in state.tool_calls.values():
            if call.turn_id != turn_id:
                continue
            if call.status not in (ToolCallStatus.REQUESTED, ToolCallStatus.RUNNING):
                continue
            if call.call_id in cache:
                continue
            out.append(call)
        out.sort(key=lambda c: c.requested_at or "")
        return out

    async def _execute_batch(
        self,
        thread_id: str,
        turn_id: str,
        calls: list[ToolCall],
        token: CancelToken,
        cache: dict[str, ToolResult],
        outcome: TurnOutcome,
    ) -> TurnOutcome | None:
        """执行一批工具调用；返回非 None 表示 Turn 已挂起等待审批。"""
        if not calls:
            return None

        # 审批预检：任何需要审批的调用都会让整批挂起，避免半批副作用
        if self.approval_gate is not None:
            for call in calls:
                if call.call_id in cache:
                    continue
                if self.approval_gate(call) == REQUIRE:
                    spec = self.tools.spec_for(call.name)
                    self.repo.wait_for_approval(
                        thread_id,
                        turn_id,
                        action={
                            "tool": call.name,
                            "arguments": call.arguments,
                            "kind": call.kind.value,
                            "risk": (spec.description if spec else "") or "unknown",
                        },
                        risk="unknown",
                        call_id=call.call_id,
                    )
                    outcome.status = TurnStatus.WAITING_APPROVAL
                    outcome.suspended = True
                    return outcome

        def on_start(call: ToolCall) -> None:
            running = _with_call_status(call, ToolCallStatus.RUNNING, clock=self.clock)
            self.repo.upsert_tool_call(
                thread_id, running, event_type=EventType.TOOL_STARTED
            )

        def on_result(call: ToolCall, result: ToolResult, duplicate: bool) -> None:
            finished = _call_from_result(call, result, self.clock)
            self.repo.upsert_tool_call(
                thread_id, finished, event_type=_tool_event_for(result.status)
            )
            self.repo.add_item(
                thread_id,
                ItemType.TOOL_RESULT,
                {
                    "status": result.status.value,
                    "output": result.output,
                    "error": result.error,
                    "duplicate": duplicate,
                },
                turn_id=turn_id,
                call_id=call.call_id,
                persist_cache=False,
            )

        report = await self.tools.execute_all(
            calls, cancel=token, on_start=on_start, on_result=on_result, cache=cache
        )
        outcome.tool_results.extend(report.results)
        return None

    # ------------------------------------------------------------------ 收尾
    def _interrupt(self, thread_id: str, turn_id: str, reason: str) -> TurnOutcome:
        """中断收尾：落中断事件 + 释放租约，**绝不**写成功事件。"""
        try:
            current = self.repo.state(thread_id).turns.get(turn_id)
        except Exception:  # noqa: BLE001 - 收尾路径必须尽力而为
            current = None
        if current is not None and current.status not in TERMINAL_TURN_STATUSES:
            if current.status is TurnStatus.INTERRUPTED:
                self.repo.release_turn(thread_id, reason=reason)
            else:
                self.repo.interrupt_turn(thread_id, turn_id, reason=reason)
        else:
            self.repo.release_turn(thread_id, reason=reason)
        return TurnOutcome(
            turn_id=turn_id,
            status=TurnStatus.INTERRUPTED,
            error={"code": "interrupted", "message": reason},
        )

    def _fail(self, thread_id: str, turn_id: str, error: dict[str, Any]) -> TurnOutcome:
        try:
            current = self.repo.state(thread_id).turns.get(turn_id)
        except Exception:  # noqa: BLE001
            current = None
        if current is not None and current.status not in TERMINAL_TURN_STATUSES:
            self.repo.fail_turn(thread_id, turn_id, error=error)
        else:
            self.repo.release_turn(thread_id, reason="failed")
        return TurnOutcome(turn_id=turn_id, status=TurnStatus.FAILED, error=error)


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------
def _is_error_class(value: Any) -> bool:
    try:
        ErrorClass(value)
        return True
    except ValueError:
        return False


def _as_error_class(value: Any) -> ErrorClass:
    try:
        return ErrorClass(value)
    except ValueError:
        return ErrorClass.FATAL


def _turn_or_raise(state: Any, turn_id: str) -> Turn:
    try:
        return state.turn(turn_id)
    except Exception as exc:  # noqa: BLE001
        raise TurnNotFound(f"turn {turn_id} not found") from exc


def _with_call_status(call: ToolCall, status: ToolCallStatus, *, clock: Clock) -> ToolCall:
    data = call.to_dict()
    data["status"] = status.value
    if status is ToolCallStatus.RUNNING:
        data["started_at"] = data.get("started_at") or clock.now_iso()
    if status in {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.TIMEOUT,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.INVALID_ARGUMENTS,
    }:
        data["finished_at"] = clock.now_iso()
    return ToolCall.from_dict(data)


def _call_from_result(call: ToolCall, result: ToolResult, clock: Clock) -> ToolCall:
    data = call.to_dict()
    data["status"] = result.status.value
    data["output"] = result.output
    data["error"] = result.error
    data["duration_ms"] = result.duration_ms
    data["started_at"] = result.started_at or data.get("started_at") or clock.now_iso()
    data["finished_at"] = result.finished_at or clock.now_iso()
    if result.status is ToolCallStatus.SUCCEEDED:
        data["error"] = None
    return ToolCall.from_dict(data)


def _tool_event_for(status: ToolCallStatus) -> EventType:
    return {
        ToolCallStatus.SUCCEEDED: EventType.TOOL_COMPLETED,
        ToolCallStatus.FAILED: EventType.TOOL_FAILED,
        ToolCallStatus.TIMEOUT: EventType.TOOL_TIMEOUT,
        ToolCallStatus.CANCELLED: EventType.TOOL_CANCELLED,
        ToolCallStatus.INVALID_ARGUMENTS: EventType.TOOL_INVALID_ARGUMENTS,
    }.get(status, EventType.TOOL_OUTPUT)


__all__ = [
    "TurnRuntime",
    "TurnOutcome",
    "ApprovalDecision",
    "ApprovalGate",
    "ALLOW",
    "REQUIRE",
]
