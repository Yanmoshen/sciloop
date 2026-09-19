# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""统一 LLM 调用入口（所有环节复用）。

``chat()`` 一次调用串起四件事：

1. **路由**：由 :class:`app.llm.router.ModelRouter` 按环节解析模型（含降级备用链）
2. **调用**：OpenAI 兼容 ``/v1/chat/completions``，统一超时 / 传输层重试 / 错误分类
3. **结构化输出**：本地 JSON Schema 校验，失败自动重试至多 ``LLM_JSON_RETRY``（默认 2）次
4. **记账**：每一次 HTTP 尝试都写 ``llm_call_logs``；单价缺失时 ``cost_usd`` 记 ``null`` 并告警

不可协商的行为：

- 结构化输出重试**不触发模型降级**（prompt/schema 问题应由人来修，换模型会掩盖问题）
- 传输层失败会按链降级，并广播 ``llm_fallback`` 事件（WP10 消费后补写 decision_logs）
- 记账失败默认让调用失败（``strict_logging=True``）：宁可重试，也不产生「查不到痕迹的花费」
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.llm import events as _events
from app.llm.errors import (
    AttemptRecord,
    IsolationViolation,
    LLMBadRequestError,
    LLMError,
    LLMJSONValidationError,
    ModelRoutingError,
    ReplayMissError,
)
from app.llm.http_client import OpenAICompatibleClient
from app.llm.pricing import PRICE_MISSING_WARNING, compute_cost_usd
from app.llm.providers import (
    detect_capability,
    json_strategy,
    looks_like_schema_rejection,
    note_strategy_rejected,
    with_schema_hint,
)
from app.llm.replay import compute_prompt_hash, is_replay_enabled, json_retry_limit, replay_or_raise
from app.llm.router import ModelRouter, get_router
from app.llm.schema import parse_and_validate
from app.llm.store import LLMStore, get_store
from app.llm.types import CallLogEntry, LLMResult, Message, ResolvedModel, Usage

logger = logging.getLogger(__name__)

DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 1536


async def chat(
    messages: list[Message] | str,
    model_ref: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_schema: dict[str, Any] | None = None,
    *,
    project_id: int | None = None,
    stage: str | None = None,
    purpose: str | None = None,
    store: LLMStore | None = None,
    router: ModelRouter | None = None,
    transport: OpenAICompatibleClient | None = None,
    allow_fallback: bool = True,
    allow_replay: bool | None = None,
    json_retry: int | None = None,
    strict_logging: bool = True,
    timeout: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> LLMResult:
    """调用 LLM 并返回结构化结果。

    :param messages: 对话消息（``[{"role","content"}]``）或纯文本
    :param model_ref: 显式模型 ``provider:model_id``；为 ``None`` 时按 ``stage`` 路由
    :param json_schema: 传入即启用结构化输出校验与自动重试
    :param stage: 六环节 / 前置环节名（写入 ``llm_call_logs.stage``）
    :param purpose: 更细的用途标签（写入 ``llm_call_logs.purpose``）
    :param allow_fallback: 传输层失败是否按链降级到备用模型
    :param allow_replay: 覆盖 ``LLM_REPLAY`` 开关（用于「回放/实时」对照实验）
    :param strict_logging: 记账失败是否让本次调用失败（默认 ``True``）
    """
    active_store = store or get_store()
    active_router = router or get_router()
    client = transport or OpenAICompatibleClient(timeout=timeout or 60.0)
    msg_list = _normalize_messages(messages)

    chain = await _resolve_chain(active_router, model_ref, stage, project_id, allow_fallback)
    replay_mode = is_replay_enabled(allow_replay)
    prompt_hash = compute_prompt_hash(
        model_ref=chain[0].model_ref,
        messages=msg_list,
        temperature=temperature,
        max_tokens=max_tokens,
        json_schema=json_schema,
    )

    fallback_from: list[str] = []
    last_error: LLMError | None = None

    for index, model in enumerate(chain):
        fallback_note = None
        if index + 1 < len(chain):
            fallback_note = f";fallback_to={chain[index + 1].model_ref}"
        if index > 0:
            previous = chain[index - 1]
            await _events.emit_fallback(
                stage=stage,
                from_ref=previous.model_ref,
                to_ref=model.model_ref,
                reason=(last_error.kind if last_error else "unknown"),
                error_kind=last_error.kind if last_error else None,
                project_id=project_id,
            )
        try:
            if replay_mode:
                return await _replay_call(
                    store=active_store,
                    model=model,
                    prompt_hash=prompt_hash,
                    json_schema=json_schema,
                    stage=stage,
                    purpose=purpose,
                    project_id=project_id,
                    strict_logging=strict_logging,
                    fallback_from=fallback_from,
                )
            return await _live_call(
                client=client,
                store=active_store,
                model=model,
                messages=msg_list,
                prompt_hash=prompt_hash,
                json_schema=json_schema,
                temperature=temperature,
                max_tokens=max_tokens,
                json_retry=json_retry,
                stage=stage,
                purpose=purpose,
                project_id=project_id,
                strict_logging=strict_logging,
                fallback_note=fallback_note,
                fallback_from=fallback_from,
                metadata=metadata,
            )
        except (ReplayMissError, LLMJSONValidationError, ModelRoutingError, IsolationViolation):
            # 配置/契约类错误不降级：换模型只会掩盖问题
            raise
        except LLMError as exc:
            last_error = exc
            if not allow_fallback or index == len(chain) - 1:
                await _events.emit_event(
                    "llm_error",
                    {
                        "stage": stage,
                        "purpose": purpose,
                        "model_ref": model.model_ref,
                        "error_kind": exc.kind,
                        "message": exc.message,
                        "project_id": project_id,
                    },
                )
                raise
            fallback_from.append(model.model_ref)
            logger.warning(
                "环节 %s 的模型 %s 调用失败（%s），按降级链切换到 %s",
                stage,
                model.model_ref,
                exc.kind,
                chain[index + 1].model_ref,
            )

    raise last_error or LLMError("LLM 调用失败：降级链已耗尽", detail={"stage": stage})


async def chat_json(
    messages: list[Message] | str,
    json_schema: dict[str, Any],
    model_ref: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> LLMResult:
    """结构化输出的便捷入口（等价于 ``chat(..., json_schema=...)``）。"""
    return await chat(
        messages,
        model_ref,
        temperature,
        max_tokens,
        json_schema,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 实时调用
# --------------------------------------------------------------------------- #
async def _live_call(
    *,
    client: OpenAICompatibleClient,
    store: LLMStore,
    model: ResolvedModel,
    messages: list[Message],
    prompt_hash: str,
    json_schema: dict[str, Any] | None,
    temperature: float | None,
    max_tokens: int | None,
    json_retry: int | None,
    stage: str | None,
    purpose: str | None,
    project_id: int | None,
    strict_logging: bool,
    fallback_note: str | None,
    fallback_from: list[str],
    metadata: dict[str, Any] | None = None,
) -> LLMResult:
    capability = detect_capability(provider=model.provider, base_url=model.base_url)
    strategy = json_strategy(capability, provider_key=model.provider) if json_schema else "none"
    max_attempts = 1 if json_schema is None else json_retry_limit(json_retry) + 1

    raw_responses: list[str] = []
    all_attempts: list[AttemptRecord] = []
    started_total = time.perf_counter()

    for attempt in range(1, max_attempts + 1):
        resolved_temperature = (
            temperature if temperature is not None else (model.temperature or DEFAULT_TEMPERATURE)
        )
        resolved_max_tokens = max_tokens or model.max_tokens or DEFAULT_MAX_TOKENS

        # 循环变量通过默认参数在定义时绑定，避免闭包捕获后续迭代的值（B023）。
        def _builder(
            current_strategy: str,
            _resolved_temperature: float = resolved_temperature,
            _resolved_max_tokens: int = resolved_max_tokens,
        ) -> tuple[dict[str, Any], list[Message]]:
            return _build_payload(
                model=model,
                messages=messages,
                temperature=_resolved_temperature,
                max_tokens=_resolved_max_tokens,
                json_schema=json_schema,
                strategy=current_strategy,
                stage=stage,
                purpose=purpose,
            )

        try:
            outcome = await _call_with_strategy_fallback(
                client=client,
                store=store,
                model=model,
                payload_builder=_builder,
                json_schema=json_schema,
                strategy=strategy,
                stage=stage,
                purpose=purpose,
                project_id=project_id,
                fallback_note=fallback_note,
                strict_logging=strict_logging,
            )
        except LLMError as exc:
            strategy = getattr(exc, "sciloop_strategy", strategy)
            all_attempts.extend(exc.attempts)
            await _log_attempts(
                store=store,
                model=model,
                attempts=exc.attempts,
                stage=stage,
                purpose=purpose,
                project_id=project_id,
                success=False,
                is_replay=False,
                cost_unknown_reason=None,
                error_suffix=exc.kind,
                strict_logging=strict_logging,
                extra_note=fallback_note,
            )
            raise

        all_attempts.extend(outcome.attempts)
        validation_error: str | None = None
        parsed: Any = None
        if json_schema is not None:
            ok, parsed, validation_error = parse_and_validate(outcome.content, json_schema)
            if not ok:
                raw_responses.append(outcome.content)

        success = validation_error is None
        error_text = None
        if not success:
            error_text = (
                f"json_schema_validation_failed(attempt={attempt}/{max_attempts}): {validation_error}"
            )
            if fallback_note:
                error_text += fallback_note

        await _log_attempts(
            store=store,
            model=model,
            attempts=outcome.attempts,
            stage=stage,
            purpose=purpose,
            project_id=project_id,
            success=success,
            is_replay=False,
            cost_unknown_reason=None,
            error_suffix=error_text,
            strict_logging=strict_logging,
            usage=outcome.usage,
        )

        if success:
            duration_ms = int((time.perf_counter() - started_total) * 1000)
            cost, cost_reason = compute_cost_usd(
                prompt_tokens=outcome.usage.prompt_tokens,
                completion_tokens=outcome.usage.completion_tokens,
                input_price=model.input_price,
                output_price=model.output_price,
                price_unit=model.price_unit,
            )
            if cost_reason:
                logger.warning(
                    "环节 %s 模型 %s 单价缺失，cost_usd 记为 null（禁止估算）：%s",
                    stage,
                    model.model_ref,
                    cost_reason,
                )
            return LLMResult(
                content=outcome.content,
                model_ref=model.model_ref,
                provider=model.provider,
                model_id=model.model_id,
                usage=outcome.usage,
                cost_usd=cost,
                cost_unknown_reason=cost_reason,
                duration_ms=duration_ms,
                attempts=attempt,
                http_calls=len(all_attempts),
                is_replay=False,
                finish_reason=outcome.finish_reason,
                parsed=parsed,
                stage=stage,
                purpose=purpose,
                prompt_hash=prompt_hash,
                resolved_from=model.source,
                fallback_from=list(fallback_from),
                raw={"strategy": strategy, "metadata": dict(metadata or {})} if metadata else {"strategy": strategy},
            )

        # 校验失败：记录原始响应后重试（最后一次失败则抛出结构化异常）
        logger.warning(
            "环节 %s 模型 %s 结构化输出校验失败（第 %s/%s 次）：%s",
            stage,
            model.model_ref,
            attempt,
            max_attempts,
            validation_error,
        )
        if attempt >= max_attempts:
            raise LLMJSONValidationError(
                f"结构化输出校验连续失败 {max_attempts} 次（模型 {model.model_ref}）：{validation_error}",
                attempts=all_attempts,
                validation_error=validation_error,
                raw_responses=raw_responses,
                provider=model.provider,
                model=model.model_id,
                detail={"stage": stage, "purpose": purpose, "json_schema": json_schema},
            )

    raise LLMError("结构化输出重试逻辑异常终止", provider=model.provider, model=model.model_id)


# --------------------------------------------------------------------------- #
# 回放调用
# --------------------------------------------------------------------------- #
async def _replay_call(
    *,
    store: LLMStore,
    model: ResolvedModel,
    prompt_hash: str,
    json_schema: dict[str, Any] | None,
    stage: str | None,
    purpose: str | None,
    project_id: int | None,
    strict_logging: bool,
    fallback_from: list[str],
) -> LLMResult:
    started = time.perf_counter()
    hit = await replay_or_raise(
        prompt_hash, store=store, model_ref=model.model_ref, stage=stage, purpose=purpose
    )
    duration_ms = int((time.perf_counter() - started) * 1000)

    parsed: Any = None
    validation_error: str | None = None
    if json_schema is not None:
        ok, parsed, validation_error = parse_and_validate(hit.content, json_schema)

    cost, cost_reason = compute_cost_usd(
        prompt_tokens=hit.usage.prompt_tokens,
        completion_tokens=hit.usage.completion_tokens,
        input_price=model.input_price,
        output_price=model.output_price,
        price_unit=model.price_unit,
    )
    if cost_reason:
        cost_reason = f"replay_counterfactual:{cost_reason}"

    error_text = None
    if validation_error:
        error_text = f"replay_fixture_schema_invalid: {validation_error}"

    await _log_attempts(
        store=store,
        model=model,
        attempts=[
            AttemptRecord(
                index=1,
                duration_ms=duration_ms,
                prompt_tokens=hit.usage.prompt_tokens,
                completion_tokens=hit.usage.completion_tokens,
            )
        ],
        stage=stage,
        purpose=purpose,
        project_id=project_id,
        success=validation_error is None,
        is_replay=True,
        cost_unknown_reason=cost_reason,
        error_suffix=error_text,
        strict_logging=strict_logging,
        usage=hit.usage,
        forced_cost=cost,
    )

    if validation_error:
        raise LLMJSONValidationError(
            f"回放 fixture 内容不符合 json_schema（prompt_hash={prompt_hash}）：{validation_error}",
            attempts=[],
            validation_error=validation_error,
            raw_responses=[hit.content],
            provider=model.provider,
            model=model.model_id,
            detail={"prompt_hash": prompt_hash, "is_replay": True},
        )

    return hit.to_result(
        model_ref=model.model_ref,
        provider=model.provider,
        model_id=model.model_id,
        cost_usd=cost,
        cost_unknown_reason=cost_reason,
        duration_ms=duration_ms,
        stage=stage,
        purpose=purpose,
        parsed=parsed,
    )


# --------------------------------------------------------------------------- #
# 组装与记账
# --------------------------------------------------------------------------- #
def _build_payload(
    *,
    model: ResolvedModel,
    messages: list[Message],
    temperature: float,
    max_tokens: int,
    json_schema: dict[str, Any] | None,
    strategy: str,
    stage: str | None,
    purpose: str | None,
) -> tuple[dict[str, Any], list[Message]]:
    effective_messages = messages
    payload: dict[str, Any] = {
        "model": model.model_id,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_schema is not None:
        if strategy == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _schema_name(stage, purpose),
                    "schema": json_schema,
                    "strict": True,
                },
            }
        elif strategy == "json_object":
            payload["response_format"] = {"type": "json_object"}
            effective_messages = with_schema_hint(messages, json_schema)
        else:  # prompt_only
            effective_messages = with_schema_hint(messages, json_schema)
    payload["messages"] = effective_messages
    return payload, effective_messages


def _schema_name(stage: str | None, purpose: str | None) -> str:
    raw = f"sciloop_{stage or 'generic'}_{purpose or 'response'}"
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in raw)
    return cleaned[:60]


def _is_schema_rejection(exc: LLMBadRequestError) -> bool:
    detail = exc.detail if isinstance(exc.detail, str) else ""
    return looks_like_schema_rejection(exc.status_code or 400, f"{exc.message} {detail}")


async def _call_with_strategy_fallback(
    *,
    client: OpenAICompatibleClient,
    store: LLMStore,
    model: ResolvedModel,
    payload_builder: Any,
    json_schema: dict[str, Any] | None,
    strategy: str,
    stage: str | None,
    purpose: str | None,
    project_id: int | None,
    fallback_note: str | None,
    strict_logging: bool,
) -> Any:
    """发一次请求；若上游拒绝 ``response_format`` 档位则自动降档并重发。

    降档属于「协议适配」而不是「重试」：它**不消耗** ``LLM_JSON_RETRY`` 的次数，
    因为不是模型的输出不合格，而是供应商不支持该参数。
    """
    current = strategy
    while True:
        payload, _ = payload_builder(current)
        try:
            return await client.chat_completions(model=model, payload=payload)
        except LLMBadRequestError as exc:
            if json_schema is None or current == "prompt_only" or not _is_schema_rejection(exc):
                raise
            downgraded = note_strategy_rejected(model.provider, current)
            logger.warning(
                "供应商 %s 拒绝 %s 档结构化输出，自动降档为 %s",
                model.provider,
                current,
                downgraded,
            )
            await _log_attempts(
                store=store,
                model=model,
                attempts=exc.attempts,
                stage=stage,
                purpose=purpose,
                project_id=project_id,
                success=False,
                is_replay=False,
                cost_unknown_reason=None,
                error_suffix=f"schema_strategy_rejected:{current}",
                strict_logging=strict_logging,
                extra_note=fallback_note,
            )
            exc.attempts = []
            current = downgraded


async def _log_attempts(
    *,
    store: LLMStore,
    model: ResolvedModel,
    attempts: list[AttemptRecord],
    stage: str | None,
    purpose: str | None,
    project_id: int | None,
    success: bool,
    is_replay: bool,
    cost_unknown_reason: str | None,
    error_suffix: str | None,
    strict_logging: bool,
    usage: Usage | None = None,
    forced_cost: float | None = None,
    extra_note: str | None = None,
) -> None:
    """把每一次 HTTP 尝试写成一行 ``llm_call_logs``。"""
    for attempt in attempts:
        prompt_tokens = attempt.prompt_tokens if attempt.prompt_tokens is not None else (usage.prompt_tokens if usage else None)
        completion_tokens = (
            attempt.completion_tokens
            if attempt.completion_tokens is not None
            else (usage.completion_tokens if usage else None)
        )
        cost = forced_cost
        if cost is None:
            cost, reason = compute_cost_usd(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                input_price=model.input_price,
                output_price=model.output_price,
                price_unit=model.price_unit,
            )
            if reason and cost_unknown_reason is None:
                cost_unknown_reason = reason

        error_parts: list[str] = []
        if not attempt.ok and attempt.error_kind:
            text = f"{attempt.error_kind}: {attempt.error_message or ''}".strip().rstrip(":")
            error_parts.append(text)
        if error_suffix and error_suffix.strip("; ") not in {"", attempt.error_kind}:
            error_parts.append(error_suffix.strip("; "))
        if extra_note:
            error_parts.append(extra_note.strip("; "))
        if cost_unknown_reason:
            error_parts.append(PRICE_MISSING_WARNING if not cost_unknown_reason.startswith("replay") else cost_unknown_reason)
            logger.warning(
                "单价缺失，cost_usd=null（禁止估算）model=%s reason=%s", model.model_ref, cost_unknown_reason
            )
        if not success and not error_parts:
            error_parts.append("call_failed")

        entry = CallLogEntry(
            project_id=project_id,
            stage=stage,
            provider=model.provider,
            model=model.model_id,
            purpose=purpose,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            duration_ms=attempt.duration_ms,
            success=bool(success and attempt.ok),
            is_replay=is_replay,
            error="; ".join(part for part in error_parts if part) or None,
            model_ref=model.model_ref,
        )
        try:
            await store.insert_call_log(entry)
        except Exception as exc:  # noqa: BLE001
            if strict_logging:
                raise
            logger.error("llm_call_logs 写入失败（strict_logging=False，已忽略）：%s", exc)


async def _resolve_chain(
    router: ModelRouter,
    model_ref: str | None,
    stage: str | None,
    project_id: int | None,
    allow_fallback: bool,
) -> list[ResolvedModel]:
    if model_ref:
        primary = await router.resolve_explicit(model_ref, stage=stage, project_id=project_id)
        chain = [primary]
        if allow_fallback:
            for extra in await router.resolve_chain(stage or "decide", project_id, exclude_refs={primary.model_ref}):
                if all(item.model_ref != extra.model_ref for item in chain):
                    chain.append(extra)
        return chain
    if not stage:
        raise ModelRoutingError("chat() 需要 stage 或显式 model_ref 之一，否则无法确定模型")
    chain = await router.resolve_chain(stage, project_id)
    if not allow_fallback:
        chain = chain[:1]
    if not chain:
        raise ModelRoutingError(
            f"环节 {stage} 无可用模型（既无路由配置，环境变量兜底也不完整）",
            detail={"stage": stage, "project_id": project_id},
        )
    return chain


def _normalize_messages(messages: list[Message] | str) -> list[Message]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if not messages:
        raise ValueError("messages 不能为空")
    normalized: list[Message] = []
    for message in messages:
        if isinstance(message, str):
            normalized.append({"role": "user", "content": message})
        elif isinstance(message, dict):
            normalized.append(
                {
                    "role": str(message.get("role", "user")),
                    "content": message.get("content", ""),
                    **({"name": message["name"]} if message.get("name") else {}),
                }
            )
        else:
            raise ValueError(f"messages 元素必须是 dict 或 str，收到 {type(message).__name__}")
    return normalized


__all__ = ["DEFAULT_MAX_TOKENS", "DEFAULT_TEMPERATURE", "chat", "chat_json"]
