"""`_agent_loop` 回喂形状的回归测试（2026-09-22）。

背景（真实事故）：研究者点批准后，续答**第二次**要工具时整轮 400 中断 ——
`The reasoning_content in the thinking mode must be passed back to the API.`
真因是循环自己 append 的 `assistant.tool_calls` 消息**没带本轮 `reasoning_content`**：
批准路径手拼的那条消息补过该字段，但循环里这条漏了 —— 补丁只补了一半，
而报错指向"供应商/鉴权"，完全看不出是自家字段拼漏。

这里用假适配器把循环驱动起来，直接断言**回喂给模型的 messages 形状**：
带 `tool_calls` 的 assistant 消息必须带 `reasoning_content`，下一条 tool 结果必须严格配对 id。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from api.v1 import chat as chat_api


def test_partial_turn_checkpoint_survives_reentry_without_duplicates(monkeypatch):
    saved = []
    monkeypatch.setattr(chat_api.conversations, "write", lambda record: saved.append(dict(record)))
    record = {"turns": []}
    base = [{"role": "user", "content": "旧问题"}]
    state = {
        "agent_history": [{"role": "user", "content": "写论文"}],
        "text": ["正在整理实验结果"], "reasoning": [], "pending": None,
        "partial_text_from": 0,
    }
    kwargs = {"base_turns": base, "text": "写论文", "state": state, "rows": [], "approvals": [], "model_id": "model", "duration_ms": 20}
    chat_api._save_stream_progress(record, **kwargs)
    state["text"].append("，已有七次计时。")
    chat_api._save_stream_progress(record, **kwargs)
    assert len(record["turns"]) == 3
    assert record["turns"][-1]["content"] == "正在整理实验结果，已有七次计时。"
    assert record["agent_history"][-1]["content"] == record["turns"][-1]["content"]
    # A checkpoint is not a terminal connection failure. The durable owner
    # supplies the real reason only when it reaches interrupted/failed.
    assert record["turns"][-1]["interruption_reason"] is None
    assert record["turns"][-1]["interrupted"] is False
    assert len(saved) == 2


def test_stream_checkpoint_preserves_newer_user_access_choice(monkeypatch):
    monkeypatch.setattr(chat_api.conversations, "read", lambda _id: {"grants": {"full_access": True}})
    monkeypatch.setattr(chat_api.conversations, "write", lambda _record: None)
    record = {"id": "c1", "grants": {"full_access": False}}
    state = {"text": [], "reasoning": [], "pending": None, "agent_history": []}
    chat_api._save_stream_progress(
        record, base_turns=[], text="运行实验", state=state, rows=[], approvals=[], model_id="model", duration_ms=1,
    )
    assert record["grants"]["full_access"] is True


def test_stream_checkpoint_clears_closed_approval_marker(monkeypatch):
    """A decided approval must not leave the resumed turn waiting forever."""

    monkeypatch.setattr(chat_api.conversations, "write", lambda _record: None)
    record = {"id": "approval-marker", "turns": []}
    request = {"id": "req-1", "status": "approved", "consumed_at": "now"}
    state = {
        "text": ["命令已完成"],
        "reasoning": [],
        "pending": request,
        "agent_history": [],
    }
    chat_api._save_stream_progress(
        record,
        base_turns=[
            {
                "role": "assistant",
                "content": "",
                "approvals": [request],
                "awaiting_approval": request["id"],
            }
        ],
        text="继续",
        state=state,
        rows=[],
        approvals=[request],
        model_id="model",
        duration_ms=1,
    )
    assert record["turns"][-1]["awaiting_approval"] is None


class _FakeResult:
    def __init__(self, *, content: str, reasoning: str, calls: list[dict[str, Any]] | None) -> None:
        self.content = content
        self.duration_ms = 1
        self.model_id = "fake"
        self.usage = None
        self.tool_calls = calls or []
        self.raw = {"reasoning": reasoning}


class _FakeUpdate:
    def __init__(self, kind: str, text: str = "", result: Any = None) -> None:
        self.kind = kind
        self.text = text
        self.result = result


def _stream_factory(rounds: list[dict[str, Any]], seen: list[list[dict[str, Any]]]):
    """按轮次造流式更新；每轮先把当时收到的 messages 快照进 `seen`。"""

    async def _chat_stream(messages: list[dict[str, Any]], **_kwargs: Any):
        seen.append([dict(m) for m in messages])
        index = min(len(seen) - 1, len(rounds) - 1)
        spec = rounds[index]
        if spec.get("raise"):
            raise RuntimeError(str(spec["raise"]))
        for piece in spec.get("reasoning_deltas", []):
            yield _FakeUpdate(kind="reasoning", text=piece)
        if spec.get("content"):
            yield _FakeUpdate(kind="delta", text=spec["content"])
        yield _FakeUpdate(
            kind="done",
            result=_FakeResult(
                content=spec.get("content", ""),
                reasoning=spec.get("reasoning_raw", ""),
                calls=spec.get("calls"),
            ),
        )

    return _chat_stream


async def _drain(agen) -> None:
    async for _ in agen:  # pragma: no branch - 只为把循环跑完
        pass


def _run(monkeypatch: pytest.MonkeyPatch, rounds: list[dict[str, Any]]):
    seen: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(chat_api.adapter, "chat_stream", _stream_factory(rounds, seen))

    async def _no_tools() -> list[dict[str, Any]]:
        return []

    async def _no_judge(_call: dict[str, Any]) -> Any:  # 只读、无需批准
        class _Verdict:
            forbidden = False
            needs_approval = False
            harmless = True

        return _Verdict()

    async def _read_run(_call: dict[str, Any], **_kwargs: Any) -> tuple[dict[str, Any], str]:
        return {"ok": True, "result": "ok"}, "done"

    from services.agent import mcp_tools as agent_tools

    monkeypatch.setattr(agent_tools, "tool_schemas", _no_tools)
    monkeypatch.setattr(agent_tools, "judge", _no_judge)
    monkeypatch.setattr(agent_tools, "run_tool_call", _read_run)

    state: dict[str, Any] = {"text": [], "reasoning": [], "result": None, "pending": None}
    loop = chat_api._agent_loop(
        [{"role": "user", "content": "看一下当前目录"}],
        conversation_id="regression-test",
        ref="fake",
        tool_defs=[],
        rows=[],
        approvals_out=[],
        state=state,
    )
    asyncio.run(_drain(loop))
    return seen


def _call(name: str, call_id: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def test_tool_round_carries_reasoning_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """带 tool_calls 的 assistant 消息必须把**本轮**思考过程一起回喂。"""

    seen = _run(
        monkeypatch,
        [
            {
                "reasoning_deltas": ["先看看目录", "再说"],
                "content": "",
                "calls": [_call("run_command", "c1")],
            },
            {"reasoning_deltas": ["读完了"], "content": "当前目录是 /app/server"},
        ],
    )

    assert len(seen) >= 2, "循环应该被驱动到第二轮"
    second = seen[1]
    assistant_with_calls = [
        m for m in second if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_with_calls, "第二轮请求里必须有带 tool_calls 的 assistant 消息"
    assert assistant_with_calls[0].get("reasoning_content") == "先看看目录再说", (
        "缺 reasoning_content 会被思考型供应商直接 400（本次事故的真因）"
    )
    # tool 结果必须严格配对同一个 call id
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0].get("tool_call_id") == "c1"


def test_unclassified_provider_error_retries_inside_agent_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """第三方适配器直接抛异常时，也不能绕过重试把整轮打成中断。"""

    seen = _run(
        monkeypatch,
        [
            {"raise": "temporary transport failure"},
            {"raise": "temporary transport failure"},
            {"content": "实验已经完成，结果已整理。"},
        ],
    )

    assert len(seen) == 3


def test_tool_round_takes_only_this_rounds_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """只带**本轮**的思考过程，不能把前面几轮的累积起来一起塞回去。"""

    seen = _run(
        monkeypatch,
        [
            {"reasoning_deltas": ["第一轮的想法"], "calls": [_call("run_command", "c1")]},
            {"reasoning_deltas": ["第二轮的想法"], "calls": [_call("run_command", "c2")]},
            {"content": "好了"},
        ],
    )

    assert len(seen) >= 3
    third = seen[2]
    with_calls = [m for m in third if m.get("role") == "assistant" and m.get("tool_calls")]
    assert with_calls, "第三轮请求里应有第二轮那条 tool_calls 消息"
    assert with_calls[-1].get("reasoning_content") == "第二轮的想法"


def test_tool_round_falls_back_to_final_frame_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    """有的供应商只在收尾帧给思考过程（delta 为空）→ 必须从原始结果兜底取。"""

    seen = _run(
        monkeypatch,
        [
            {
                "reasoning_deltas": [],
                "reasoning_raw": "收尾帧才给的思考",
                "calls": [_call("run_command", "c1")],
            },
            {"content": "done"},
        ],
    )

    second = seen[1]
    with_calls = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
    assert with_calls and with_calls[0].get("reasoning_content") == "收尾帧才给的思考"


def test_reasoning_is_emitted_separately_from_tool_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """思考走独立 SSE 通道，工具 rows 只描述真实工具活动。"""

    seen: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(chat_api.adapter, "chat_stream", _stream_factory(
        [
            {"reasoning_deltas": ["先看目录"], "calls": [_call("run_command", "c1")]},
            {"content": "看完了"},
        ],
        seen,
    ))

    async def _no_tools() -> list[dict[str, Any]]:
        return []

    async def _no_judge(_call: dict[str, Any]) -> Any:
        class _Verdict:
            forbidden = False
            needs_approval = False
            harmless = True

        return _Verdict()

    async def _read_run(_call: dict[str, Any], **_kwargs: Any) -> tuple[dict[str, Any], str]:
        return {"ok": True, "result": "ok"}, "done"

    from services.agent import mcp_tools as agent_tools

    monkeypatch.setattr(agent_tools, "tool_schemas", _no_tools)
    monkeypatch.setattr(agent_tools, "judge", _no_judge)
    monkeypatch.setattr(agent_tools, "run_tool_call", _read_run)

    state: dict[str, Any] = {"text": [], "reasoning": [], "result": None, "pending": None}
    loop = chat_api._agent_loop(
        [{"role": "user", "content": "看一下当前目录"}],
        conversation_id="reasoning-row-test",
        ref="fake",
        tool_defs=[],
        rows=[],
        approvals_out=[],
        state=state,
    )

    events: list[str] = []

    async def _collect() -> None:
        async for chunk in loop:
            events.append(chunk)

    asyncio.run(_collect())

    reasoning_events = [e for e in events if "event: reasoning" in e]
    assert reasoning_events, "应当通过 reasoning SSE 事件发送思考"
    assert "先看目录" in reasoning_events[0]

    # rows 中只允许真实工具活动；思考不能伪装成工具调用。
    assert not any('"kind": "reasoning"' in e for e in events)
    tool_rows = [e for e in events if "run_command" in e and "event: row" in e]
    assert tool_rows
    assert all('"tool_name": "run_command"' in e for e in tool_rows)
    assert any('"tool_status": "running"' in e for e in tool_rows)
    assert any('"tool_status": "completed"' in e for e in tool_rows)


def test_soft_provider_failure_does_not_emit_backend_error_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recovery prose is handled by the caller; the loop must not leak a canned diagnostic."""

    async def _failed_stream(*_args: Any, **_kwargs: Any):
        raise RuntimeError("temporary provider failure")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_api.adapter, "chat_stream", _failed_stream)
    state: dict[str, Any] = {"text": [], "reasoning": [], "result": None, "pending": None}
    rows: list[dict[str, Any]] = []

    async def _collect() -> None:
        async for _ in chat_api._agent_loop(
            [{"role": "user", "content": "你好"}],
            conversation_id="soft-failure-test",
            ref="fake",
            tool_defs=[],
            rows=rows,
            approvals_out=[],
            state=state,
        ):
            pass

    asyncio.run(_collect())
    assert state.get("soft_failure")
    assert not any("本轮未拿到模型" in str(row.get("text")) for row in rows)
