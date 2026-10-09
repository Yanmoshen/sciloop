"""压缩与记忆测试（验收书 §5）。

覆盖：压缩状态与摘要查看 / 编辑 / 恢复、压缩失败不留半成品、三级记忆的查看 / 编辑 / 删除
与作用域隔离。
"""

from __future__ import annotations

from api.v2.agent_protocol import ErrorCode


def _long_thread(api, name: str = "压缩会话"):
    thread = api.start_thread(name, scenario="compaction")["thread"]
    thread_id = thread["thread_id"]
    api.run_turn(thread_id, "第一轮：梳理研究空白")
    api.run_turn(thread_id, "第二轮：记录实验配置")
    return thread_id


def test_compact_emits_lifecycle_events_and_keeps_snapshot(api, service) -> None:
    thread_id = _long_thread(api)
    events_before = len(api.events(thread_id))
    result = api.call("thread/compact", {"thread_id": thread_id}, idem="compact-1")["result"]

    assert result["ok"] is True
    assert result["summary_id"].startswith("sum_")
    assert result["covered_until"] >= 1
    assert result["tokens_before"] > result["tokens_after"] > 0, "压缩必须真的降低上下文规模"

    types = api.event_types(thread_id)
    assert "compaction/started" in types
    assert "compaction/completed" in types
    assert len(api.events(thread_id)) > events_before

    # 原始事件仍在（压缩不改写历史），压缩前快照被保留
    assert len(api.events(thread_id)) >= events_before
    snapshot = service.compaction.snapshot_path(thread_id, result["snapshot_sequence"])
    assert snapshot.endswith(f"{result['covered_until']}.json")


def test_compaction_list_view_and_active_marker(api) -> None:
    thread_id = _long_thread(api, "摘要列表")
    api.call("thread/compact", {"thread_id": thread_id}, idem="compact-list")
    listed = api.call("thread/compaction/list", {"thread_id": thread_id})
    assert len(listed["summaries"]) == 1
    entry = listed["summaries"][0]
    assert entry["active"] is True
    assert entry["edited"] is False
    assert entry["text"]


def test_edit_and_restore_summary(api) -> None:
    thread_id = _long_thread(api, "摘要编辑")
    summary_id = api.call("thread/compact", {"thread_id": thread_id}, idem="compact-edit")["result"][
        "summary_id"
    ]

    edited = api.call(
        "thread/compaction/edit",
        {"thread_id": thread_id, "summary_id": summary_id, "text": "研究者手工修订后的摘要"},
        idem="edit-1",
    )
    assert edited["summary"]["text"] == "研究者手工修订后的摘要"
    assert edited["summary"]["edited"] is True
    assert edited["summary"]["active"] is True
    assert api.find_events(thread_id, "compaction/edited")

    # 压缩第二次（新内容）产生新摘要后，旧摘要可被恢复为生效摘要
    api.run_turn(thread_id, "第三轮：新增内容")
    second_id = api.call("thread/compact", {"thread_id": thread_id}, idem="compact-2")["result"][
        "summary_id"
    ]
    assert second_id != summary_id
    restored = api.call(
        "thread/compaction/restore", {"thread_id": thread_id, "summary_id": summary_id}, idem="restore-1"
    )
    assert restored["summary"]["active"] is True
    assert restored["summary"]["text"] == "研究者手工修订后的摘要"
    assert api.find_events(thread_id, "compaction/restored")
    snapshot = api.call("thread/resume", {"thread_id": thread_id})
    assert snapshot["active_summary"]["summary_id"] == summary_id


def test_compaction_without_new_content_is_reported(api) -> None:
    thread_id = _long_thread(api, "无新内容")
    api.call("thread/compact", {"thread_id": thread_id}, idem="compact-once")
    again = api.call("thread/compact", {"thread_id": thread_id}, idem="compact-twice")["result"]
    assert again["ok"] is False
    assert again["error"]["code"] == "nothing_new"
    # 失败不得留下半成品：生效摘要仍是第一次的那一条
    summaries = api.call("thread/compaction/list", {"thread_id": thread_id})["summaries"]
    assert len(summaries) == 1


def test_compaction_failure_keeps_context_intact(api, service) -> None:
    """摘要生成失败时必须落 compaction/failed，且不替换活动上下文。"""
    from contracts.agent_v2.errors import ModelStreamError

    thread_id = _long_thread(api, "压缩失败")

    class BrokenSummarizer:
        call_count = 0

        async def stream(self, request, cancel=None):  # noqa: ANN001, ANN202
            raise ModelStreamError("retryable", "摘要服务暂时不可用")
            yield  # pragma: no cover - 使函数成为异步生成器

    service.compaction.gateway = type(service.compaction.gateway)(BrokenSummarizer())
    result = api.call("thread/compact", {"thread_id": thread_id}, idem="compact-broken")["result"]
    assert result["ok"] is False
    assert result["error"]["code"] == "summarize_failed"
    assert "compaction/failed" in api.event_types(thread_id)
    assert "compaction/completed" not in api.event_types(thread_id)
    assert api.call("thread/compaction/list", {"thread_id": thread_id})["summaries"] == []


def test_compaction_unknown_summary(api) -> None:
    thread = api.start_thread("未知摘要", scenario="text_multi_turn")["thread"]
    api.fails(
        "thread/compaction/edit",
        {"thread_id": thread["thread_id"], "summary_id": "sum_" + "0" * 23, "text": "x"},
        expect=ErrorCode.SUMMARY_NOT_FOUND.value,
        idem="edit-missing",
    )


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #
def test_memory_scopes_are_isolated(api) -> None:
    listed = api.call("memory/list", {"scope": "user", "scope_id": "u1"})
    assert listed["records"] == []
    assert set(listed["layout"]) == {"user", "project", "conversation"}

    created = api.call(
        "memory/update",
        {"scope": "user", "scope_id": "u1", "text": "研究者偏好中文回答", "tags": ["偏好"]},
        idem="mem-1",
    )
    assert created["record"]["scope"] == "user"
    assert created["record"]["origin"] == "user"
    assert created["record"]["edited_by_user"] is True
    assert created["record"]["version"] == 1

    assert api.call("memory/list", {"scope": "project", "scope_id": "u1"})["records"] == []
    assert len(api.call("memory/list", {"scope": "user", "scope_id": "u1"})["records"]) == 1


def test_memory_update_and_delete_roundtrip(api) -> None:
    created = api.call(
        "memory/update",
        {"scope": "project", "scope_id": "p1", "text": "项目使用 SQLite"},
        idem="mem-2",
    )
    memory_id = created["record"]["memory_id"]

    updated = api.call(
        "memory/update",
        {"scope": "project", "scope_id": "p1", "memory_id": memory_id, "text": "项目使用 SQLite + aiosqlite"},
        idem="mem-3",
    )
    assert updated["record"]["version"] == 2
    assert updated["record"]["text"].endswith("aiosqlite")

    deleted = api.call("memory/delete", {"memory_id": memory_id}, idem="mem-4")
    assert deleted["deleted"] is True
    assert api.call("memory/list", {"scope": "project", "scope_id": "p1"})["records"] == []
    with_deleted = api.call(
        "memory/list", {"scope": "project", "scope_id": "p1", "include_deleted": True}
    )
    assert with_deleted["records"][0]["deleted"] is True


def test_conversation_memory_emits_event_into_thread(api) -> None:
    thread = api.start_thread("对话记忆", scenario="text_multi_turn")["thread"]
    thread_id = thread["thread_id"]
    result = api.call(
        "memory/update",
        {"scope": "conversation", "scope_id": thread_id, "text": "本会话关注采样规模"},
        idem="mem-conv",
    )
    assert result["record"]["scope"] == "conversation"
    written = api.find_events(thread_id, "memory/written")
    assert written, "对话记忆必须在线程事件流里留下可审计记录"
    assert written[-1].payload["memory_id"] == result["record"]["memory_id"]

    deleted = api.call("memory/delete", {"memory_id": result["record"]["memory_id"]}, idem="mem-conv-del")
    assert deleted["deleted"] is True


def test_memory_validation(api) -> None:
    api.fails(
        "memory/list", {"scope": "global"}, expect=ErrorCode.INVALID_PARAMS.value
    )
    api.fails(
        "memory/update",
        {"scope": "user", "scope_id": "u1", "text": ""},
        expect=ErrorCode.INVALID_PARAMS.value,
        idem="mem-empty",
    )
    api.fails(
        "memory/delete", {"memory_id": "mem_bad"}, expect=ErrorCode.INVALID_ID.value, idem="mem-bad"
    )
