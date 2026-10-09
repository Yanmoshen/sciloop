"""agent_runtime_v2：不依赖 Web 请求的执行循环。

    from services.agent_runtime_v2 import TurnRuntime, ToolScheduler

    runtime = TurnRuntime(repo=repo, gateway=gateway, tools=ToolScheduler(executor))
    outcome = asyncio.run(runtime.run(thread_id, turn_id))
"""

from __future__ import annotations

from .context import (
    CONTEXT_ITEM_TYPES,
    build_context,
    compaction_covered_until,
    estimate_tokens,
    item_to_message,
    transcript,
)
from .runtime import ALLOW, REQUIRE, ApprovalGate, TurnOutcome, TurnRuntime
from .tools import DispatchReport, ToolScheduler

__all__ = [
    "TurnRuntime",
    "TurnOutcome",
    "ApprovalGate",
    "ALLOW",
    "REQUIRE",
    "ToolScheduler",
    "DispatchReport",
    "build_context",
    "item_to_message",
    "compaction_covered_until",
    "estimate_tokens",
    "transcript",
    "CONTEXT_ITEM_TYPES",
]
