# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""SciLoop LLM 适配层（WP02）。

对外只暴露三个入口：

- :func:`app.llm.adapter.chat` —— 所有环节唯一的 LLM 调用入口
- :func:`app.llm.router.resolve_pair` —— 盲评隔离校验（WP12 强依赖）
- :func:`app.llm.replay` / SSE ``demo_mode`` 事件 —— 演示回放兜底

设计红线（见 contracts.json）：

- 每一次调用（含传输层每次重试、每次 JSON 校验重试）都必须写 ``llm_call_logs``
- 单价缺失时 ``cost_usd`` 置 ``null`` 并告警，**禁止估算造假**
- ``LLM_REPLAY=1`` 时未命中 fixture 必须抛错，**禁止静默走实时**
- 生成模型与评审模型 ``model_ref`` 必须不同，否则 ``IsolationViolation``
"""

from __future__ import annotations

from app.llm.adapter import chat, chat_json
from app.llm.errors import (
    IsolationViolation,
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMJSONValidationError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    ModelRoutingError,
    ReplayMissError,
)
from app.llm.events import LLMEvent, default_bus, emit_event, subscribe
from app.llm.replay import compute_prompt_hash, is_replay_enabled
from app.llm.router import (
    ModelRouter,
    get_router,
    resolve,
    resolve_chain,
    resolve_pair,
)
from app.llm.types import CallLogEntry, LLMResult, ModelRef, ResolvedModel, Usage

__all__ = [
    "CallLogEntry",
    "IsolationViolation",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMError",
    "LLMEvent",
    "LLMJSONValidationError",
    "LLMRateLimitError",
    "LLMResult",
    "LLMServerError",
    "LLMTimeoutError",
    "ModelRef",
    "ModelRouter",
    "ModelRoutingError",
    "ReplayMissError",
    "ResolvedModel",
    "Usage",
    "chat",
    "chat_json",
    "compute_prompt_hash",
    "default_bus",
    "emit_event",
    "get_router",
    "is_replay_enabled",
    "resolve",
    "resolve_chain",
    "resolve_pair",
    "subscribe",
]
