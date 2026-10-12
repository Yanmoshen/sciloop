from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from api.v1.chat import (
    HomeChatRequest,
    _prepare_home_message,
    _truncate_for_edit,
    _turn_in_progress_detail,
)
from services import conversations


@pytest.mark.asyncio
@pytest.mark.parametrize("target_project", [7, None])
async def test_conversation_project_changes_are_rejected_without_side_effects(
    tmp_path, monkeypatch, target_project
) -> None:
    import httpx
    from fastapi import FastAPI

    from api.v1.conversations import router
    from core.security import require_owner

    monkeypatch.setenv("CONVERSATIONS_DIR", str(tmp_path / "conversations"))
    record = conversations.create(title="论文讨论", model_ref="test", project_id=3)
    original_path = conversations.locate(record["id"])
    original_bytes = original_path.read_bytes()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_owner] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.patch(
            f"/conversations/{record['id']}",
            json={"project_id": target_project, "archived": True, "title": "不应保存"},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["detail"]["allowed"] == ["archived", "title"]
    assert conversations.locate(record["id"]) == original_path
    assert original_path.read_bytes() == original_bytes


def test_home_request_builds_multimodal_image_message() -> None:
    image = b"\x89PNG\r\n\x1a\nimage"
    payload = HomeChatRequest(
        text="这张图是什么？",
        model_config_id=1,
        model_id="vision-model",
        attachments=[
            {
                "name": "figure.png",
                "mime_type": "image/png",
                "size": len(image),
                "data_base64": base64.b64encode(image).decode("ascii"),
            }
        ],
    )

    display_text, content, metadata = _prepare_home_message(payload)

    assert display_text == "这张图是什么？"
    assert content[0] == {"type": "text", "text": "这张图是什么？"}
    assert content[1]["type"] == "image_url"
    assert metadata == [{"name": "figure.png", "mime_type": "image/png", "size": len(image)}]


def test_turn_in_progress_conflict_includes_resume_identity(monkeypatch) -> None:
    from types import SimpleNamespace

    from services import home_turns

    owner = SimpleNamespace(
        conversation_id="202610129907",
        turn_id="a" * 32,
        snapshot={"status": "inProgress", "event_seq": 4},
    )
    monkeypatch.setattr(home_turns, "active", lambda conversation_id: owner)

    assert _turn_in_progress_detail(owner.conversation_id) == {
        "conversation_id": owner.conversation_id,
        "turn_id": owner.turn_id,
        "status": "inProgress",
        "event_seq": 4,
        "resume_url": f"/api/v1/chat/turns/{owner.conversation_id}/{owner.turn_id}/stream",
    }


def test_only_latest_user_turn_can_be_replaced() -> None:
    record = {
        "turns": [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "最后一问"},
        ],
        "agent_history": [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "最后一问"},
        ],
    }

    with pytest.raises(HTTPException) as exc_info:
        _truncate_for_edit(record, 0)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "only_latest_user_turn_editable"
    assert _truncate_for_edit(record, 2) is True
    assert record["turns"] == record["agent_history"][:2]


def test_append_turns_adds_iso_timestamps_to_both_roles(monkeypatch) -> None:
    record = {"turns": [], "agent_history": []}
    monkeypatch.setattr(conversations, "write", lambda _record: None)

    conversations.append_turns(
        record,
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
    )

    assert all(turn["ts"].endswith("+00:00") for turn in record["turns"])
    assert [message["content"] for message in record["agent_history"]] == ["hello", "world"]
