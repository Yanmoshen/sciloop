"""压缩与记忆验收（对应验收书 §7 压缩与记忆验收）。

- 自动和手动压缩都生成正式压缩事件；
- 摘要失败时活动上下文不变；
- 原始历史、压缩摘要和恢复快照同时可审计；
- 摘要可查看、编辑和恢复；
- 用户、项目、对话记忆目录隔离；
- 用户编辑的记忆不会被自动流程静默覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _helpers import Harness, run

from contracts.agent_v2 import (
    ErrorClass,
    MemoryOrigin,
    MemoryOverwriteDenied,
    MemoryScope,
    error_response,
    new_id,
)
from services.agent_runtime_v2 import build_context


def _seed_conversation(h: Harness, thread_id: str, rounds: int = 2, *, filler: int = 40):
    """灌入若干轮对话，让上下文足够触发压缩。"""
    for i in range(rounds):
        h.play(
            thread_id,
            f"第{i}问：请展开说明",
            h.text_script(f"第{i}轮回答" * filler),
            key=f"seed-{i}",
        )


# ---------------------------------------------------------------------------- §7.1
def test_manual_compaction_emits_official_events(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("手动压缩")
    _seed_conversation(h, thread.thread_id)
    svc = h.compaction(h.provider(h.text_script("这是摘要：用户在做论文统计")), token_budget=10)

    assert svc.should_compact(thread.thread_id) is True
    result = run(svc.compact(thread.thread_id, trigger="manual"))
    assert result.ok is True
    assert result.summary_id
    assert result.covered_until
    assert result.snapshot_sequence

    types = set(h.event_types(thread.thread_id))
    assert {"compaction/started", "compaction/completed"} <= types
    assert "compaction/failed" not in types


def test_auto_compaction_is_triggered_by_token_budget(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("自动压缩")
    _seed_conversation(h, thread.thread_id, rounds=1, filler=60)
    svc = h.compaction(h.provider(h.text_script("自动摘要")), token_budget=20)
    assert svc.should_compact(thread.thread_id) is True
    result = run(svc.compact(thread.thread_id, trigger="auto"))
    assert result.ok is True
    assert "compaction/completed" in set(h.event_types(thread.thread_id))
    # 压缩后上下文回落，不再触发
    assert svc.should_compact(thread.thread_id) is False


def test_no_new_events_means_no_secondary_compaction(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("空压缩")
    _seed_conversation(h, thread.thread_id, rounds=1)
    svc = h.compaction(h.provider(h.text_script("摘要一")), token_budget=10)
    assert run(svc.compact(thread.thread_id, trigger="manual")).ok is True
    second = run(svc.compact(thread.thread_id, trigger="manual"))
    assert second.ok is False
    assert second.error["code"] == "nothing_new"


# ---------------------------------------------------------------------------- §7.2
def test_failed_summary_keeps_the_active_context_unchanged(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("失败压缩")
    _seed_conversation(h, thread.thread_id)
    ok_svc = h.compaction(h.provider(h.text_script("原始摘要")), token_budget=10)
    good = run(ok_svc.compact(thread.thread_id, trigger="manual"))
    assert good.ok

    before = build_context(h.repo.state(thread.thread_id))
    failing = h.compaction(
        h.provider([error_response(ErrorClass.FATAL, "summarizer down")]), token_budget=10
    )
    # 制造新事件，让压缩有东西可做
    h.play(thread.thread_id, "新的一轮", h.text_script("新一轮回答" * 30), key="extra")

    result = run(failing.compact(thread.thread_id, trigger="auto"))
    assert result.ok is False
    assert "compaction/failed" in set(h.event_types(thread.thread_id))

    # 生效摘要仍是原来那条，且上下文里的摘要文本没变
    summaries = ok_svc.summaries(thread.thread_id)
    active = [s for s in summaries if s["active"]]
    assert len(active) == 1
    assert active[0]["text"] == "原始摘要"
    after = build_context(h.repo.state(thread.thread_id))
    assert sum(1 for m in before if "原始摘要" in str(m.get("content", ""))) == sum(
        1 for m in after if "原始摘要" in str(m.get("content", ""))
    )


# ---------------------------------------------------------------------------- §7.3
def test_raw_history_summary_and_snapshot_are_all_auditable(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("三方审计")
    _seed_conversation(h, thread.thread_id)
    svc = h.compaction(h.provider(h.text_script("可审计摘要")), token_budget=10)
    result = run(svc.compact(thread.thread_id, trigger="manual"))

    store = h.store_for(thread.thread_id)
    # 1) 原始历史仍在，且一条不少
    events = store.read_all()
    assert any(e.type == "item/added" for e in events)
    assert len([e for e in events if e.type == "item/added"]) >= 4
    h.assert_sequence_is_contiguous(thread.thread_id)

    # 2) 压缩前快照文件存在且可解析
    snapshot_path = Path(svc.snapshot_path(thread.thread_id, result.snapshot_sequence))
    assert snapshot_path.is_file()
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["sequence"] == result.snapshot_sequence
    assert payload["state"]["items"], "快照必须包含压缩前的完整状态"

    # 3) 摘要在事件流里可查
    completed = [e for e in events if e.type == "compaction/completed"]
    assert completed and completed[0].payload["summary_id"] == result.summary_id
    assert svc.summary(thread.thread_id, result.summary_id)["text"] == "可审计摘要"


# ---------------------------------------------------------------------------- §7.4
def test_summary_can_be_viewed_edited_and_restored(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("摘要编辑")
    _seed_conversation(h, thread.thread_id)
    svc = h.compaction(h.provider(h.text_script("第一版摘要")), token_budget=10)
    first = run(svc.compact(thread.thread_id, trigger="manual"))

    # 查看
    listed = svc.summaries(thread.thread_id)
    assert len(listed) == 1 and listed[0]["active"] is True

    # 编辑 -> 生效上下文必须使用编辑后的文本
    edited = svc.edit_summary(thread.thread_id, first.summary_id, "用户修订版摘要")
    assert edited["text"] == "用户修订版摘要"
    assert edited["edited"] is True
    context = build_context(h.repo.state(thread.thread_id))
    assert any("用户修订版摘要" in str(m.get("content", "")) for m in context)
    assert "compaction/edited" in set(h.event_types(thread.thread_id))

    # 再做一次压缩，产生第二版摘要
    h.play(thread.thread_id, "又一轮", h.text_script("又一轮回答" * 30), key="again")
    svc2 = h.compaction(h.provider(h.text_script("第二版摘要")), token_budget=10)
    second = run(svc2.compact(thread.thread_id, trigger="manual"))
    assert second.ok
    active_now = [s for s in svc2.summaries(thread.thread_id) if s["active"]]
    assert active_now[0]["summary_id"] == second.summary_id

    # 恢复回第一版（含其编辑内容）
    restored = svc2.restore_summary(thread.thread_id, first.summary_id)
    assert restored["active"] is True
    assert restored["text"] == "用户修订版摘要"
    assert "compaction/restored" in set(h.event_types(thread.thread_id))
    context2 = build_context(h.repo.state(thread.thread_id))
    assert any("用户修订版摘要" in str(m.get("content", "")) for m in context2)


def test_context_after_compaction_is_summary_plus_tail(tmp_path):
    h = Harness.create(tmp_path)
    thread = h.thread("摘要+尾段")
    _seed_conversation(h, thread.thread_id)
    svc = h.compaction(h.provider(h.text_script("摘要正文")), token_budget=10)
    result = run(svc.compact(thread.thread_id, trigger="manual"))

    # 压缩后新增一轮：只有这一轮应该出现在尾段里
    h.play(thread.thread_id, "压缩后的新问题", h.text_script("压缩后的新回答"), key="after")

    context = build_context(h.repo.state(thread.thread_id))
    joined = "\n".join(str(m.get("content", "")) for m in context)
    assert "摘要正文" in joined
    assert "压缩后的新问题" in joined
    assert "压缩后的新回答" in joined
    assert "第0问" not in joined, "被摘要覆盖的历史不应再进上下文"
    # 但原始历史仍在磁盘上
    assert any(
        e.payload.get("item", {}).get("payload", {}).get("text") == "第0问：请展开说明"
        for e in h.store_for(thread.thread_id).read_all()
        if e.type == "item/added"
    )
    assert result.covered_until is not None


# ---------------------------------------------------------------------------- §7.5 记忆
def test_memory_scopes_are_isolated(tmp_path):
    h = Harness.create(tmp_path)
    ev = new_id("event")
    h.memory.write(MemoryScope.USER, "u1", "用户偏好：中文", origin="user", source_event_ids=[ev])
    h.memory.write(MemoryScope.USER, "u2", "另一个用户的偏好", origin="user", source_event_ids=[ev])
    h.memory.write(MemoryScope.PROJECT, "p1", "项目约定：只读优先", source_event_ids=[ev])
    thread_id = h.thread("对话").thread_id
    h.memory.write(MemoryScope.CONVERSATION, thread_id, "本对话在做论文统计", source_event_ids=[ev])

    assert [r.text for r in h.memory.user("u1")] == ["用户偏好：中文"]
    assert [r.text for r in h.memory.user("u2")] == ["另一个用户的偏好"]
    assert [r.text for r in h.memory.project("p1")] == ["项目约定：只读优先"]
    assert [r.text for r in h.memory.conversation(thread_id)] == ["本对话在做论文统计"]
    # 跨作用域不得串味
    assert all(r.scope is MemoryScope.USER for r in h.memory.user("u1"))
    assert len(h.memory.project("p1")) == 1

    layout = h.memory.layout()
    # 新布局（WP-07）：user / projects / threads，每条作用域一个 records.jsonl
    assert layout["user"].endswith("user")
    assert layout["project"].endswith("projects")
    assert layout["conversation"].endswith("threads")
    # 物理目录隔离
    assert Path(layout["user"], "u1").is_dir()
    assert Path(layout["project"], "p1").is_dir()
    assert Path(layout["user"], "u1", "records.jsonl").is_file()


def test_user_edited_memory_is_not_silently_overwritten_by_automation(tmp_path):
    h = Harness.create(tmp_path)
    record = h.memory.write(MemoryScope.USER, "u1", "初始内容", origin="auto", source_event_ids=[new_id("event")])
    assert record.version == 1
    assert record.edited_by_user is False

    # 用户编辑
    edited = h.memory.update(record.memory_id, "用户手工修订", actor="user")
    assert edited.version == 2
    assert edited.edited_by_user is True
    assert edited.origin is MemoryOrigin.USER

    # 自动流程不得覆盖
    with pytest.raises(MemoryOverwriteDenied):
        h.memory.update(record.memory_id, "自动想改回去", actor="auto")
    with pytest.raises(MemoryOverwriteDenied):
        h.memory.write(MemoryScope.USER, "u1", "自动想改回去", origin="auto", memory_id=record.memory_id)

    # 内容仍是用户版本
    assert h.memory.get(record.memory_id).text == "用户手工修订"
    # 用户自己可以继续改
    assert h.memory.update(record.memory_id, "用户再改一次", actor="user").version == 3


def test_automation_can_still_create_new_records(tmp_path):
    h = Harness.create(tmp_path)
    first = h.memory.write(MemoryScope.CONVERSATION, "th_x", "自动记录一", source_event_ids=[new_id("event")])
    second = h.memory.write(MemoryScope.CONVERSATION, "th_x", "自动记录二", source_event_ids=[new_id("event")])
    assert first.memory_id != second.memory_id
    assert len(h.memory.conversation("th_x")) == 2


def test_memory_record_carries_source_version_time_and_deletion_state(tmp_path):
    h = Harness.create(tmp_path)
    event_id = new_id("event")
    record = h.memory.write(
        MemoryScope.PROJECT, "p1", "带来源的记忆", source_event_ids=[event_id], tags=["规范"]
    )
    stored = h.memory.get(record.memory_id)
    assert stored.source_event_ids == [event_id]
    assert stored.version == 1
    assert stored.created_at == stored.updated_at
    assert stored.deleted is False
    assert stored.tags == ["规范"]

    deleted = h.memory.delete(record.memory_id, actor="user")
    assert deleted.deleted is True
    assert deleted.version == 2
    # 软删除：文件保留、列表默认隐藏、可按需包含
    assert h.memory.find_path(record.memory_id).is_file()
    assert h.memory.project("p1") == []
    assert len(h.memory.project("p1", include_deleted=True)) == 1


def test_memory_files_follow_the_planned_layout(tmp_path):
    h = Harness.create(tmp_path)
    record = h.memory.write(MemoryScope.USER, "u1", "布局", source_event_ids=[new_id("event")])
    path = h.memory.find_path(record.memory_id)
    assert path.name == "records.jsonl"
    assert path.parent.name == "u1"
    assert path.parent.parent.name == "user"
    assert path.parent.parent.parent == tmp_path / "memories"
    # 落盘是「信封 + 契约对象」：record 部分仍可直接被 schema 校验
    from contracts.agent_v2 import validator_for

    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert line["envelope"] == 1 and line["content_hash"]
    validator_for("memory").check(line["record"])
