import asyncio
import json

import pytest

from services import home_turns


@pytest.fixture
def turn_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(home_turns.conversations, "conversations_dir", lambda: tmp_path)
    home_turns._owners.clear()
    yield tmp_path
    home_turns._owners.clear()


async def _frames(*frames):
    for frame in frames:
        yield frame
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_subscriber_detach_does_not_cancel_owner(turn_storage):
    release = asyncio.Event()

    async def source():
        yield 'event: delta\ndata: {"text":"a"}\n\n'
        await release.wait()
        yield 'event: done\ndata: {"duration_ms":3}\n\n'

    owner = home_turns.start("202610129901", source(), kind="chat", base_count=0)
    sub = home_turns.subscribe(owner.conversation_id, owner.turn_id)
    await anext(sub)
    await sub.aclose()
    assert home_turns.active(owner.conversation_id) is owner
    release.set()
    await owner.task
    assert owner.snapshot["status"] == "completed"


@pytest.mark.asyncio
async def test_subscriber_heartbeat_is_structured_and_nonterminal(turn_storage, monkeypatch):
    release = asyncio.Event()

    async def source():
        await release.wait()
        yield 'event: done\ndata: {"duration_ms":3}\n\n'

    owner = home_turns.start("202610129906", source(), kind="chat", base_count=0)
    await asyncio.sleep(0)

    async def timeout(_awaitable, timeout):
        assert timeout == 15
        _awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(home_turns.asyncio, "wait_for", timeout)
    subscriber = home_turns.subscribe(owner.conversation_id, owner.turn_id)
    frame = await anext(subscriber)
    assert frame.startswith("event: heartbeat\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["conversation_id"] == owner.conversation_id
    assert payload["turn_id"] == owner.turn_id
    assert payload["event_seq"] == 0
    assert payload["turn_status"] == "inProgress"
    assert payload["running_ms"] >= 0
    assert payload["last_event_at"]
    assert home_turns.active(owner.conversation_id) is owner
    await subscriber.aclose()
    release.set()
    await owner.task


@pytest.mark.asyncio
async def test_diagnostic_error_keeps_owner_until_interrupted_terminal(turn_storage):
    async def source():
        yield 'event: error\ndata: {"code":"research_subscription_timeout"}\n\n'
        yield 'event: done\ndata: {"node_status":"interrupted","duration_ms":4}\n\n'

    owner = home_turns.start("202610129902", source(), kind="research", base_count=0)
    await owner.task
    assert owner.snapshot["status"] == "interrupted"
    assert owner.snapshot["reason"] != "worker_failed"


@pytest.mark.asyncio
async def test_terminal_stamp_does_not_relabel_later_turn(turn_storage):
    record = {"id": "202610129903", "turns": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old"}]}
    home_turns.conversations.write(record)

    owner = home_turns.start(record["id"], _frames('event: done\ndata: {"duration_ms":1}\n\n'), kind="chat", base_count=2)
    await owner.task
    record = home_turns.conversations.read(record["id"])
    record["turns"].extend([{"role": "user", "content": "new"}, {"role": "assistant", "content": "new"}])
    home_turns.conversations.write(record)
    home_turns._stamp(owner, attach=False)
    record = home_turns.conversations.read(record["id"])
    assert [turn.get("turn_id") for turn in record["turns"][2:]] == [None, None]


@pytest.mark.asyncio
async def test_restart_snapshot_only_marks_matching_turn(turn_storage):
    record = {"id": "202610129904", "turns": [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old"}]}
    home_turns.conversations.write(record)
    owner = home_turns.start(record["id"], _frames(), kind="chat", base_count=2)
    path, _ = home_turns._paths(owner.conversation_id, owner.turn_id)
    owner.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner.task
    # Simulate a process restart by removing the in-memory owner and restoring
    # an inProgress snapshot; no unrelated history may be relabelled.
    home_turns._owners.clear()
    data = {"conversation_id": owner.conversation_id, "turn_id": owner.turn_id, "kind": "chat", "status": "inProgress", "event_seq": 0}
    path.write_text(__import__("json").dumps(data), encoding="utf-8")
    assert home_turns.snapshot(owner.conversation_id, owner.turn_id)["status"] == "failed"
    record = home_turns.conversations.read(record["id"])
    assert all(turn.get("turn_id") is None for turn in record["turns"])


@pytest.mark.asyncio
async def test_stamp_permission_error_keeps_owner_and_terminal(turn_storage, monkeypatch):
    original = home_turns._save
    calls = 0

    def fail_once(owner):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise home_turns.PersistenceError({"operation": "test.save", "path": "locked", "message": "locked"})
        return original(owner)

    monkeypatch.setattr(home_turns, "_save", fail_once)
    owner = home_turns.start(
        "202610129905",
        _frames('event: delta\ndata: {"text":"hello"}\n\n', 'event: done\ndata: {"duration_ms":1}\n\n'),
        kind="chat",
        base_count=0,
    )
    await owner.task
    assert owner.snapshot["status"] == "completed"
    assert owner.snapshot.get("persistence_warnings")
