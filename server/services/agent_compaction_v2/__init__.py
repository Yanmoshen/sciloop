"""agent_compaction_v2：压缩与 recap（计划书 WP-06）。

    from services.agent_compaction_v2 import (
        CompactionService, CompactionPolicy, CompactionTrigger, Recap,
    )

    policy = CompactionPolicy(token_budget=6000)
    svc = CompactionService(repo=repo, gateway=gateway, policy=policy)
    if svc.should_compact(thread_id):
        result = await svc.compact(thread_id, trigger="auto")

模块布局说明：计划书 §WP-06 期待 ``recap.py`` / ``service.py`` / ``policy.py``。
``recap.py`` 与 ``policy.py`` 为独立实现；压缩主流程实现在 ``compaction.py``，
``service.py`` 作为规范导入路径再导出，保持两种 import 都可用。
"""

from __future__ import annotations

from .compaction import DEFAULT_SUMMARIZER_PROMPT, CompactionResult, CompactionService
from .policy import CompactionPolicy, CompactionTrigger
from .recap import (
    RECAP_FIELDS,
    RECAP_INSTRUCTIONS,
    Recap,
    RecapFact,
    RecapStatus,
    RecapViolation,
    build_recap,
    coerce_recap,
    parse_recap,
)

__all__ = [
    "CompactionService",
    "CompactionResult",
    "DEFAULT_SUMMARIZER_PROMPT",
    "CompactionPolicy",
    "CompactionTrigger",
    "Recap",
    "RecapFact",
    "RecapStatus",
    "RecapViolation",
    "RECAP_FIELDS",
    "RECAP_INSTRUCTIONS",
    "build_recap",
    "coerce_recap",
    "parse_recap",
]
