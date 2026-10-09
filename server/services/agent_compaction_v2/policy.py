"""压缩触发策略（计划书 WP-06「触发：token 预算、用户手动请求或运行时明确需要」）。

策略与执行解耦：本模块只回答「该不该压缩、为什么」，
真正的压缩由 :class:`~services.agent_compaction_v2.service.CompactionService` 执行。
这样令牌预算的判定可以单独测试，不需要模型或事件存储。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class CompactionTrigger(StrEnum):
    """触发来源。"""

    AUTO = "auto"        # token 预算
    MANUAL = "manual"    # 用户手动请求
    RUNTIME = "runtime"  # 运行时明确需要（例如上下文超限错误后）


@dataclass
class CompactionPolicy:
    """压缩策略参数。"""

    #: token 预算：估算超过即触发自动压缩
    token_budget: int = 6000
    #: 距上次压缩至少要有这么多条新内容事件才值得再压（默认 1，防止空转）
    min_new_events: int = 1
    #: 单个 Turn 内允许的自动压缩次数上限
    max_auto_per_turn: int = 1

    def should_compact(
        self,
        *,
        estimated_tokens: int,
        new_events_since_last: int,
        auto_count_in_turn: int = 0,
    ) -> bool:
        """是否需要（自动）压缩。"""
        if auto_count_in_turn >= self.max_auto_per_turn:
            return False
        if new_events_since_last < self.min_new_events:
            return False
        return estimated_tokens > self.token_budget

    def decide(
        self,
        *,
        estimated_tokens: int,
        new_events_since_last: int,
        auto_count_in_turn: int = 0,
        manual_request: bool = False,
        context_overflow: bool = False,
    ) -> CompactionTrigger | None:
        """给出触发来源；不需要压缩返回 ``None``。

        手动请求与上下文超限**绕过预算判定**——它们本来就是「必须压」的信号，
        但仍要求「有新内容可压」，否则压了也是空转。
        """
        if new_events_since_last < self.min_new_events:
            return None
        if context_overflow:
            return CompactionTrigger.RUNTIME
        if manual_request:
            return CompactionTrigger.MANUAL
        if self.should_compact(
            estimated_tokens=estimated_tokens,
            new_events_since_last=new_events_since_last,
            auto_count_in_turn=auto_count_in_turn,
        ):
            return CompactionTrigger.AUTO
        return None

    def describe(self, trigger: CompactionTrigger, *, estimated_tokens: int) -> dict[str, Any]:
        """给事件的载荷：让「为什么压」可审计。"""
        return {
            "trigger": trigger.value,
            "token_budget": self.token_budget,
            "estimated_tokens": estimated_tokens,
            "min_new_events": self.min_new_events,
            "max_auto_per_turn": self.max_auto_per_turn,
        }


__all__ = ["CompactionPolicy", "CompactionTrigger"]
