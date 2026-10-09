"""记忆存储验收（计划书 WP-07 + 验收书 §7.4 后半）。

- 三类记忆目录隔离（user / projects / threads）；
- append-only records.jsonl：每次写入追加一行信封，历史可审计；
- 记录带来源事件、创建者、版本、时间、置信度、内容哈希、删除状态；
- 用户编辑不被静默覆盖；
- 删除是可审计 tombstone（历史行保留）；
- 模型只接收经过作用域和长度过滤的记忆。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import Harness

from contracts.agent_v2 import (
    MemoryOrigin,
    MemoryOverwriteDenied,
    new_id,
)
from services.agent_memory_v2 import RECORDS_FILENAME, MemoryEnvelope, MemoryStore


def _event_id() -> str:
    return new_id("event")


# ---------------------------------------------------------------------------- 布局
def test_scope_directories_follow_the_plan(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    layout = store.layout()
    assert layout["user"].endswith("user")
    assert layout["project"].endswith("projects")
    assert layout["conversation"].endswith("threads")

    store.write("user", "u1", "偏好中文", origin="user", source_event_ids=[_event_id()])
    store.write("project", "p1", "约定：只读优先", source_event_ids=[_event_id()])
    store.write("conversation", "th_x", "本对话在做论文统计", source_event_ids=[_event_id()])

    assert (tmp_path / "memories" / "user" / "u1" / RECORDS_FILENAME).is_file()
    assert (tmp_path / "memories" / "projects" / "p1" / RECORDS_FILENAME).is_file()
    assert (tmp_path / "memories" / "threads" / "th_x" / RECORDS_FILENAME).is_file()
    # 作用域不串味
    assert len(store.user("u1")) == 1
    assert len(store.project("p1")) == 1
    assert store.user("p2") == []


def test_records_are_append_only_lines_with_envelope_fields(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    record = store.write(
        "user",
        "u1",
        "初版内容",
        origin="user",
        creator="researcher-a",
        confidence=0.8,
        source_event_ids=[_event_id()],
        tags=["偏好"],
    )
    path = store.find_path(record.memory_id)
    assert path.name == RECORDS_FILENAME

    store.update(record.memory_id, "第二版内容", actor="user")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2, "更新必须是追加一行，而不是覆盖"
    first = json.loads(lines[0])
    assert first["creator"] == "researcher-a"
    assert first["confidence"] == 0.8
    assert first["content_hash"] and first["revision"] == 1
    assert first["record"]["source_event_ids"]
    assert json.loads(lines[1])["revision"] == 2

    # 历史可审计：两版都在
    history = store.history(record.memory_id)
    assert [e.revision for e in history] == [1, 2]
    assert not all(e.record.deleted for e in history)


def test_content_hash_detects_tampering(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    record = store.write("user", "u1", "原始内容", origin="user")
    path = store.find_path(record.memory_id)
    raw = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert raw["content_hash"] == MemoryStore.hash_text("原始内容")

    tampered = dict(raw)
    tampered["record"] = {**raw["record"], "text": "被手改的内容"}
    path.write_text(json.dumps(tampered, ensure_ascii=False) + "\n", encoding="utf-8")
    stats = store.stats("user", "u1")
    assert stats["tampered"] == 1, "内容被手改必须能被检出"
    envelope = MemoryEnvelope.from_dict(tampered)
    assert envelope.intact is False


# ---------------------------------------------------------------------------- 保护与删除
def test_user_edited_memory_is_never_silently_overwritten(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    record = store.write("user", "u1", "初始", origin="auto", source_event_ids=[_event_id()])
    assert record.version == 1 and record.edited_by_user is False

    edited = store.update(record.memory_id, "用户手工修订", actor="user")
    assert edited.version == 2 and edited.edited_by_user is True
    assert edited.origin is MemoryOrigin.USER

    with pytest.raises(MemoryOverwriteDenied):
        store.update(record.memory_id, "自动想改回去", actor="auto")
    with pytest.raises(MemoryOverwriteDenied):
        store.write("user", "u1", "自动想改回去", origin="auto", memory_id=record.memory_id)

    assert store.get(record.memory_id).text == "用户手工修订"
    assert store.update(record.memory_id, "用户再改一次", actor="user").version == 3


def test_automation_can_still_create_new_records(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    a = store.write("conversation", "th_x", "自动一", source_event_ids=[_event_id()])
    b = store.write("conversation", "th_x", "自动二", source_event_ids=[_event_id()])
    assert a.memory_id != b.memory_id
    assert len(store.conversation("th_x")) == 2


def test_deletion_is_an_auditable_tombstone(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    record = store.write("project", "p1", "要删的内容", source_event_ids=[_event_id()])
    deleted = store.delete(record.memory_id, actor="user")

    assert deleted.deleted is True and deleted.version == 2
    assert store.project("p1") == []
    assert len(store.project("p1", include_deleted=True)) == 1

    path = store.find_path(record.memory_id)
    assert path.is_file(), "tombstone 不能删文件——历史必须留存"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2, "删除也是追加一行"
    assert json.loads(lines[1])["record"]["deleted"] is True


def test_rewrite_is_atomic_and_compacts_history(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    record = store.write("user", "u1", "v1", origin="user")
    store.update(record.memory_id, "v2", actor="user")
    store.update(record.memory_id, "v3", actor="user")
    assert len(store.history(record.memory_id)) == 3

    latest = store.envelopes("user", "u1")
    store.rewrite("user", "u1", latest)
    assert len(store.history(record.memory_id)) == 1
    assert store.get(record.memory_id).text == "v3"


# ---------------------------------------------------------------------------- 与 Harness 集成
def test_memory_store_records_from_a_real_thread(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("记忆线程")
    turn = h.repo.start_turn(thread.thread_id, inputs=[{"text": "记住我的偏好"}], idempotency_key="k")
    h.repo.complete_turn(thread.thread_id, turn.turn_id)
    event = h.repo.emit_event(thread.thread_id, "memory/written", payload={"scope": "user"})

    record = h.memory.write(
        "user",
        "u1",
        "偏好中文回答",
        origin="user",
        creator="user",
        source_event_ids=[event.event_id],
        confidence=0.9,
    )
    envelope = h.memory.get_envelope(record.memory_id)
    assert envelope.record.source_event_ids == [event.event_id]
    assert envelope.creator == "user"
    assert envelope.confidence == 0.9
    assert envelope.intact is True
    assert Path(h.memory.find_path(record.memory_id)).name == RECORDS_FILENAME


def test_scope_ids_must_be_safe_path_segments(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    for bad in ("../escape", "a/b", "", "."):
        with pytest.raises(ValueError):
            store.write("user", bad, "x", source_event_ids=[_event_id()])
