# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""响应回放（演示与现场网络兜底）。

``LLM_REPLAY=1`` 时，适配层不再发起实时请求，而是按 ``prompt_hash`` 从 ``demo_fixtures``
（``fixture_type='llm_response'``）取预录响应，并把结果标记 ``is_replay=true``。

两条红线：

1. **未命中必须抛 :class:`ReplayMissError`**，禁止静默走实时（否则演示会悄悄变成付费实时调用，
   且评委看到的「回放」结果无法与实时结果区分）
2. 回放结果在 UI 上必须由 DemoBadge 明示（SSE ``demo_mode`` 事件由本模块广播）

``prompt_hash`` 的算法在本模块唯一定义，WP16 录制 fixture 时必须复用 :func:`compute_prompt_hash`。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.llm.errors import ReplayMissError
from app.llm.events import emit_demo_mode, emit_event
from app.llm.store import FIXTURE_TYPE_LLM_RESPONSE, LLMStore, get_store
from app.llm.types import LLMResult, Message, Usage

logger = logging.getLogger(__name__)

#: prompt_hash 算法版本；任何参与哈希的字段变更都必须升版本（同时写进 fixture.note）
PROMPT_HASH_VERSION = "v1"

_HASH_FIELDS = ("model_ref", "messages", "temperature", "max_tokens", "json_schema")


def is_replay_enabled(explicit: bool | None = None) -> bool:
    """回放开关：显式参数 > 环境变量 ``LLM_REPLAY``。

    支持 ``1/true/yes/on`` 四种写法（``.env`` 里经常混用）。
    """
    if explicit is not None:
        return bool(explicit)
    raw = os.environ.get("LLM_REPLAY", "")
    if raw:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from app.core.config import get_settings

        return bool(get_settings().llm_replay)
    except Exception:  # pragma: no cover - 配置未就绪
        return False


def json_retry_limit(explicit: int | None = None) -> int:
    """``LLM_JSON_RETRY``（默认 2 ⇒ 共 3 次调用）。"""
    if explicit is not None:
        return max(0, int(explicit))
    try:
        from app.core.config import get_settings

        return max(0, int(get_settings().llm_json_retry))
    except Exception:  # pragma: no cover
        raw = os.environ.get("LLM_JSON_RETRY", "2")
        try:
            return max(0, int(raw))
        except ValueError:
            return 2


def compute_prompt_hash(
    *,
    model_ref: str,
    messages: list[Message],
    temperature: float | None,
    max_tokens: int | None,
    json_schema: dict[str, Any] | None = None,
) -> str:
    """计算稳定 ``prompt_hash``（sha256 hex，64 字符）。

    参与哈希的字段固定为 :data:`_HASH_FIELDS`，且对 key 排序、去除多余空白，
    保证「同输入 → 同哈希」，与调用次数、时间、项目无关。
    """
    canonical = {
        "version": PROMPT_HASH_VERSION,
        "model_ref": str(model_ref),
        "messages": _normalize_messages(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "json_schema": json_schema,
    }
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _normalize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            normalized.append({"role": "user", "content": str(message)})
            continue
        normalized.append(
            {
                "role": str(message.get("role", "user")),
                "content": message.get("content", ""),
            }
        )
    return normalized


@dataclass
class ReplayHit:
    """一次命中的回放。"""

    prompt_hash: str
    content: str
    usage: Usage = field(default_factory=Usage)
    model_returned: str | None = None
    finish_reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_result(
        self,
        *,
        model_ref: str,
        provider: str,
        model_id: str,
        cost_usd: float | None,
        cost_unknown_reason: str | None,
        duration_ms: int,
        stage: str | None,
        purpose: str | None,
        parsed: Any = None,
    ) -> LLMResult:
        return LLMResult(
            content=self.content,
            model_ref=model_ref,
            provider=provider,
            model_id=model_id,
            usage=self.usage,
            cost_usd=cost_usd,
            cost_unknown_reason=cost_unknown_reason,
            duration_ms=duration_ms,
            attempts=1,
            http_calls=0,
            is_replay=True,
            finish_reason=self.finish_reason or "replay",
            parsed=parsed,
            stage=stage,
            purpose=purpose,
            prompt_hash=self.prompt_hash,
            resolved_from="replay_fixture",
        )


def build_fixture_payload(
    *,
    content: str,
    model: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    finish_reason: str | None = None,
    note_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 ``demo_fixtures.payload``（WP16 录制时使用本函数，保证字段口径一致）。"""
    payload: dict[str, Any] = {
        "content": content,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "finish_reason": finish_reason or "stop",
        "prompt_hash_version": PROMPT_HASH_VERSION,
    }
    if note_extra:
        payload["note_extra"] = dict(note_extra)
    return payload


def payload_to_content(payload: dict[str, Any] | None) -> str:
    """从 fixture payload 中取回文本内容（兼容 ``choices`` 原始响应形态）。"""
    if not payload:
        return ""
    if isinstance(payload.get("content"), str):
        return payload["content"]
    choices = payload.get("choices") or []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message.get("content"), str):
            return message["content"]
    # 允许整个 fixture 存的就是一个完整响应体
    if isinstance(payload.get("response"), dict):
        return payload_to_content(payload["response"])
    return ""


async def load_replay(
    prompt_hash: str,
    *,
    store: LLMStore | None = None,
) -> ReplayHit | None:
    """按 ``prompt_hash`` 查 fixture；未命中返回 ``None``（由调用方决定如何报错）。"""
    active_store = store or get_store()
    payload = await active_store.find_fixture(FIXTURE_TYPE_LLM_RESPONSE, prompt_hash)
    if payload is None:
        return None
    content = payload_to_content(payload)
    usage = Usage.from_openai(
        {
            "prompt_tokens": payload.get("prompt_tokens"),
            "completion_tokens": payload.get("completion_tokens"),
        }
    )
    return ReplayHit(
        prompt_hash=prompt_hash,
        content=content,
        usage=usage,
        model_returned=payload.get("model"),
        finish_reason=payload.get("finish_reason"),
        payload=payload,
    )


async def replay_or_raise(
    prompt_hash: str,
    *,
    store: LLMStore | None = None,
    model_ref: str,
    stage: str | None = None,
    purpose: str | None = None,
) -> ReplayHit:
    """命中则返回；未命中抛 :class:`ReplayMissError` 并广播 ``replay_miss`` 事件。"""
    hit = await load_replay(prompt_hash, store=store)
    if hit is None:
        message = (
            f"回放未命中：demo_fixtures 中不存在 fixture_type='llm_response' "
            f"且 fixture_key='{prompt_hash}' 的记录（model_ref={model_ref}）。"
            "LLM_REPLAY=1 时禁止静默走实时调用，请先用 WP16 的录制脚本补齐 fixture，"
            "或关闭 LLM_REPLAY。"
        )
        await emit_event(
            "replay_miss",
            {"prompt_hash": prompt_hash, "model_ref": model_ref, "stage": stage, "purpose": purpose},
        )
        raise ReplayMissError(
            message, prompt_hash=prompt_hash, provider=model_ref.split(":", 1)[0], model=model_ref
        )
    # 回放命中：为离线演练广播 demo_mode（SSE 载荷 {snapshot, replay}）
    await emit_demo_mode(snapshot=False, replay=True, prompt_hash=prompt_hash)
    return hit


__all__ = [
    "PROMPT_HASH_VERSION",
    "ReplayHit",
    "build_fixture_payload",
    "compute_prompt_hash",
    "is_replay_enabled",
    "json_retry_limit",
    "load_replay",
    "payload_to_content",
    "replay_or_raise",
]
