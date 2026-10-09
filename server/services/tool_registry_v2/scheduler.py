# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License")
"""调度策略：只读并行、副作用串行（Agent 2 / WP-01）。

契约层（Agent 1 的 ``ToolScheduler``）按 ``kind`` 分派；本模块在它之上再加一层
**元数据驱动的策略**：只有「声明 ``parallel_safe`` + 权限类别为 read + 无副作用」的 Call
才允许并行，其余一律串行（包括"只读但未声明并行"的工具）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from contracts.agent_v2.cancellation import CancelToken
from contracts.agent_v2.clock import Clock
from contracts.agent_v2.models import ToolCall
from services.agent_runtime_v2 import DispatchReport
from services.agent_runtime_v2 import ToolScheduler as ContractScheduler

from .models import ToolDefinition
from .registry import ToolRegistry


class ToolScheduler:
    """元数据驱动的调度器（对外接口与 Agent 1 的调度器一致）。"""

    def __init__(self, registry: ToolRegistry, *, clock: Clock | None = None) -> None:
        self.registry = registry
        self._inner = ContractScheduler(registry, clock=clock)
        self.report = DispatchReport()

    # ------------------------------------------------------------------ 查询
    def definition_for(self, name: str) -> ToolDefinition | None:
        return self.registry.definition(name)

    def parallelizable(self, call: ToolCall) -> bool:
        definition = self.registry.definition(call.name)
        return bool(definition is not None and definition.parallelizable)

    def specs(self) -> Sequence[Any]:
        return self.registry.specs()

    def spec_for(self, name: str) -> Any:
        return self.registry.spec(name)

    def kind_of(self, name: str) -> Any:
        return self._inner.kind_of(name)

    # ------------------------------------------------------------------ 执行
    async def execute_all(
        self,
        calls: Sequence[ToolCall],
        *,
        cancel: CancelToken | None = None,
        on_start: Any | None = None,
        on_result: Any | None = None,
        cache: dict[str, Any] | None = None,
    ) -> DispatchReport:
        parallel = [call for call in calls if self.parallelizable(call)]
        serial = [call for call in calls if not self.parallelizable(call)]

        store: dict[str, Any] = cache if cache is not None else {}
        merged = DispatchReport()
        by_id: dict[str, Any] = {}

        def merge(report: DispatchReport) -> None:
            for result in report.results:
                by_id[result.call_id] = result
            merged.duplicates.extend(report.duplicates)
            merged.max_concurrency_read_only = max(
                merged.max_concurrency_read_only, report.max_concurrency_read_only
            )
            merged.max_concurrency_side_effect = max(
                merged.max_concurrency_side_effect, report.max_concurrency_side_effect
            )

        if parallel:
            merge(
                await self._inner.execute_all(
                    parallel, cancel=cancel, on_start=on_start, on_result=on_result, cache=store
                )
            )
        for call in serial:
            # 逐个提交 → 天然串行，不会被内层调度器并行化
            merge(
                await self._inner.execute_all(
                    [call], cancel=cancel, on_start=on_start, on_result=on_result, cache=store
                )
            )

        merged.results = [by_id[call.call_id] for call in calls if call.call_id in by_id]
        self.report = merged
        return merged


__all__ = ["ToolScheduler"]
