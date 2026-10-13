"""Real WebSocket regression for streaming while a command is awaiting work."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from api.v2.agent.mount import install
from api.v2.agent.router import WS_PATH
from api.v2.agent.service import set_service
from api.v2.agent_protocol import PROTOCOL_VERSION


def _request(method: str, params: dict | None = None, *, rid: str, idem: str | None = None) -> dict:
    frame = {
        "contract": PROTOCOL_VERSION,
        "kind": "request",
        "id": rid,
        "method": method,
        "params": params or {},
    }
    if idem is not None:
        frame["idempotency_key"] = idem
    return frame


def _until(ws, predicate, limit: int = 400) -> list[dict]:
    seen: list[dict] = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message)
        if predicate(message):
            return seen
    raise AssertionError(f"target message not found: {seen[-1] if seen else None}")


def test_ws_flushes_subscribed_events_while_agent_wait_is_pending(tmp_path) -> None:
    """A long ``agent/wait`` must not starve unrelated subscribed events."""
    service = __import__("api.v2.agent.service", fromlist=["AgentV2Service"]).AgentV2Service(
        conversations_root=tmp_path / "conversations",
        memories_root=tmp_path / "memories",
        poll_interval_s=0.01,
        heartbeat_s=300,
        token_budget=200,
    )
    app = FastAPI()
    install(app, service=service)
    set_service(service)

    try:
        with TestClient(app) as client, client.websocket_connect(WS_PATH) as observer, client.websocket_connect(WS_PATH) as producer:
            observer.receive_json()
            producer.receive_json()

            observer.send_json(
                _request(
                    "thread/start",
                    {"name": "parent", "settings": {"scenario": "child_agents"}},
                    rid="p1",
                    idem="parent-start",
                )
            )
            parent_response = _until(observer, lambda m: m.get("kind") == "response" and m.get("id") == "p1")[-1]
            parent_id = parent_response["result"]["thread"]["thread_id"]
            observer.send_json(_request("thread/subscribe", {"thread_id": parent_id, "after_sequence": 0}, rid="p2"))
            _until(observer, lambda m: m.get("kind") == "response" and m.get("id") == "p2")
            observer.send_json(
                _request("turn/start", {"thread_id": parent_id, "text": "拆分"}, rid="p3", idem="parent-turn")
            )
            _until(observer, lambda m: m.get("kind") == "notification" and m.get("method") == "event" and m.get("params", {}).get("type") == "turn/completed")
            observer.send_json(_request("agent/list", {"thread_id": parent_id}, rid="p4"))
            children = _until(observer, lambda m: m.get("kind") == "response" and m.get("id") == "p4")[-1]["result"]["children"]
            child_id = next(child["thread_id"] for child in children if child["status"] == "running")

            producer.send_json(
                _request(
                    "thread/start",
                    {"name": "stream-source", "settings": {"scenario": "turn_interrupt"}},
                    rid="s1",
                    idem="source-start",
                )
            )
            source_id = _until(producer, lambda m: m.get("kind") == "response" and m.get("id") == "s1")[-1]["result"]["thread"]["thread_id"]
            producer.send_json(_request("thread/subscribe", {"thread_id": source_id, "after_sequence": 0}, rid="s1-sub"))
            _until(producer, lambda m: m.get("kind") == "response" and m.get("id") == "s1-sub")
            observer.send_json(_request("thread/subscribe", {"thread_id": source_id, "after_sequence": 0}, rid="p5"))
            _until(observer, lambda m: m.get("kind") == "response" and m.get("id") == "p5")
            producer.send_json(_request("turn/start", {"thread_id": source_id, "text": "长回答"}, rid="s2", idem="source-turn"))
            _until(producer, lambda m: m.get("kind") == "response" and m.get("id") == "s2")
            # Ensure the source has produced at least one durable event; the
            # observer must then receive its replay while agent/wait is still
            # pending on an unrelated child.
            _until(producer, lambda m: m.get("kind") == "notification" and m.get("method") == "event")

            observer.send_json(
                _request(
                    "agent/wait",
                    {"thread_id": parent_id, "child_thread_ids": [child_id], "timeout_s": 0.5},
                    rid="p6",
                )
            )
            # Control notifications may be queued ahead of replayed events;
            # the user-visible guarantee is that the subscribed event is
            # delivered before the long wait response completes.
            first = None
            for _ in range(20):
                candidate = observer.receive_json()
                if candidate.get("kind") == "notification" and candidate.get("method") == "event":
                    first = candidate
                    break
            assert first is not None, "no subscribed event arrived while agent/wait was pending"
            assert first.get("thread_id") == source_id, first
    finally:
        set_service(None)
        import asyncio

        asyncio.run(service.aclose())
