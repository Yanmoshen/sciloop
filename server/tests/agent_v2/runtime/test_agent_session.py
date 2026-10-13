"""Regression coverage for the Codex-style session adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from api.v2.agent.service import AgentV2Service


@pytest.fixture()
def service(tmp_path: Path):
    value = AgentV2Service(
        conversations_root=tmp_path / "conversations",
        memories_root=tmp_path / "memories",
        poll_interval_s=0.01,
        heartbeat_s=0.05,
        token_budget=200,
    )
    yield value
    _run(value.aclose())


def _run(coro):
    return asyncio.run(coro)


def test_session_submit_is_idempotent_and_waitable(service) -> None:
    thread = service.repo.create_thread("session")

    async def scenario():
        first = await service.session.submit(
            thread.thread_id, "你好", idempotency_key="same-turn"
        )
        second = await service.session.submit(
            thread.thread_id, "你好", idempotency_key="same-turn"
        )
        outcome = await service.session.wait(thread.thread_id, timeout_s=5)
        return first, second, outcome

    first, second, outcome = _run(scenario())
    assert first.turn_id == second.turn_id
    assert first.created is True
    assert second.created is False
    assert outcome.status.value == "completed"
    state = service.repo.state(thread.thread_id)
    assert len(state.turn_order) == 1
    assert state.assistant_text(state.turn_order[0])


def test_session_context_and_snapshot_rebuild_without_model_call(service) -> None:
    thread = service.repo.create_thread("snapshot")

    async def scenario():
        turn = await service.session.submit(thread.thread_id, "保留这个事实", idempotency_key="t1")
        await service.session.wait(thread.thread_id, timeout_s=5)
        return turn

    turn = _run(scenario())
    info = service.session.context_info(thread.thread_id)
    snapshot = service.session.snapshot(thread.thread_id)
    assert info["messages"]
    assert any(item.get("content") == "保留这个事实" for item in info["messages"])
    assert snapshot["thread"]["thread_id"] == thread.thread_id
    assert snapshot["active_turn"] is None
    assert snapshot["running"] is False
    assert any(item["turn_id"] == turn.turn_id for item in snapshot["items"])


def test_session_failed_turn_can_be_retried_in_a_new_turn(service) -> None:
    thread = service.repo.create_thread("retry", settings={"scenario": "error_fatal"})

    async def scenario():
        first = await service.session.submit(thread.thread_id, "重试我", idempotency_key="fail")
        await service.session.wait(thread.thread_id, timeout_s=5)
        retry = await service.session.recover(thread.thread_id, first.turn_id)
        await service.session.wait(thread.thread_id, timeout_s=5)
        return first, retry

    first, retry = _run(scenario())
    assert retry.mode == "retry"
    assert retry.retried_from == first.turn_id
    assert retry.turn_id != first.turn_id
    assert len(service.repo.state(thread.thread_id).turn_order) == 2


def test_session_worker_read_error_is_converged_to_failed(service) -> None:
    """Transport errors from a host worker must not strand an active Turn."""
    thread = service.repo.create_thread("read error")

    class ReadError(Exception):
        pass

    async def scenario():
        async def boom():
            raise ReadError("upstream read failed")

        service.host.start = lambda _thread_id, _turn_id: asyncio.create_task(boom())  # type: ignore[method-assign]
        started = await service.session.submit(thread.thread_id, "触发读取失败")
        result = await service.session.wait(thread.thread_id, timeout_s=1)
        return started, result

    started, result = _run(scenario())
    assert result.status.value == "failed"
    state = service.repo.state(thread.thread_id)
    assert state.active_turn is None
    assert state.turn(started.turn_id).error["code"] == "model_stream_error"
