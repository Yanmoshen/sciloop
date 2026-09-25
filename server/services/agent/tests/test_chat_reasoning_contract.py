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


def test_reasoning_is_emitted_as_a_row_before_the_tool_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """每轮思考要**落成一行**，而且必须排在这一轮的过程行之前。

    背景（研究者 2026-09-24 要求）：以前整个回合的思考只存在 turn.reasoning 里，
    界面只能把它整段堆在正文最上方；落成行之后前端才能把它和过程行**交叉展示**。
    顺序错了（思考跑到自己的动作后面）读起来就是"先干活后想"。
    """

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

    reasoning_rows = [e for e in events if '"kind": "reasoning"' in e or '"kind":"reasoning"' in e]
    assert reasoning_rows, "应当把本轮思考作为一行发出去（kind=reasoning）"
    assert "先看目录" in reasoning_rows[0]

    # 顺序：思考行必须出现在这一轮的工具行之前
    idx_reason = next(i for i, e in enumerate(events) if "reasoning" in e and "row" in e)
    idx_tool = next(
        (i for i, e in enumerate(events) if "run_command" in e and "row" in e),
        len(events),
    )
    assert idx_reason < idx_tool, "思考行必须排在本轮过程行之前，否则读起来像先干活后想"
