"""工具调度：**只读并行 / 副作用串行**，并做重复调用保护（Agent 1 / WP-04）。

Agent 1 不实现任何具体工具：执行能力由 :class:`contracts.agent_v2.ToolExecutor`
注入。本模块只负责计划书 §4.3 要求的调度语义与可观测证据：

- ``kind=read_only`` 的调用并发执行；
- ``kind=side_effect`` 的调用**串行**执行（并发峰值必须为 1）；
- 同一个 ``call_id`` 只执行一次，重复出现直接复用已有结果
  （避免模型重复请求导致副作用被执行两次）；
- 未知工具名 → ``invalid_arguments``，不调用执行器，错误回喂模型；
- 超时 → ``timeout``；执行器抛错 → ``failed``；取消 → ``cancelled``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from contracts.agent_v2.cancellation import CancelledError, CancelToken
from contracts.agent_v2.clock import Clock, SystemClock
from contracts.agent_v2.enums import ToolCallStatus, ToolKind
from contracts.agent_v2.fake import ToolExecutor
from contracts.agent_v2.models import ToolCall, ToolResult, ToolSpec

StartHook = Callable[[ToolCall], None]
ResultHook = Callable[[ToolCall, ToolResult, bool], None]


@dataclass
class DispatchReport:
    """一轮工具执行的证据。"""

    results: list[ToolResult] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    max_concurrency_read_only: int = 0
    max_concurrency_side_effect: int = 0

    def by_call_id(self) -> dict[str, ToolResult]:
        return {r.call_id: r for r in self.results}

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.results),
            "duplicates": list(self.duplicates),
            "max_concurrency_read_only": self.max_concurrency_read_only,
            "max_concurrency_side_effect": self.max_concurrency_side_effect,
        }


class ToolScheduler:
    """工具调度器。"""

    def __init__(self, executor: ToolExecutor, *, clock: Clock | None = None) -> None:
        self.executor = executor
        self.clock = clock or SystemClock()
        self._active_read_only = 0
        self._active_side_effect = 0
        self.report = DispatchReport()

    # ------------------------------------------------------------------ 查询
    def spec_for(self, name: str) -> ToolSpec | None:
        spec = self.executor.spec(name)
        return spec

    def kind_of(self, name: str) -> ToolKind:
        spec = self.spec_for(name)
        if spec is None:
            # 未知工具按副作用处理：宁可串行也不要猜它可以并行
            return ToolKind.SIDE_EFFECT
        return spec.kind

    def specs(self) -> Sequence[ToolSpec]:
        return self.executor.specs()

    # ------------------------------------------------------------------ 执行
    async def execute_all(
        self,
        calls: Sequence[ToolCall],
        *,
        cancel: CancelToken | None = None,
        on_start: StartHook | None = None,
        on_result: ResultHook | None = None,
        cache: dict[str, ToolResult] | None = None,
    ) -> DispatchReport:
        """执行一批调用，返回证据报告。

        :param cache: ``call_id -> ToolResult``，跨轮复用以实现重复调用保护；
            传入的字典会被就地更新。
        """
        store: dict[str, ToolResult] = cache if cache is not None else {}
        report = DispatchReport()
        by_id: dict[str, ToolResult] = {}
        lock = asyncio.Lock()

        async def run(call: ToolCall) -> ToolResult:
            kind = self.kind_of(call.name)
            cached = store.get(call.call_id)
            if cached is not None:
                async with lock:
                    report.duplicates.append(call.call_id)
                if on_result is not None:
                    on_result(call, cached, True)
                return cached

            if on_start is not None:
                on_start(call)

            if kind is ToolKind.READ_ONLY:
                self._active_read_only += 1
                async with lock:
                    report.max_concurrency_read_only = max(
                        report.max_concurrency_read_only, self._active_read_only
                    )
            else:
                self._active_side_effect += 1
                async with lock:
                    report.max_concurrency_side_effect = max(
                        report.max_concurrency_side_effect, self._active_side_effect
                    )

            try:
                result = await self._execute_one(call, kind, cancel)
            finally:
                if kind is ToolKind.READ_ONLY:
                    self._active_read_only -= 1
                else:
                    self._active_side_effect -= 1

            store[call.call_id] = result
            if on_result is not None:
                on_result(call, result, False)
            return result

        parallel = [c for c in calls if self.kind_of(c.name) is ToolKind.READ_ONLY]
        serial = [c for c in calls if self.kind_of(c.name) is not ToolKind.READ_ONLY]

        gathered: list[ToolResult] = []
        if parallel:
            gathered.extend(
                await asyncio.gather(*(run(c) for c in parallel), return_exceptions=False)
            )
        for call in serial:
            gathered.extend([await run(call)])

        for call in calls:
            by_id[call.call_id] = store[call.call_id]
        report.results = [by_id[c.call_id] for c in calls]
        self.report = report
        return report

    async def _execute_one(
        self, call: ToolCall, kind: ToolKind, cancel: CancelToken | None
    ) -> ToolResult:
        spec = self.spec_for(call.name)
        started = self.clock.now_iso()
        if spec is None:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.INVALID_ARGUMENTS,
                error={
                    "code": "unknown_tool",
                    "message": f"tool {call.name!r} is not registered",
                },
                started_at=started,
                finished_at=self.clock.now_iso(),
                duration_ms=0,
            )
        try:
            if spec.timeout_s is not None:
                result = await asyncio.wait_for(
                    self.executor.execute(call, cancel), timeout=float(spec.timeout_s)
                )
            else:
                result = await self.executor.execute(call, cancel)
        except TimeoutError:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.TIMEOUT,
                error={
                    "code": "timeout",
                    "message": f"tool {call.name!r} exceeded {spec.timeout_s}s",
                },
                started_at=started,
                finished_at=self.clock.now_iso(),
            )
        except asyncio.CancelledError:
            # 异步取消必须原样传播（协作式取消走下面的 CancelledError 分支）
            raise
        except CancelledError:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.CANCELLED,
                error={"code": "cancelled", "message": "cancelled during execution"},
                started_at=started,
                finished_at=self.clock.now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 - 执行器崩溃必须回喂模型而不是中断循环
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                status=ToolCallStatus.FAILED,
                error={
                    "code": "executor_error",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                started_at=started,
                finished_at=self.clock.now_iso(),
            )
        if result.started_at is None:
            result = ToolResult(
                call_id=result.call_id,
                name=result.name,
                status=result.status,
                output=result.output,
                error=result.error,
                duration_ms=result.duration_ms,
                started_at=started,
                finished_at=self.clock.now_iso(),
            )
        return result


__all__ = ["ToolScheduler", "DispatchReport"]
