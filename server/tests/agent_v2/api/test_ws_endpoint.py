"""WebSocket 通道测试（验收书 §2、§6）。

用 Starlette 的 ``TestClient`` 起真实的应用事件循环：后台 Turn 任务会随测试线程
阻塞在 ``receive`` 时继续推进，因此可以观察到**真实的流式推送**。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.v2.agent.mount import PATCH_SNIPPET, install
from api.v2.agent.router import MOUNT_PREFIX, WS_PATH
from api.v2.agent.service import set_service
from api.v2.agent_protocol import PROTOCOL_VERSION

WS_URL = WS_PATH


@pytest.fixture()
def app(service) -> FastAPI:
    """按协调 Agent 的挂载方式装配应用（验证挂载补丁可用且幂等）。"""
    application = FastAPI()
    install(application, service=service)
    install(application, service=service)  # 幂等：不得重复挂载
    yield application
    set_service(None)


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


def _request(method: str, params: dict | None = None, *, rid: str = "r1", idem: str | None = None) -> dict:
    frame = {
        "contract": PROTOCOL_VERSION,
        "kind": "request",
        "id": rid,
        "method": method,
        "params": params or {},
    }
    if idem:
        frame["idempotency_key"] = idem
    return frame


def _read_until(ws, predicate: Callable[[dict], bool], *, limit: int = 400) -> list[dict]:
    seen: list[dict] = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message)
        if predicate(message):
            return seen
    raise AssertionError(f"未在 {limit} 条消息内等到目标消息；最后一条：{seen[-1] if seen else None}")


def _is_event(message: dict, thread_id: str | None = None) -> bool:
    """只有 ``method == "event"`` 的通知参与事件游标。

    连接级 / 订阅级控制通知（心跳、订阅确认、关闭提示）用的是**连接内自增序号**，
    与事件序号是不同的序列空间，绝不能用它推进游标。
    """
    if message.get("kind") != "notification" or message.get("method") != "event":
        return False
    return thread_id is None or message.get("thread_id") == thread_id


def _response_for(ws, request_id: str) -> dict:
    messages = _read_until(
        ws, lambda m: m["kind"] == "response" and m["id"] == request_id
    )
    return messages[-1]


def test_health_and_protocol_http_endpoints(client: TestClient) -> None:
    health = client.get(f"{MOUNT_PREFIX}/agent/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["host"] == "fake"
    assert body["contract_freeze_tag"] == "agent-v2-contract-v1"
    assert body["scenarios"]

    protocol = client.get(f"{MOUNT_PREFIX}/agent/protocol")
    assert protocol.status_code == 200
    described = protocol.json()
    assert described["protocol_version"] == PROTOCOL_VERSION
    assert described["websocket_path"] == WS_PATH
    assert "thread/start" in {row["name"] for row in described["methods"]}
    assert described["schema"]["$id"].startswith("agent.v2.protocol.v1")


def test_rpc_http_channel_end_to_end(client: TestClient) -> None:
    started = client.post(
        f"{MOUNT_PREFIX}/agent/rpc",
        json=_request("thread/start", {"name": "HTTP 通道"}, rid="h1", idem="h-start"),
    ).json()
    assert started["response"]["ok"] is True
    thread_id = started["response"]["result"]["thread"]["thread_id"]

    turn = client.post(
        f"{MOUNT_PREFIX}/agent/rpc",
        json=_request("turn/start", {"thread_id": thread_id, "text": "你好"}, rid="h2", idem="h-turn"),
    ).json()
    assert turn["response"]["ok"] is True

    replayed = client.post(
        f"{MOUNT_PREFIX}/agent/rpc",
        json=_request("thread/events/replay", {"thread_id": thread_id, "after_sequence": 0}, rid="h3"),
    ).json()
    events = replayed["response"]["result"]["events"]
    assert events and events[0]["type"] == "thread/created"


def test_rpc_rejects_bad_frame(client: TestClient) -> None:
    bad = client.post(f"{MOUNT_PREFIX}/agent/rpc", json={"kind": "response", "id": "x"}).json()
    assert bad["response"] is None
    assert bad["error"]["code"] == "invalid_request"


def test_mount_patch_snippet_is_documented() -> None:
    assert "install_agent_v2(app)" in PATCH_SNIPPET


def test_ws_full_flow_with_streaming_notifications(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as ws:
        welcome = ws.receive_json()
        assert welcome["kind"] == "notification"
        assert welcome["method"] == "subscription/started"
        assert welcome["thread_id"] is None
        assert welcome["event_id"].startswith("ev_")
        assert welcome["params"]["service"]["host"] == "fake"

        ws.send_json(_request("protocol/describe", rid="d1"))
        assert _response_for(ws, "d1")["ok"] is True

        ws.send_json(_request("thread/start", {"name": "WS 流式"}, rid="d2", idem="ws-start"))
        thread_id = _response_for(ws, "d2")["result"]["thread"]["thread_id"]

        ws.send_json(
            _request("thread/subscribe", {"thread_id": thread_id, "after_sequence": 0}, rid="d3")
        )
        subscribed = _response_for(ws, "d3")["result"]
        assert subscribed["thread_id"] == thread_id

        ws.send_json(
            _request("turn/start", {"thread_id": thread_id, "text": "跑一轮"}, rid="d4", idem="ws-turn")
        )
        messages = _read_until(
            ws,
            lambda m: m["kind"] == "notification"
            and m["method"] == "event"
            and m["params"]["type"] == "turn/completed",
        )
        events = [m for m in messages if _is_event(m, thread_id)]
        assert events, "订阅方必须收到事件通知"
        sequences = [note["sequence"] for note in events]
        assert sequences == sorted(sequences), "通知必须按序号单调投递"
        assert len(set(sequences)) == len(sequences), "通知不得重复投递"
        assert all(note["event_id"].startswith("ev_") for note in events)

        # 取消订阅 -> 明确通知
        ws.send_json(_request("thread/unsubscribe", {"thread_id": thread_id}, rid="d5"))
        assert _response_for(ws, "d5")["result"]["subscribed"] is False
        cancelled = _read_until(ws, lambda m: m.get("method") == "subscription/cancelled")
        assert cancelled[-1]["thread_id"] == thread_id


def test_ws_reconnect_resumes_from_last_cursor(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        ws.send_json(_request("thread/start", {"name": "WS 重连"}, rid="s1", idem="rc-start"))
        thread_id = _response_for(ws, "s1")["result"]["thread"]["thread_id"]
        ws.send_json(_request("thread/subscribe", {"thread_id": thread_id, "after_sequence": 0}, rid="s2"))
        _response_for(ws, "s2")
        ws.send_json(_request("turn/start", {"thread_id": thread_id, "text": "第一轮"}, rid="s3", idem="rc-t1"))
        messages = _read_until(
            ws,
            lambda m: m["kind"] == "notification"
            and m["method"] == "event"
            and m["params"]["type"] == "turn/completed",
        )
        delivered = [m["sequence"] for m in messages if _is_event(m, thread_id)]
        cursor = max(delivered)

    # 断线后重连：带上最后游标，只补新事件
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        ws.send_json(
            _request("thread/subscribe", {"thread_id": thread_id, "after_sequence": cursor}, rid="s4")
        )
        resumed = _response_for(ws, "s4")["result"]
        assert resumed["cursor"] == cursor
        assert resumed["pending"] == 0, "重连时没有新事件就不该补齐"
        ws.send_json(_request("turn/start", {"thread_id": thread_id, "text": "第二轮"}, rid="s5", idem="rc-t2"))
        messages = _read_until(
            ws,
            lambda m: m["kind"] == "notification"
            and m["method"] == "event"
            and m["params"]["type"] == "turn/completed",
        )
        fresh = [m["sequence"] for m in messages if _is_event(m, thread_id)]
        assert fresh and min(fresh) == cursor + 1, "重连后必须恰好从游标之后继续"


def test_ws_malformed_frame_gets_structured_error_and_stays_open(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        ws.send_text("{ not json")
        error_note = _read_until(ws, lambda m: m.get("method") == "protocol/error")
        assert error_note[-1]["params"]["error"]["code"] == "invalid_request"
        # 连接仍然可用
        ws.send_json(_request("protocol/describe", rid="ok"))
        assert _response_for(ws, "ok")["ok"] is True


def test_ws_reports_stale_cursor_on_subscribe(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        ws.send_json(_request("thread/start", {"name": "游标失效"}, rid="t1", idem="sc-start"))
        thread_id = _response_for(ws, "t1")["result"]["thread"]["thread_id"]
        ws.send_json(
            _request("thread/subscribe", {"thread_id": thread_id, "after_sequence": 999}, rid="t2")
        )
        response = _response_for(ws, "t2")
        assert response["ok"] is False
        assert response["error"]["code"] == "stale_cursor"
        assert response["error"]["data"]["reason"] == "cursor_ahead"


def test_ws_rejects_non_request_frames(client: TestClient) -> None:
    with client.websocket_connect(WS_URL) as ws:
        ws.receive_json()
        ws.send_json({"contract": PROTOCOL_VERSION, "kind": "notification", "method": "event"})
        note = _read_until(ws, lambda m: m.get("method") == "protocol/error")
        assert note[-1]["params"]["error"]["code"] == "invalid_request"


def test_ws_closes_after_server_shutdown(service_factory) -> None:
    svc = service_factory(heartbeat_s=300.0)
    import asyncio

    asyncio.run(svc.shutdown())
    application = FastAPI()
    install(application, service=svc)
    with TestClient(application) as test_client, test_client.websocket_connect(WS_URL) as ws:
        welcome = ws.receive_json()
        assert welcome["method"] == "subscription/started"
        with pytest.raises(WebSocketDisconnect):
            for _ in range(20):
                ws.receive_json()
    set_service(None)


def test_ws_identity_of_response_and_notification_shapes(client: TestClient) -> None:
    """帧结构必须能被协议 Schema 校验（端到端再确认一次）。"""
    from api.v2.agent_protocol import validate_frame

    with client.websocket_connect(WS_URL) as ws:
        welcome = ws.receive_json()
        validate_frame(welcome, "notification")
        ws.send_json(_request("thread/list", rid="l1"))
        response = _response_for(ws, "l1")
        validate_frame(response, "response")
        assert json.dumps(response)  # 可序列化
