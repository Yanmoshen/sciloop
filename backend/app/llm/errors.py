# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""LLM 错误分类。

错误分类同时服务三件事：重试策略、``llm_call_logs.error`` 的写法、失败分析报告（WP17）。

分类口径：

============ ============ ==================================================
kind         retryable    典型成因
============ ============ ==================================================
timeout      True         连接/读取超时、上游无响应
rate_limit   True         HTTP 429 或上游限流文案
server       True         HTTP 5xx、上游网关错误
auth         False        HTTP 401/403、key 无效或欠费
bad_request  False        HTTP 400/404/422、参数或 schema 不合法
unknown      False        其它无法归类的异常
============ ============ ==================================================

**auth / bad_request 不重试**：重试只会白烧 token，且会污染成本护栏的读数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ErrorKind = Literal["timeout", "rate_limit", "auth", "bad_request", "server", "unknown"]

RETRYABLE_KINDS: frozenset[str] = frozenset({"timeout", "rate_limit", "server"})


@dataclass
class AttemptRecord:
    """一次 HTTP 尝试的留痕（每个 AttemptRecord 对应 ``llm_call_logs`` 的一行）。"""

    index: int
    duration_ms: int
    status_code: int | None = None
    error_kind: str | None = None
    error_message: str | None = None
    retry_after_seconds: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def ok(self) -> bool:
        return self.error_kind is None


class LLMError(Exception):
    """LLM 层异常基类。"""

    kind: ErrorKind = "unknown"
    retryable: bool = False
    http_status: int | None = None

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
        attempts: list[AttemptRecord] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.status_code = status_code if status_code is not None else self.http_status
        self.detail = detail
        #: 本次逻辑调用涉及的 HTTP 尝试（含重试），供适配层逐行落库
        self.attempts: list[AttemptRecord] = list(attempts or [])

    @property
    def model_ref(self) -> str | None:
        if self.provider and self.model:
            return f"{self.provider}:{self.model}"
        return self.model

    def with_attempt(self, attempt: AttemptRecord) -> LLMError:
        """追加一条尝试记录（返回 self，便于链式书写）。"""
        self.attempts.append(attempt)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.kind,
            "message": self.message,
            "detail": {
                "provider": self.provider,
                "model": self.model,
                "status_code": self.status_code,
                "retryable": self.retryable,
                "raw_detail": self.detail,
            },
        }


class LLMTimeoutError(LLMError):
    """超时。"""

    kind = "timeout"
    retryable = True
    http_status = 504


class LLMRateLimitError(LLMError):
    """被限流。"""

    kind = "rate_limit"
    retryable = True
    http_status = 429

    def __init__(self, message: str, *, retry_after_seconds: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class LLMAuthError(LLMError):
    """鉴权失败（401/403）。"""

    kind = "auth"
    retryable = False
    http_status = 401


class LLMBadRequestError(LLMError):
    """请求不合法（400/404/422）。"""

    kind = "bad_request"
    retryable = False
    http_status = 400


class LLMServerError(LLMError):
    """上游服务端错误（5xx）。"""

    kind = "server"
    retryable = True
    http_status = 502


class LLMJSONValidationError(LLMError):
    """结构化输出校验失败（重试 ``LLM_JSON_RETRY`` 次后仍失败）。

    ``raw_responses`` 保留每一次的原始文本，便于回到 prompt 层定位问题（WP02-T2）。
    """

    kind = "bad_request"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        attempts: list[AttemptRecord] | None = None,
        validation_error: str | None = None,
        raw_responses: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, attempts=attempts, **kwargs)
        self.validation_error = validation_error
        self.raw_responses = list(raw_responses or [])

    @property
    def call_count(self) -> int:
        return len(self.attempts)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["detail"]["validation_error"] = self.validation_error
        payload["detail"]["call_count"] = self.call_count
        payload["detail"]["raw_responses"] = self.raw_responses
        return payload


class ModelRoutingError(LLMError):
    """路由无法解析（无路由配置、供应商缺 key、单价缺失导致无法安全执行等）。"""

    kind = "bad_request"
    retryable = False


class IsolationViolation(LLMError):
    """盲评隔离红线被破坏：``generator_model_ref == reviewer_model_ref``。

    契约要求（contracts.blind_review_rules.isolation）：无法满足时 ``plan_review``
    直接失败，**禁止静默回退同一模型**。WP12 依赖本异常做门禁。
    """

    kind = "bad_request"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        generator_stage: str | None = None,
        reviewer_stage: str | None = None,
        generator_ref: str | None = None,
        reviewer_ref: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.generator_stage = generator_stage
        self.reviewer_stage = reviewer_stage
        self.generator_ref = generator_ref
        self.reviewer_ref = reviewer_ref

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["detail"].update(
            {
                "generator_stage": self.generator_stage,
                "reviewer_stage": self.reviewer_stage,
                "generator_ref": self.generator_ref,
                "reviewer_ref": self.reviewer_ref,
            }
        )
        return payload


class ReplayMissError(LLMError):
    """``LLM_REPLAY=1`` 但 ``demo_fixtures`` 未命中。

    禁止静默走实时：演示态下静默实时调用会让「离线可演示」与「成本可控」两个承诺同时失效。
    """

    kind = "bad_request"
    retryable = False

    def __init__(self, message: str, *, prompt_hash: str | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.prompt_hash = prompt_hash

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["detail"]["prompt_hash"] = self.prompt_hash
        return payload


class SecretBackendUnavailable(LLMError):
    """缺少对称加密依赖，无法安全存储供应商 API Key（禁止退化为明文存储）。"""

    kind = "bad_request"
    retryable = False


# --------------------------------------------------------------------------- #
# 分类工具
# --------------------------------------------------------------------------- #
def classify_status(status_code: int | None, body: str | None = None) -> type[LLMError]:
    """把 HTTP 状态码 + 响应体归入错误分类。"""
    if status_code is None:
        return LLMServerError
    if status_code in (401, 403):
        return LLMAuthError
    if status_code == 429:
        return LLMRateLimitError
    if status_code == 408:
        return LLMTimeoutError
    if status_code >= 500:
        return LLMServerError
    if 400 <= status_code < 500:
        return LLMBadRequestError
    text = (body or "").lower()
    if "timeout" in text or "timed out" in text:
        return LLMTimeoutError
    if "rate limit" in text or "too many requests" in text:
        return LLMRateLimitError
    if "api key" in text or "unauthorized" in text or "invalid key" in text:
        return LLMAuthError
    return LLMError


def from_exception(
    exc: BaseException,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> LLMError:
    """把 httpx/内置异常归一为 :class:`LLMError`。"""
    if isinstance(exc, LLMError):
        return exc
    name = type(exc).__name__.lower()
    message = str(exc) or type(exc).__name__
    if "timeout" in name or "timeout" in message.lower():
        return LLMTimeoutError(message, provider=provider, model=model)
    if isinstance(exc, (ConnectionError, OSError)) or "connect" in name:
        return LLMServerError(message, provider=provider, model=model)
    return LLMError(message, provider=provider, model=model)


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, LLMError) and exc.retryable


__all__ = [
    "RETRYABLE_KINDS",
    "AttemptRecord",
    "ErrorKind",
    "IsolationViolation",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMError",
    "LLMJSONValidationError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTimeoutError",
    "ModelRoutingError",
    "ReplayMissError",
    "SecretBackendUnavailable",
    "classify_status",
    "from_exception",
    "is_retryable",
]
