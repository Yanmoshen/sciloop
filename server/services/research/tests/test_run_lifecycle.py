# Copyright 2026 SciLoop contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""研究节点编排层 · 生命周期测试（纯函数 + 生成器外壳，不需要数据库与网络）。

覆盖两件**会直接毁掉演示**的事，两件都在 2026-09-22 现场踩到过：

1. ``run_node`` 被取消（SSE 客户端断连）时，必须给那行 ``running`` 一个交代。
   否则库里留下永远 ``running`` 的僵尸行 —— 控制台的流光会一直转，看起来像还在跑。
   实测当时有两行：一行卡了 9 分钟、一行卡了 **15 小时**。
2. ``chain_state`` 读取时把「超时仍未收尾的 running」如实报成已中断，
   让界面回到可继续状态，而不是永远进行中（不改库，只改读取口径）。

风格与 ``services/agent/tests`` 一致：同步用例 + ``asyncio.run``，
不依赖 pytest-asyncio 的模式配置。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime

from llm.errors import LLMAuthError, LLMRateLimitError, LLMServerError, LLMTimeoutError
from services.research import orchestrator

UTC = datetime.UTC


# --------------------------------------------------------------------------- #
# 1) 读侧：超时未收尾的 running → 已中断
# --------------------------------------------------------------------------- #
def _row(**extra):
    base = {
        "node": "literature_review",
        "status": "running",
        "entry_index": 1,
        "started_at": datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC),
        "updated_at": datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC),
        "cost_usd": 0.0,
    }
    base.update(extra)
    return base


def test_stale_running_is_reported_as_interrupted() -> None:
    now = datetime.datetime(2026, 9, 22, 1, 30, tzinfo=UTC)  # 闲置 30 分钟
    out = orchestrator._interrupted_if_stale(_row(), now=now)

    assert out["status"] == "pending"
    assert out["interrupted"] is True
    assert out["validation"]["rules"] == ["interrupted"]
    # 面向研究者的话术必须说清「没有结论 + 可以重试」，而不是「模型没做好」
    message = out["validation"]["items"][0]["message"]
    assert "中断" in message and "重试" in message
    # 原行的证据字段不能被读取口径弄丢
    assert out["entry_index"] == 1
    assert out["updated_at"] == _row()["updated_at"]


def test_fresh_running_is_left_alone() -> None:
    now = datetime.datetime(2026, 9, 22, 1, 5, tzinfo=UTC)  # 只闲置 5 分钟
    row = _row(updated_at=datetime.datetime(2026, 9, 22, 1, 0, tzinfo=UTC))

    assert orchestrator._interrupted_if_stale(row, now=now) is row


def test_terminal_rows_are_never_rewritten() -> None:
    now = datetime.datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
    for status in ("done", "failed", "waiting_human", "blocked", "pending"):
        row = _row(status=status)
        assert orchestrator._interrupted_if_stale(row, now=now) is row


def test_naive_timestamp_from_legacy_row_does_not_crash() -> None:
    """历史行可能是 naive 时间戳（0009 之前）——按 UTC 解，不许抛异常。"""

    now = datetime.datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    row = _row(updated_at=datetime.datetime(2026, 9, 22, 1, 0), started_at=None)

    out = orchestrator._interrupted_if_stale(row, now=now)
    assert out["status"] == "pending"


def test_missing_timestamps_are_not_guessed() -> None:
    now = datetime.datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
    row = _row(updated_at=None, started_at=None)

    assert orchestrator._interrupted_if_stale(row, now=now) is row


def test_retry_budget_boundary_is_terminal() -> None:
    limit = orchestrator.graph.MAX_RETRY_PER_ENTRY
    assert not orchestrator._retry_exhausted(limit - 1, limit)
    assert orchestrator._retry_exhausted(limit, limit)
    assert orchestrator._retry_exhausted(limit + 1, limit)


def test_provider_retry_classification_is_separate_from_contract_repair() -> None:
    assert orchestrator._provider_retryable(LLMTimeoutError("read timeout"))
    assert orchestrator._provider_retryable(LLMRateLimitError("busy"))
    assert orchestrator._provider_retryable(LLMServerError("upstream 503"))
    assert not orchestrator._provider_retryable(LLMAuthError("invalid key"))
    assert not orchestrator._provider_retryable(ValueError("invalid JSON contract"))


def test_validation_dict_accepts_serialized_retry_record() -> None:
    payload = {"ok": False, "retryable": True, "retry_scheduled": True}
    assert orchestrator._validation_dict(payload) == payload
    result = orchestrator.ValidationResult(ok=False, level="L1", hits=[])
    assert orchestrator._validation_dict(result)["ok"] is False


def test_exhausted_llm_failure_persists_finished_terminal_row(monkeypatch) -> None:
    calls: list[dict] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def record(_session, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(orchestrator.store, "upsert_node_run", record)

    asyncio.run(
        orchestrator._settle_llm_failure(
            Session,
            conversation_id="retry-exhausted",
            project_id=None,
            node="literature_review",
            entry_index=1,
            retry_count=orchestrator.graph.MAX_RETRY_PER_ENTRY,
            validation={"ok": False, "terminal": True},
            raw_output="",
            llm_calls=2,
            total_cost=0,
        )
    )

    assert len(calls) == 1
    assert calls[0]["status"] == "failed"
    assert calls[0]["finished_at"] is not None
    assert calls[0]["retry_count"] == orchestrator.graph.MAX_RETRY_PER_ENTRY


# --------------------------------------------------------------------------- #
# 2) 写侧：被取消 / 异常时必须收尾
# --------------------------------------------------------------------------- #
def _source(script):
    """造一个假的 ``_run_node_events``：按脚本产事件，遇到 ``__raise__`` 就抛。"""

    async def _gen(session_factory, *, conversation_id, node=None, text="", project_id=None):
        for event, data in script:
            if event == "__raise__":
                raise data
            yield event, data

    return _gen


def _drain():
    """跑一遍 run_node，返回事件名序列（异常照样往外抛）。"""

    async def _run():
        names = []
        async for event, _data in orchestrator.run_node(None, conversation_id="conv-test"):
            names.append(event)
        return names

    return _run()


def _with_settle_recording(monkeypatch, script):
    calls: list[dict] = []

    async def fake_settle(session_factory, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(orchestrator, "_settle_interrupted", fake_settle)
    monkeypatch.setattr(orchestrator, "_run_node_events", _source(script))
    return calls


def test_cancel_after_entering_node_settles_the_row(monkeypatch) -> None:
    calls = _with_settle_recording(
        monkeypatch,
        [
            ("meta", {"node": "literature_review"}),
            ("node", {"node": "literature_review", "entry_index": 1}),
            ("__raise__", asyncio.CancelledError()),
        ],
    )

    try:
        asyncio.run(_drain())
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - 取消必须原样抛出，不许被吞掉
        raise AssertionError("CancelledError 必须继续往外抛")

    assert len(calls) == 1
    assert calls[0]["conversation_id"] == "conv-test"
    assert calls[0]["node"] == "literature_review"
    assert calls[0]["entry_index"] == 1


def test_unexpected_error_after_entering_node_settles_the_row(monkeypatch) -> None:
    calls = _with_settle_recording(
        monkeypatch,
        [
            ("node", {"node": "idea_and_feasibility", "entry_index": 2}),
            ("__raise__", RuntimeError("boom")),
        ],
    )

    try:
        asyncio.run(_drain())
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("原始异常必须继续往外抛")

    assert len(calls) == 1
    assert calls[0]["node"] == "idea_and_feasibility"
    assert calls[0]["entry_index"] == 2


def test_done_event_means_no_extra_settle(monkeypatch) -> None:
    """正常收尾过（``done``）的行不许被兜底逻辑覆盖。"""

    calls = _with_settle_recording(
        monkeypatch,
        [
            ("node", {"node": "literature_review", "entry_index": 1}),
            ("done", {"status": "done"}),
            ("__raise__", asyncio.CancelledError()),
        ],
    )

    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(_drain())

    assert calls == []


def test_error_event_means_no_extra_settle(monkeypatch) -> None:
    """各失败分支在发 ``error`` 之前都已把行落成终态，不要再写一次。"""

    calls = _with_settle_recording(
        monkeypatch,
        [
            ("node", {"node": "paper_writing", "entry_index": 1}),
            ("error", {"code": "llm_failed"}),
            ("__raise__", RuntimeError("after error")),
        ],
    )

    with contextlib.suppress(RuntimeError):
        asyncio.run(_drain())

    assert calls == []


def test_retryable_error_then_waiting_done_is_forwarded_without_settle(monkeypatch) -> None:
    calls = _with_settle_recording(
        monkeypatch,
        [
            ("node", {"node": "literature_review", "entry_index": 1}),
            ("error", {"code": "read_timeout", "retryable": True, "retry_scheduled": True}),
            ("waiting_human", {"node": "literature_review", "reason": "provider_retry_scheduled"}),
            ("done", {"node": "literature_review", "status": "waiting_human"}),
        ],
    )
    assert asyncio.run(_drain()) == ["node", "error", "waiting_human", "done"]
    assert calls == []


def test_failure_before_any_node_is_not_settled(monkeypatch) -> None:
    """还没进入任何节点（连行都没建）时不该去写库。"""

    calls = _with_settle_recording(
        monkeypatch,
        [
            ("meta", {"node": "literature_review"}),
            ("error", {"code": "unknown_node"}),
        ],
    )

    assert asyncio.run(_drain()) == ["meta", "error"]
    assert calls == []


def test_events_pass_through_unchanged(monkeypatch) -> None:
    """外壳不许篡改事件流——前端/对话层都按原事件记流水。"""

    calls = _with_settle_recording(
        monkeypatch,
        [
            ("meta", {"node": "literature_review"}),
            ("node", {"node": "literature_review", "entry_index": 1}),
            ("attempt", {"attempt": 1}),
            ("validation", {"ok": False}),
            ("done", {"status": "waiting_human"}),
        ],
    )

    assert asyncio.run(_drain()) == [
        "meta",
        "node",
        "attempt",
        "validation",
        "done",
    ]
    assert calls == []


def test_provider_stall_is_bounded_and_emits_terminal_done(monkeypatch) -> None:
    """A stalled provider must not leave the UI in a permanent running state."""
    calls: list[dict] = []

    async def fake_settle(session_factory, **kwargs):
        calls.append(kwargs)

    async def stalled_source(session_factory, *, conversation_id, node=None, text="", project_id=None):
        yield "node", {"node": "literature_review", "entry_index": 1}
        await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator, "_settle_interrupted", fake_settle)
    monkeypatch.setattr(orchestrator, "_run_node_events", stalled_source)

    async def drain():
        names = []
        async for event, _data in orchestrator.run_node(
            None,
            conversation_id="conv-timeout",
            idle_timeout_s=0.01,
            wall_timeout_s=0.02,
        ):
            names.append(event)
        return names

    names = asyncio.run(drain())
    assert names[-3:] == ["notice", "waiting_human", "done"]
    assert calls and calls[0]["node"] == "literature_review"
