"""Regression checks for model-owned provider-failure recovery."""

import asyncio

from api.v1 import chat


def test_fixed_social_and_failure_reply_helpers_are_removed() -> None:
    assert not hasattr(chat, "_fast_simple_reply")
    assert not hasattr(chat, "_local_failure_reply")


def test_home_turn_interrupt_targets_active_server_task() -> None:
    async def exercise() -> None:
        started = asyncio.Event()

        async def owner() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(owner())
        await started.wait()
        chat._ACTIVE_HOME_TURNS["turn-stop-test"] = (asyncio.Event(), task)
        try:
            assert chat.request_home_turn_interrupt("turn-stop-test")
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("active server turn must receive cancellation")
        finally:
            chat._ACTIVE_HOME_TURNS.pop("turn-stop-test", None)

    asyncio.run(exercise())
