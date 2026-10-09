"""EventStore 验收（对应验收书 §4 EventStore 验收）。

- 事件严格按序追加；
- 断线后使用游标可以补齐事件；
- 重复事件可识别，重复副作用不会由运行时再次提交；
- 模拟写入中断后，恢复逻辑不会静默丢失或跳过事件；
- 原始事件和快照文件位置符合计划书布局。
"""

from __future__ import annotations

import json

import pytest
from _helpers import Harness

from contracts.agent_v2 import (
    CONTRACT_VERSION,
    ContractViolation,
    CorruptedEventError,
    Event,
    EventType,
    new_id,
)


def test_events_are_appended_strictly_in_order(tmp_path):
    h = Harness.create(tmp_path)
    store = h.store_for(h.thread("顺序").thread_id)
    base = store.last_sequence()  # thread/created 已占用序号 1
    for i in range(5):
        store.emit(EventType.MODEL_DELTA, payload={"text": str(i)})
    events = store.read_all()
    assert [e.sequence for e in events][-5:] == [base + 1, base + 2, base + 3, base + 4, base + 5]
    h.assert_sequence_is_contiguous(events[0].thread_id)


def test_cursor_read_fills_the_gap_after_disconnect(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("游标")
    store = h.store_for(thread.thread_id)
    for i in range(4):
        store.emit(EventType.MODEL_DELTA, payload={"text": str(i)})

    cursor = store.last_sequence()  # 客户端已消费到这个序号
    for i in range(3):
        store.emit(EventType.MODEL_DELTA, payload={"text": f"new-{i}"})

    tail = store.read_cursor(cursor)
    assert [e.sequence for e in tail] == [cursor + 1, cursor + 2, cursor + 3]
    assert [e.payload["text"] for e in tail] == ["new-0", "new-1", "new-2"]
    # 按序号读取（含端点）
    assert [e.sequence for e in store.read_from(cursor + 2)] == [cursor + 2, cursor + 3]


def test_idempotent_append_returns_existing_event_without_duplicate_row(tmp_path):
    h = Harness.create(tmp_path)
    store = h.store_for(h.thread("幂等").thread_id)
    before = len(store.read_all())
    first = store.emit(EventType.TOOL_STARTED, payload={"name": "write_file"}, idempotency_key="k1")
    second = store.emit(EventType.TOOL_STARTED, payload={"name": "write_file"}, idempotency_key="k1")
    assert first.event_id == second.event_id
    assert first.sequence == second.sequence
    assert len(store.read_all()) == before + 1, "重复幂等键不得新增行"
    assert store.find_by_idempotency_key("k1").event_id == first.event_id


def test_broken_trailing_line_is_reported_not_skipped(tmp_path):
    """模拟写入中断：只留下不完整的最后一行。"""
    h = Harness.create(tmp_path)
    thread = h.thread("损坏")
    store = h.store_for(thread.thread_id)
    store.emit(EventType.MODEL_DELTA, payload={"text": "ok"})
    written = store.last_sequence()

    with open(store.events_path, "ab") as fh:
        fh.write(b'{"contract": "agent.v2.contract.v1", "event_id": "ev_trunc')

    with pytest.raises(CorruptedEventError) as exc:
        store.read_all()
    detail = exc.value.to_dict()
    assert detail["line_no"] == written + 1
    assert "truncated" in detail["reason"]
    assert detail["raw_prefix"]

    # 非严格模式：给出结构化损坏清单，且**不返回**损坏行之后的内容
    result = store.recover(strict=False)
    assert result.resumed_from == "corrupt"
    assert len(result.corruptions) == 1
    assert result.corruptions[0].line_no == written + 1


def test_sequence_gap_is_detected(tmp_path):
    """中间行被删 => 序号跳号，必须报错而不是静默跳过。"""
    h = Harness.create(tmp_path)
    store = h.store_for(h.thread("跳号").thread_id)
    for i in range(4):
        store.emit(EventType.MODEL_DELTA, payload={"text": str(i)})
    lines = store.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5  # thread/created + 4
    store.events_path.write_text("\n".join([lines[0], lines[1], lines[3]]) + "\n", encoding="utf-8")
    with pytest.raises(CorruptedEventError) as exc:
        store.read_all()
    assert "sequence gap" in exc.value.reason


def test_foreign_thread_event_is_rejected(tmp_path):
    h = Harness.create(tmp_path)
    store = h.store_for(h.thread("外来").thread_id)
    foreign = Event(
        event_id=new_id("event"),
        sequence=1,
        type=EventType.MODEL_DELTA,
        created_at=h.clock.now_iso(),
        thread_id=new_id("thread"),
        payload={},
    )
    with pytest.raises(ContractViolation):
        store.append(foreign)


def test_layout_matches_the_plan(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("布局")
    store = h.store_for(thread.thread_id)
    store.emit(EventType.MODEL_DELTA, payload={"text": "x"})
    seq = store.write_snapshot({"thread": thread.to_dict()})

    paths = store.paths()
    assert paths["events"].endswith("events.jsonl")
    assert paths["snapshots"].endswith("snapshots")
    assert paths["thread_dir"].endswith(h.repo.folder_of(thread.thread_id))
    assert store.events_path.is_file()
    assert (store.snapshots_dir / f"{seq}.json").is_file()


def test_snapshot_is_atomic_and_recover_combines_snapshot_with_tail(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("快照")
    store = h.store_for(thread.thread_id)
    for i in range(3):
        store.emit(EventType.MODEL_DELTA, payload={"text": str(i)})
    seq = store.write_snapshot({"marker": "state-at-3"})
    # 快照点之后继续追加
    for i in range(2):
        store.emit(EventType.MODEL_DELTA, payload={"text": f"tail-{i}"})

    result = store.recover()
    assert result.resumed_from == "snapshot+tail"
    assert result.snapshot_sequence == seq
    assert result.snapshot_state == {"marker": "state-at-3"}
    assert [e.sequence for e in result.events] == [seq + 1, seq + 2]

    # 快照文件内容本身是合法 JSON，且带契约版本
    payload = json.loads((store.snapshots_dir / f"{seq}.json").read_text(encoding="utf-8"))
    assert payload["contract"] == CONTRACT_VERSION
    assert payload["state"] == {"marker": "state-at-3"}


def test_rewrite_is_atomic_and_renumbers(tmp_path):
    h = Harness.create(tmp_path)
    store = h.store_for(h.thread("重写").thread_id)
    for i in range(3):
        store.emit(EventType.MODEL_DELTA, payload={"text": str(i)})
    kept = store.read_all()[1:]
    assert len(kept) == 3
    store.rewrite_atomic(kept)
    events = store.read_all()
    assert [e.sequence for e in events] == [1, 2, 3]
    assert [e.payload["text"] for e in events] == ["0", "1", "2"]


def test_lease_is_exclusive_and_released_by_marker_not_deletion(tmp_path):
    h = Harness.create(tmp_path)
    store = h.store_for(h.thread("租约").thread_id)
    lease = store.lease("turn", owner="a")
    assert lease.acquire() is True
    assert lease.acquire() is False, "同一租约不得被二次获取"
    assert lease.is_free() is False
    lease.release("done")
    assert lease.is_free() is True
    # 文件仍在（不靠删除释放），内容里带 released 标记
    assert lease.path.is_file()
    assert json.loads(lease.path.read_text(encoding="utf-8"))["released"] is True
    assert lease.acquire() is True
