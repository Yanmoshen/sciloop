"""压缩服务的规范导入路径（计划书 §WP-06 期待 ``service.py``）。

实现留在 :mod:`services.agent_compaction_v2.compaction`（历史原因，测试已指向它），
本模块只做再导出，保证 ``from services.agent_compaction_v2.service import CompactionService``
这条计划书里的路径同样可用。
"""

from __future__ import annotations

from .compaction import (
    DEFAULT_SUMMARIZER_PROMPT,
    CompactionResult,
    CompactionService,
)
from .policy import CompactionPolicy, CompactionTrigger

__all__ = [
    "CompactionService",
    "CompactionResult",
    "DEFAULT_SUMMARIZER_PROMPT",
    "CompactionPolicy",
    "CompactionTrigger",
]
