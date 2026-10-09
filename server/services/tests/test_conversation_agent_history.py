from __future__ import annotations

import json

from services import conversations


def _record(tmp_path, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_DIR", str(tmp_path))
    return conversations.create(title="多轮测试", model_ref="test:model")


def test_protocol_history_keeps_tool_pairs_and_excludes_system(tmp_path, monkeypatch):
    record = _record(tmp_path, monkeypatch)
    messages = [
        {"role": "system", "content": "动态提示词"},
        {"role": "user", "content": "读取文件"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
            ],
            "reasoning_content": "思考",
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'},
        {"role": "assistant", "content": "已读取"},
    ]
    conversations.update_agent_state(record, messages)
    conversations.write(record)

    restored = conversations.read(record["id"])
    assert restored is not None
    history = conversations.context_messages(restored)
    assert [item["role"] for item in history] == ["user", "assistant", "tool", "assistant"]
    assert history[1]["tool_calls"][0]["id"] == "call-1"
    assert history[2]["tool_call_id"] == "call-1"
    assert all(item["role"] != "system" for item in history)
    json.dumps(history, ensure_ascii=False)


def test_edit_restart_truncates_active_history_at_user_turn(tmp_path, monkeypatch):
    record = _record(tmp_path, monkeypatch)
    history = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
        {"role": "assistant", "content": "第二答"},
    ]
    conversations.update_agent_state(record, history)
    conversations.truncate_agent_history_from_user(record, 1)
    assert [item["content"] for item in conversations.context_messages(record)] == ["第一问", "第一答"]


def test_update_agent_state_persists_compaction_checkpoint_and_snapshot(tmp_path, monkeypatch):
    record = _record(tmp_path, monkeypatch)
    checkpoint = {"checkpoint_id": "cp-1", "trigger": "manual", "summary": "早期事实"}
    snapshot = {"id": "cp-1", "messages": [{"role": "user", "content": "原始问题"}]}
    conversations.update_agent_state(
        record,
        [{"role": "user", "content": "早期事实"}],
        checkpoints=[checkpoint],
        snapshots=[snapshot],
    )
    conversations.write(record)
    restored = conversations.read(record["id"])
    assert restored is not None
    assert restored["compaction_checkpoints"][0]["trigger"] == "manual"
    assert restored["agent_history_snapshots"][0]["messages"][0]["content"] == "原始问题"
