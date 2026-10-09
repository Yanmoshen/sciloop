"""模型循环验收（对应验收书 §5 模型循环验收 + §8 部分条目）。

- fake provider 的纯文本流可完成 Turn；
- 供应商错误被分类为可重试、上下文超限或不可恢复；
- TurnRuntime 不包含供应商专用协议分支（静态扫描 + 功能性证明）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import Harness, run

from contracts.agent_v2 import (
    ErrorClass,
    ItemType,
    ModelStreamKind,
    StopReason,
    StreamCompleted,
    TextDelta,
    TurnStatus,
    Usage,
    error_response,
    reasoning_response,
    text_response,
    tool_call_response,
)
from contracts.agent_v2.cancellation import CancelToken
from services.model_gateway_v2 import RetryPolicy


# ---------------------------------------------------------------------------- §5.1
def test_text_only_stream_completes_the_turn(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    outcome, provider, turn = h.play(thread.thread_id, "你好", h.text_script("你好，我是执行内核"))
    assert outcome.status is TurnStatus.COMPLETED
    assert turn.status is TurnStatus.COMPLETED
    assert outcome.stop_reason == StopReason.END_TURN.value
    # stop_reason 记在事件负载里（Turn 契约本身不含该字段，避免与事件重复存储）
    completed = [e for e in h.store_for(thread.thread_id).read_all() if e.type == "turn/completed"]
    assert completed and completed[0].payload["stop_reason"] == StopReason.END_TURN.value
    state = h.repo.state(thread.thread_id)
    assert state.assistant_text(turn.turn_id) == "你好，我是执行内核"
    assert provider.call_count == 1
    assert h.event_types(thread.thread_id, turn_id=turn.turn_id).count("model/delta") == 1
    h.assert_sequence_is_contiguous(thread.thread_id)


def test_reasoning_deltas_are_persisted_but_not_fed_back(tmp_path):
    from services.agent_runtime_v2 import build_context

    h = Harness.create(tmp_path)
    thread = h.thread()
    outcome, _, turn = h.play(
        thread.thread_id,
        "想一想",
        [reasoning_response("先看数据", "结论是 A")],
    )
    assert outcome.status is TurnStatus.COMPLETED
    state = h.repo.state(thread.thread_id)
    assert len(state.items_of_type(ItemType.REASONING)) == 1
    assert "model/reasoning_delta" in h.event_types(thread.thread_id)
    # 推理片段不进上下文
    context = build_context(state)
    assert all("先看数据" not in str(m.get("content", "")) for m in context)


# ---------------------------------------------------------------------------- §5.2
def test_tool_call_produces_start_output_completed_events_with_call_id(tmp_path):
    h = Harness.create(tmp_path)
    h.executor.results["read_file"] = ("succeeded", {"text": "文件内容"})
    thread = h.thread()
    script = [tool_call_response("read_file", {"path": "a.txt"}, call_id="call_a"), text_response("读到了")]
    outcome, provider, turn = h.play(thread.thread_id, "读文件", script)

    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.iterations == 2, "一次工具调用需要两轮模型交互"
    types = h.event_types(thread.thread_id, turn_id=turn.turn_id)
    for expected in ("model/tool_call_started", "model/tool_call_completed", "tool/started", "tool/completed"):
        assert expected in types, f"缺少 {expected}"

    state = h.repo.state(thread.thread_id)
    call = state.tool_calls["call_a"]
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}
    assert call.output == {"text": "文件内容"}
    results = [i for i in state.items_for_turn(turn.turn_id) if i.type is ItemType.TOOL_RESULT]
    assert results and results[0].call_id == "call_a", "工具结果必须按 call_id 配对"

    # 第二轮请求里必须带 tool 角色消息（结果已回喂模型）
    assert provider.call_count == 2
    second = provider.calls[1].messages
    tool_messages = [m for m in second if m.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["tool_call_id"] == "call_a"
    assert "文件内容" in tool_messages[0]["content"]


def test_usage_is_recorded_from_the_stream(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    script = [[TextDelta("好"), Usage(input_tokens=11, output_tokens=7), StreamCompleted(StopReason.END_TURN)]]
    outcome, _, _ = h.play(thread.thread_id, "统计用量", script)
    assert outcome.usage["input_tokens"] == 11
    assert outcome.usage["output_tokens"] == 7
    assert "model/usage" in h.event_types(thread.thread_id)


# ---------------------------------------------------------------------------- §5.3 错误分类
def test_retryable_error_is_retried_then_succeeds(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    script = [error_response(ErrorClass.RETRYABLE, "429 busy"), text_response("重试后成功")]
    turn = h.start(thread.thread_id, "重试", key="r")
    provider = h.provider(script)
    runtime = h.runtime(provider, retry=RetryPolicy(max_attempts=3, base_delay_s=0.01))
    outcome = run(runtime.run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.retries == 1
    assert provider.call_count == 2
    assert "model/retry_scheduled" in h.event_types(thread.thread_id, turn_id=turn.turn_id)
    # 重试的等待时间必须走注入的时钟（FakeClock 不真实等待）
    assert h.clock.slept, "退避等待应通过时钟注入"
    h.assert_no_lease_leak()


def test_invalid_request_and_fatal_are_not_retried(tmp_path):
    for error_class in (ErrorClass.INVALID_REQUEST, ErrorClass.FATAL):
        h = Harness.create(tmp_path / error_class.value)
        thread = h.thread()
        turn = h.start(thread.thread_id, "必然失败")
        provider = h.provider([error_response(error_class, "bad request")])
        outcome = run(h.runtime(provider, retry=RetryPolicy(max_attempts=5)).run(thread.thread_id, turn.turn_id))
        assert outcome.status is TurnStatus.FAILED
        assert provider.call_count == 1, f"{error_class} 不应被重试"
        state = h.repo.state(thread.thread_id)
        assert state.turns[turn.turn_id].status is TurnStatus.FAILED
        assert state.items_of_type(ItemType.ERROR), "失败必须落错误 Item"
        h.assert_no_lease_leak()


def test_retry_does_not_duplicate_already_emitted_text(tmp_path):
    """已经吐出文本后再报可重试错误：不得重试（否则输出会重复）。"""
    h = Harness.create(tmp_path)
    thread = h.thread()
    script = [
        [TextDelta("前半段"), error_response(ErrorClass.RETRYABLE, "mid-stream failure")[0]],
        text_response("不会被用到"),
    ]
    turn = h.start(thread.thread_id, "中途失败")
    provider = h.provider(script)
    outcome = run(h.runtime(provider, retry=RetryPolicy(max_attempts=3)).run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.FAILED
    assert provider.call_count == 1, "已产生输出后不得重试"
    h.assert_no_lease_leak()


def test_context_overflow_triggers_compaction_then_continues(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    script = [
        error_response(ErrorClass.CONTEXT_OVERFLOW, "context too long"),
        text_response("这是压缩摘要"),
        text_response("压缩后继续回答"),
    ]
    turn = h.start(thread.thread_id, "很长的请求")
    provider = h.provider(script)
    compaction = h.compaction(provider, token_budget=10)
    outcome = run(h.runtime(provider, compaction=compaction).run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.compactions == 1
    types = h.event_types(thread.thread_id, turn_id=turn.turn_id)
    assert "compaction/started" in types and "compaction/completed" in types
    assert "model/failed" in types
    h.assert_no_lease_leak()


def test_max_iterations_is_a_failure_not_a_hang(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.start(thread.thread_id, "无限工具")
    # 每一轮都请求工具，永不结束
    provider = h.provider([tool_call_response("read_file", {"p": "x"}, call_id="c1")])
    outcome = run(
        h.runtime(provider, max_iterations=3).run(thread.thread_id, turn.turn_id)
    )
    assert outcome.status is TurnStatus.FAILED
    assert outcome.error["code"] == "max_iterations"
    h.assert_no_lease_leak()


# ---------------------------------------------------------------------------- §5.4 供应商无关
VENDOR_TOKENS = (
    "openai",
    "anthropic",
    "claude",
    "gemini",
    "deepseek",
    "moonshot",
    "qwen",
    "zhipu",
    "glm-",
    "bedrock",
    "vertex",
    "mistral",
    "llama",
    "ollama",
    "azure",
    "chat_completions",
    "chat/completions",
)


def test_turn_runtime_has_no_vendor_specific_branches():
    """静态守卫：运行时/网关/上下文模块里不得出现供应商专有名词或协议路径。"""
    runtime_dir = Path(__file__).resolve().parents[3] / "services"
    targets = [
        runtime_dir / "agent_runtime_v2" / "runtime.py",
        runtime_dir / "agent_runtime_v2" / "context.py",
        runtime_dir / "agent_runtime_v2" / "tools.py",
        runtime_dir / "agent_events_v2" / "store.py",
        runtime_dir / "agent_threads_v2" / "repository.py",
    ]
    offenders = []
    for path in targets:
        text = path.read_text(encoding="utf-8").lower()
        for token in VENDOR_TOKENS:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, f"运行时不得出现供应商专有分支：{offenders}"


def test_runtime_is_provider_agnostic_in_practice(tmp_path):
    """功能性证明：换一个「名字很供应商」的 provider，运行时行为完全不变。

    供应商差异只体现在 provider 自己的异常翻译上，运行时不需要任何改动。
    """

    class VendorNamedProvider:
        """某个供应商的适配器（名字带专有名词，但对外只暴露统一协议）。"""

        def __init__(self, inner):
            self.inner = inner

        async def stream(self, request, cancel=None):
            async for item in self.inner.stream(request, cancel):
                yield item

    h = Harness.create(tmp_path)
    thread = h.thread()
    turn = h.start(thread.thread_id, "你好")
    inner = h.provider(h.text_script("统一协议回答"))
    runtime = h.runtime(VendorNamedProvider(inner))  # type: ignore[arg-type]
    outcome = run(runtime.run(thread.thread_id, turn.turn_id))
    assert outcome.status is TurnStatus.COMPLETED
    assert h.repo.state(thread.thread_id).assistant_text(turn.turn_id) == "统一协议回答"


def test_gateway_normalizes_unclassified_provider_exception(tmp_path):
    """provider 直接抛异常时，网关必须归一化成带分类的 StreamError。"""

    class ExplodingProvider:
        async def stream(self, request, cancel=None):
            raise ConnectionError("socket closed")
            yield  # pragma: no cover

    from contracts.agent_v2 import ModelRequest
    from services.model_gateway_v2 import ModelGateway

    h = Harness.create(tmp_path)
    gateway = ModelGateway(ExplodingProvider(), clock=h.clock)
    request = ModelRequest(request_id="r1", messages=[{"role": "user", "content": "hi"}])
    items = run(_collect(gateway.stream(request)))
    assert len(items) == 1
    assert items[0].kind == ModelStreamKind.ERROR.value
    assert items[0].error_class == ErrorClass.RETRYABLE.value


async def _collect(iterator):
    return [item async for item in iterator]


def test_gateway_passes_cancellation_through(tmp_path):
    from contracts.agent_v2 import ModelRequest
    from contracts.agent_v2.cancellation import CancelledError
    from services.model_gateway_v2 import ModelGateway

    h = Harness.create(tmp_path)
    token = CancelToken()
    token.cancel("stop")
    gateway = ModelGateway(h.provider(h.text_script("不会输出")), clock=h.clock)
    request = ModelRequest(request_id="r1", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(CancelledError):
        run(_collect(gateway.stream(request, token)))
