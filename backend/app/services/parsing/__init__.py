# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""8 字段解析卡片服务（WP06）。

对外契约（供 WP07 / WP08 / WP13 使用）::

    from app.services.parsing import build_card, get_card, list_card_versions

    result = await build_card(paper_id, force=False)   # 生成 vN+1 并落库
    row = await get_card(paper_id)                     # 默认最新版本
    versions = await list_card_versions(paper_id)      # force 重解析后旧版本仍在

卡片 8 字段（附录 A.2 ``paper_cards``）：
``research_problem`` / ``core_method`` / ``key_innovation[]`` / ``technical_route[]`` /
``experimental_setup{datasets,baselines,metrics}`` / ``main_conclusions[]`` /
``limitations[]`` / ``transferable[]``。

结论性条目（``key_innovation`` / ``main_conclusions`` / ``limitations``）的
``evidence_span`` 由 :mod:`app.services.parsing.locator` 回原文核对后填入；
核对不上时``evidence_span=null`` 并计入 ``unlocated_count``（禁止编造引用）。
"""

from __future__ import annotations

from app.services.parsing.card_builder import (
    CARD_BUILDER_VERSION,
    CARD_FIELDS,
    CARD_JOB_NAME,
    CARD_SCHEMA,
    CONCLUDING_FIELDS,
    SCOPE_ABSTRACT_ONLY,
    SCOPE_FULLTEXT,
    SUBMISSION_PATTERNS,
    SYSTEM_PROMPT,
    CardContext,
    CardPayload,
    CardRow,
    CardValidationError,
    PaperNotFoundError,
    SqlCardRepository,
    build_card,
    build_messages,
    call_card_llm,
    collect_card_context,
    get_card,
    get_card_task,
    get_llm_call_log,
    list_card_tasks,
    list_card_versions,
    run_card_build_sync,
    submit_card_build,
    unknown_fields,
    validate_card_payload,
)
from app.services.parsing.locator import (
    FULLTEXT_GATE_COVERAGE,
    LocateReport,
    SpanIndex,
    gate_state,
    locate_card,
    locate_normalized,
    normalize_text,
    verify_located_span,
)

__all__ = [
    # 建卡入口
    "build_card",
    "get_card",
    "list_card_versions",
    "get_llm_call_log",
    "submit_card_build",
    "run_card_build_sync",
    "get_card_task",
    "list_card_tasks",
    # 结构契约
    "CARD_FIELDS",
    "CARD_SCHEMA",
    "CARD_BUILDER_VERSION",
    "CARD_JOB_NAME",
    "CONCLUDING_FIELDS",
    "SCOPE_ABSTRACT_ONLY",
    "SCOPE_FULLTEXT",
    "SYSTEM_PROMPT",
    "SUBMISSION_PATTERNS",
    "CardPayload",
    "CardContext",
    "CardRow",
    "CardValidationError",
    "PaperNotFoundError",
    "SqlCardRepository",
    "build_messages",
    "call_card_llm",
    "collect_card_context",
    "unknown_fields",
    "validate_card_payload",
    # 定位
    "FULLTEXT_GATE_COVERAGE",
    "LocateReport",
    "SpanIndex",
    "gate_state",
    "locate_card",
    "locate_normalized",
    "normalize_text",
    "verify_located_span",
]
