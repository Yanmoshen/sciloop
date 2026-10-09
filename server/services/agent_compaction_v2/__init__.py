"""agent_compaction_v2：压缩领域流程。

    from services.agent_compaction_v2 import CompactionService

    svc = CompactionService(repo=repo, gateway=gateway, token_budget=6000)
    if svc.should_compact(thread_id):
        result = asyncio.run(svc.compact(thread_id, trigger="auto"))
"""

from __future__ import annotations

from .compaction import DEFAULT_SUMMARIZER_PROMPT, CompactionResult, CompactionService

__all__ = ["CompactionService", "CompactionResult", "DEFAULT_SUMMARIZER_PROMPT"]
