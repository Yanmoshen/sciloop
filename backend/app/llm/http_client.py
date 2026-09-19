# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""OpenAI 兼容 ``/v1/chat/completions`` 传输层。

职责边界：**只负责 HTTP**（URL 组装、鉴权头、超时、传输层重试、错误分类、逐次尝试留痕），
不理解业务语义，也不写数据库。记账由适配层根据 :class:`ChatOutcome` / :class:`LLMError.attempts` 完成。

传输层重试只针对**幂等且可自愈**的错误（``timeout`` / ``rate_limit`` / ``server``）；
``auth`` 与 ``bad_request`` 立即失败（重试只会白烧成本）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from app.llm.errors import (
    AttemptRecord,
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMRateLimitError,
    classify_status,
    from_exception,
)
from app.llm.providers import chat_completions_url
from app.llm.types import ResolvedModel, Usage

logger = logging.getLogger(__name__)

#: 统一超时（connect/read/write/pool 各自上限，秒）
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 0.8
MAX_BACKOFF_SECONDS = 8.0
#: 单次响应体读取上限（防御性，5 MB，与执行器输出上限同量级）
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


@dataclass
class ChatOutcome:
    """一次成功的 chat completions 调用（可能内含若干次 HTTP 重试）。"""

    payload: dict[str, Any]
    content: str
    usage: Usage
    finish_reason: str | None
    model_returned: str | None
    attempts: list[AttemptRecord] = field(default_factory=list)

    @property
    def http_calls(self) -> int:
        return len(self.attempts)


class OpenAICompatibleClient:
    """OpenAI 兼容协议的 httpx 客户端封装。"""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
        client: Any | None = None,
        transport: Any | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._client = client
        self._transport = transport

    # -- httpx 客户端 ------------------------------------------------------ #
    def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                follow_redirects=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 主调用 ------------------------------------------------------------ #
    async def chat_completions(
        self,
        *,
        model: ResolvedModel,
        payload: dict[str, Any],
    ) -> ChatOutcome:
        """发起一次 chat completions 调用（含传输层重试）。

        失败时抛 :class:`LLMError` 子类，``exc.attempts`` 含**全部** HTTP 尝试记录，
        适配层据此逐行写入 ``llm_call_logs``。
        """
        url = chat_completions_url(model.base_url)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {model.api_key}",
        }
        if not model.api_key:
            raise LLMAuthError(
                f"供应商 {model.provider} 未配置 API Key（可在设置页填写或改用 env: 引用）",
                provider=model.provider,
                model=model.model_id,
            )

        attempts: list[AttemptRecord] = []
        last_error: LLMError | None = None

        for index in range(1, self.max_retries + 2):
            started = time.perf_counter()
            try:
                response = await self._post(url, headers, payload)
            except Exception as exc:  # noqa: BLE001 - 归一为 LLMError
                error = from_exception(exc, provider=model.provider, model=model.model_id)
                duration = int((time.perf_counter() - started) * 1000)
                attempts.append(
                    AttemptRecord(
                        index=index,
                        duration_ms=duration,
                        error_kind=error.kind,
                        error_message=error.message,
                    )
                )
                error.attempts = attempts
                last_error = error
                if error.retryable and index <= self.max_retries:
                    await self._sleep(index, None)
                    continue
                raise error from exc

            duration = int((time.perf_counter() - started) * 1000)
            status = response.status_code
            body_text = _safe_text(response)

            if status >= 400:
                error_cls = classify_status(status, body_text)
                retry_after = _parse_retry_after(response)
                message = _error_message(body_text) or f"HTTP {status}"
                error: LLMError
                if error_cls is LLMRateLimitError:
                    error = LLMRateLimitError(
                        message,
                        retry_after_seconds=retry_after,
                        provider=model.provider,
                        model=model.model_id,
                        status_code=status,
                        detail=_truncate(body_text),
                    )
                else:
                    error = error_cls(
                        message,
                        provider=model.provider,
                        model=model.model_id,
                        status_code=status,
                        detail=_truncate(body_text),
                    )
                attempts.append(
                    AttemptRecord(
                        index=index,
                        duration_ms=duration,
                        status_code=status,
                        error_kind=error.kind,
                        error_message=error.message,
                        retry_after_seconds=retry_after,
                    )
                )
                error.attempts = attempts
                # 400 通常意味着参数/schema 不被接受：交给适配层判断是否需要降档，不在这里重试
                if isinstance(error, LLMBadRequestError) or error.kind == "bad_request":
                    raise error
                if error.retryable and index <= self.max_retries:
                    last_error = error
                    await self._sleep(index, retry_after)
                    continue
                raise error

            data = _safe_json(body_text)
            if data is None:
                error = LLMError(
                    "上游返回非 JSON 响应体",
                    provider=model.provider,
                    model=model.model_id,
                    status_code=status,
                    detail=_truncate(body_text),
                )
                attempts.append(
                    AttemptRecord(
                        index=index,
                        duration_ms=duration,
                        status_code=status,
                        error_kind=error.kind,
                        error_message=error.message,
                    )
                )
                error.attempts = attempts
                raise error

            content, finish_reason = _extract_content(data)
            usage = Usage.from_openai(data.get("usage"))
            attempts.append(
                AttemptRecord(
                    index=index,
                    duration_ms=duration,
                    status_code=status,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                )
            )
            return ChatOutcome(
                payload=data,
                content=content,
                usage=usage,
                finish_reason=finish_reason,
                model_returned=data.get("model"),
                attempts=attempts,
            )

        # 理论上不可达：循环内要么 return 要么 raise
        raise last_error or LLMError("未知调用失败", provider=model.provider, model=model.model_id)

    # -- 内部工具 ---------------------------------------------------------- #
    async def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Any:
        client = self._get_client()
        return await client.post(url, headers=headers, json=payload, timeout=self.timeout)

    async def _sleep(self, index: int, retry_after: float | None) -> None:
        delay = min(self.backoff_seconds * (2 ** (index - 1)), self.max_backoff_seconds)
        delay += random.uniform(0, 0.25 * delay)
        if retry_after is not None:
            delay = max(delay, min(retry_after, self.max_backoff_seconds))
        logger.warning("LLM 调用失败，第 %s 次重试，等待 %.2fs", index, delay)
        await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# 解析工具
# --------------------------------------------------------------------------- #
def _safe_text(response: Any) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        return ""
    if len(text) > MAX_RESPONSE_BYTES:
        return text[:MAX_RESPONSE_BYTES]
    return text


def _safe_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_content(data: dict[str, Any]) -> tuple[str, str | None]:
    """从响应体中取出文本内容（兼容 content 为字符串或分段数组两种形态）。"""
    choices = data.get("choices") or []
    if not choices:
        return "", None
    first = choices[0] or {}
    finish_reason = first.get("finish_reason")
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        content = "".join(parts)
    if content is None:
        # 部分供应商把结果放在 text 字段
        content = first.get("text") or ""
    return str(content), finish_reason


def _error_message(body_text: str) -> str | None:
    data = _safe_json(body_text)
    if not data:
        return None
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "")[:400] or None
    if isinstance(error, str):
        return error[:400]
    message = data.get("message")
    return str(message)[:400] if message else None


def _parse_retry_after(response: Any) -> float | None:
    try:
        raw = response.headers.get("retry-after")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _truncate(text: str, limit: int = 800) -> str:
    return text[:limit] if text else ""


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "ChatOutcome",
    "OpenAICompatibleClient",
]
