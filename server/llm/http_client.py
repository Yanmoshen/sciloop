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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from llm.errors import (
    AttemptRecord,
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMRateLimitError,
    classify_status,
    from_exception,
)
from llm.providers import chat_completions_url
from llm.types import ResolvedModel, Usage

logger = logging.getLogger(__name__)

#: 统一超时（connect/read/write/pool 各自上限，秒）。
#: 2026-09-24 由 60 提到 300：实测推理类模型出字约 50 tokens/秒，输出 3000+ tokens
#: 就要 60–70 秒；**输出已不设上限**，再配一个 60 秒的读超时等于把长回答判成失败。
DEFAULT_TIMEOUT_SECONDS = 300.0
#: 流式调用的读超时：按"两个 chunk 之间"计算。与 :data:`DEFAULT_TIMEOUT_SECONDS` 对齐为
#: 300 秒 —— 口径统一，不因为"走不走流式"给出不同的耐心上限。
DEFAULT_STREAM_TIMEOUT_SECONDS = 300.0
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
        stream_timeout: float = DEFAULT_STREAM_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
        client: Any | None = None,
        transport: Any | None = None,
    ) -> None:
        self.timeout = timeout
        self.stream_timeout = stream_timeout
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

    # -- 流式调用（只给首页对话用） ------------------------------------------ #
    async def chat_completions_stream(
        self,
        *,
        model: ResolvedModel,
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        """发起一次**流式** chat completions 调用，逐个 yield 上游 SSE 的 JSON 对象。

        与 :meth:`chat_completions` 的分工：

        - 这里**不做传输层重试**：一旦有内容已经推给前端，重试就会让用户看到重复文本。
          建连阶段的失败（HTTP >= 400 / 连不上）仍会抛 :class:`LLMError`，由调用方决定降级。
        - 不做内容解析，只把 ``data:`` 行解析成 dict 交出去（解析在适配层做）。
        """
        url = chat_completions_url(model.base_url)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {model.api_key}",
        }
        if not model.api_key:
            raise LLMAuthError(
                f"供应商 {model.provider} 未配置 API Key（可在设置页填写或改用 env: 引用）",
                provider=model.provider,
                model=model.model_id,
            )

        client = self._get_client()
        started = time.perf_counter()
        try:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
                timeout=self.stream_timeout,
            ) as response:
                if response.status_code >= 400:
                    body_text = await _aread_text(response)
                    error_cls = classify_status(response.status_code, body_text)
                    message = _error_message(body_text) or f"HTTP {response.status_code}"
                    raise error_cls(
                        message,
                        provider=model.provider,
                        model=model.model_id,
                        status_code=response.status_code,
                        detail=_truncate(body_text),
                    )
                async for line in response.aiter_lines():
                    chunk = _parse_sse_line(line)
                    if chunk is None:
                        continue
                    if chunk is _SSE_DONE:
                        break
                    yield chunk
        except LLMError as exc:
            duration = int((time.perf_counter() - started) * 1000)
            exc.attempts = [
                AttemptRecord(
                    index=1,
                    duration_ms=duration,
                    status_code=exc.status_code,
                    error_kind=exc.kind,
                    error_message=exc.message,
                )
            ]
            raise
        except Exception as exc:  # noqa: BLE001 - 归一为 LLMError（含流中途断线）
            duration = int((time.perf_counter() - started) * 1000)
            error = from_exception(exc, provider=model.provider, model=model.model_id)
            error.attempts = [
                AttemptRecord(
                    index=1,
                    duration_ms=duration,
                    error_kind=error.kind,
                    error_message=error.message,
                )
            ]
            raise error from exc

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
    if not str(content).strip():
        # 思考型模型（或网关）可能把正文放在 reasoning_content / reasoning，
        # 而 content 为空、completion_tokens 却照常计入 —— 此时改读思考内容，
        # 否则前端会拿到空正文（表现为「已完成但没有任何输出」）。
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if isinstance(reasoning, list):
            reasoning = "".join(
                str(item.get("text") or "") if isinstance(item, dict) else str(item)
                for item in reasoning
            )
        if isinstance(reasoning, str) and reasoning.strip():
            content = reasoning
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


# --------------------------------------------------------------------------- #
# 流式（SSE）解析
# --------------------------------------------------------------------------- #
#: ``data: [DONE]`` 的哨兵：与「解析失败跳过」区分开，让调用方能正常收尾
_SSE_DONE: Any = object()


async def _aread_text(response: Any) -> str:
    try:
        raw = await response.aread()
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")[:MAX_RESPONSE_BYTES]
    return str(raw)[:MAX_RESPONSE_BYTES]


def _parse_sse_line(line: str) -> Any:
    """把一行 SSE 解析成 dict。

    返回 ``None`` = 跳过（空行 / 注释 / 非 data 行 / 非 JSON 的分片）；
    返回 :data:`_SSE_DONE` = 流结束；返回 dict = 一个数据块。
    """
    text = (line or "").strip()
    if not text or text.startswith(":"):
        return None
    if not text.startswith("data:"):
        return None
    data = text[5:].strip()
    if not data:
        return None
    if data == "[DONE]":
        return _SSE_DONE
    return _safe_json(data)


def extract_tool_call_deltas(chunk: dict[str, Any]) -> list[dict[str, Any]]:
    """从流式分片里取出**工具调用增量**（原始分片，不做合并）。

    单独一个函数而不是塞进 `extract_delta`：后者的返回是三元组
    `(正文, 思考, finish)`，再加第四项会让所有既有调用点都要改签名；
    而工具增量只有流式累加器关心。取不到即空列表 —— 「这一片没有工具增量」，
    不是「不支持」。合并规则见 `adapter._merge_tool_call_deltas`。
    """

    choices = chunk.get("choices") or []
    if not choices:
        return []
    first = choices[0]
    if not isinstance(first, dict):
        return []
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return []
    calls = delta.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def extract_delta(chunk: dict[str, Any]) -> tuple[str, str, str | None]:
    """从流式分片里取出 ``(正文增量, 思考增量, finish_reason)``。

    兼容三种真实形态：``delta.content``、``delta.reasoning_content``（思考型模型）、
    以及个别网关直接给 ``choices[0].text``。
    """
    choices = chunk.get("choices") or []
    if not choices:
        return "", "", None
    first = choices[0] or {}
    if not isinstance(first, dict):  # pragma: no cover - 防御上游异常结构
        return "", "", None
    finish_reason = first.get("finish_reason")
    delta = first.get("delta")
    if not isinstance(delta, dict):
        delta = {}
    content = delta.get("content")
    if content is None:
        content = first.get("text")
    if isinstance(content, list):
        content = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in content
        )
    reasoning = delta.get("reasoning_content")
    if reasoning is None:
        reasoning = delta.get("reasoning")
    if isinstance(reasoning, list):
        reasoning = "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in reasoning
        )
    return str(content or ""), str(reasoning or ""), finish_reason


def extract_usage(chunk: dict[str, Any]) -> Usage | None:
    """流式分片里的 ``usage``（多数供应商只在最后一帧给，且需 ``stream_options``）。"""
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    return Usage.from_openai(usage)


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
    "DEFAULT_STREAM_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "ChatOutcome",
    "OpenAICompatibleClient",
    "extract_delta",
    "extract_usage",
]
