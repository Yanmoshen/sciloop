"""真实模型供应商适配器。

``RegisteredModelProvider`` 只负责把项目现有的 ``llm`` 流接口接到
``agent.v2`` 的供应商无关协议。路由、鉴权和 OpenAI 兼容协议仍由
``llm.router`` / ``llm.adapter`` 负责；这里不保存或记录 API key。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

from contracts.agent_v2.cancellation import CancelledError, CancelToken
from contracts.agent_v2.enums import ErrorClass, StopReason
from contracts.agent_v2.models import (
    ModelRequest,
    ReasoningDelta,
    StreamCompleted,
    StreamError,
    StreamItem,
    TextDelta,
    ToolCallCompleted,
    Usage,
)
from llm.adapter import StreamUpdate, chat_stream
from llm.errors import LLMError, from_exception
from llm.http_client import OpenAICompatibleClient
from llm.router import ModelRouter, get_router


def classify_provider_error(exc: BaseException) -> ErrorClass:
    """将 ``llm`` 的错误分类映射到 agent.v2 的封闭枚举。"""
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    error = from_exception(exc)
    if isinstance(error, LLMError):
        if error.kind in {"timeout", "rate_limit", "server"}:
            return ErrorClass.RETRYABLE
        if error.kind == "bad_request":
            return ErrorClass.INVALID_REQUEST
    return ErrorClass.FATAL


class RegisteredModelProvider:
    """使用设置页/环境路由的真实模型供应商。

    ``model_ref`` 是默认模型（可为 ``None``，此时按 ``stage`` 路由）。
    单次请求可通过 ``request.model`` 或 ``request.metadata['model']`` 覆盖，
    后者用于线程/项目级模型选择。所有消息和工具声明原样传递给 OpenAI
    兼容适配器，因而支持工具调用后的 ``tool`` 角色续轮。
    """

    def __init__(
        self,
        model_ref: str | None = None,
        *,
        router: ModelRouter | None = None,
        transport: OpenAICompatibleClient | None = None,
        stage: str = "agent",
    ) -> None:
        self.model_ref = model_ref
        self.router = router
        self.transport = transport
        self.stage = stage
        self.calls: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def stream(
        self, request: ModelRequest, cancel: CancelToken | None = None
    ) -> AsyncIterator[StreamItem]:
        if not isinstance(request, ModelRequest):
            yield StreamError(
                error_class=ErrorClass.INVALID_REQUEST.value,
                message="request must be a ModelRequest",
            )
            return
        self.calls.append(request)
        cancel = cancel or CancelToken()
        cancel.raise_if_cancelled()

        metadata = getattr(request, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        selected_model = metadata.get("model") or request.model or self.model_ref
        stage = str(metadata.get("stage") or self.stage)
        project_id = metadata.get("project_id")
        purpose = metadata.get("purpose")
        params = dict(request.params or {})
        if selected_model is None:
            selected_model = params.get("model")
        temperature = params.pop("temperature", None)
        max_tokens = params.pop("max_tokens", None)
        timeout = params.pop("timeout", None)
        params.pop("model", None)

        tool_aliases: dict[str, str] = {}
        model_tools = [_model_tool_schema(tool, tool_aliases) for tool in request.tools or []]

        kwargs: dict[str, Any] = {
            "model_ref": selected_model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": model_tools,
            "stage": stage,
            "purpose": purpose,
            "project_id": project_id,
            "router": self.router or get_router(),
            "transport": self.transport,
            "allow_fallback": False,
            "metadata": params,
        }
        if timeout is not None:
            kwargs["timeout"] = float(timeout)

        updates = chat_stream(request.messages, **kwargs)
        completed = False
        failed = False
        try:
            while True:
                cancel.raise_if_cancelled()
                next_task = asyncio.create_task(updates.__anext__())
                loop = asyncio.get_running_loop()
                unsubscribe = cancel.subscribe(
                    lambda _reason, task=next_task, event_loop=loop: event_loop.call_soon_threadsafe(
                        task.cancel
                    )
                )
                try:
                    update = await next_task
                except StopAsyncIteration:
                    break
                except asyncio.CancelledError as exc:
                    if cancel.cancelled:
                        raise CancelledError(cancel.reason or "cancelled") from exc
                    raise
                finally:
                    unsubscribe()
                if not isinstance(update, StreamUpdate):
                    yield StreamError(
                        error_class=ErrorClass.FATAL.value,
                        message=f"llm adapter yielded {type(update).__name__}",
                    )
                    return
                if update.kind == "delta" and update.text:
                    yield TextDelta(update.text)
                elif update.kind == "reasoning" and update.text:
                    yield ReasoningDelta(update.text)
                elif update.kind == "done":
                    completed = True
                    result = update.result
                    if result is None:
                        yield StreamError(
                            error_class=ErrorClass.FATAL.value,
                            message="model stream completed without result",
                        )
                        return
                    usage = result.usage
                    if not usage.is_empty():
                        yield Usage(
                            input_tokens=usage.prompt_tokens or 0,
                            output_tokens=usage.completion_tokens or 0,
                        )
                    for call in result.tool_calls or []:
                        mapped = _tool_call(call, tool_aliases)
                        if mapped is None:
                            yield StreamError(
                                error_class=ErrorClass.INVALID_REQUEST.value,
                                message="model returned malformed tool call",
                            )
                            return
                        yield mapped
                    yield StreamCompleted(_stop_reason(result.finish_reason))
                    return
        except (CancelledError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary classification
            failed = True
            error = from_exception(exc)
            yield StreamError(
                error_class=classify_provider_error(error).value,
                message=_safe_message(error),
                retry_after_s=float(getattr(error, "retry_after_seconds", 0.0) or 0.0),
            )
        finally:
            with_context = getattr(updates, "aclose", None)
            if with_context is not None:
                try:
                    await with_context()
                except (asyncio.CancelledError, CancelledError):
                    raise
                except Exception:
                    pass
        if not completed and not failed:
            # A provider ending without a done frame is a protocol failure. Do
            # not let the runtime mistake a truncated response for success.
            yield StreamError(
                error_class=ErrorClass.FATAL.value,
                message="model stream ended without completion",
            )


def _model_tool_schema(tool: Any, aliases: dict[str, str]) -> dict[str, Any]:
    """Return a provider-safe tool schema and remember its internal name."""
    if not isinstance(tool, dict):
        return tool
    function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    original_name = str(function.get("name") or "")
    model_name = re.sub(r"[^A-Za-z0-9_-]", "_", original_name)
    model_name = model_name or "tool"
    # Keep aliases deterministic while avoiding collisions after sanitizing.
    candidate = model_name
    suffix = 2
    while candidate in aliases and aliases[candidate] != original_name:
        candidate = f"{model_name}_{suffix}"
        suffix += 1
    aliases[candidate] = original_name
    normalized_function = dict(function)
    normalized_function["name"] = candidate
    normalized = dict(tool)
    normalized["type"] = "function"
    normalized["function"] = normalized_function
    return normalized


def _tool_call(
    call: dict[str, Any], aliases: dict[str, str] | None = None
) -> ToolCallCompleted | None:
    if not isinstance(call, dict):
        return None
    call_id = str(call.get("id") or call.get("call_id") or "")
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    name = str(function.get("name") or "")
    if aliases:
        name = aliases.get(name, name)
    raw_args = function.get("arguments", {})
    if not call_id or not name:
        return None
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_args, dict):
        return None
    return ToolCallCompleted(call_id=call_id, name=name, arguments=raw_args)


def _stop_reason(value: Any) -> str:
    text = str(value or "end_turn")
    if text in {"tool_calls", "function_call", "tool_use"}:
        return StopReason.TOOL_USE.value
    if text in {"length", "max_tokens"}:
        return StopReason.MAX_TOKENS.value
    return StopReason.END_TURN.value


def _safe_message(exc: BaseException) -> str:
    message = str(exc) or type(exc).__name__
    return message[:500]


__all__ = ["RegisteredModelProvider", "classify_provider_error"]
