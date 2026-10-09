"""授权与「按状态给可行动错误」的测试（WP-01 错误码 / §7.2）。

新增的错误码不是摆设：每一条都有明确的触发点，前端据此能直接给出下一步动作。
"""

from __future__ import annotations

import asyncio

from api.v2.agent_protocol import PROTOCOL_VERSION, ErrorCode, Request
from contracts.agent_v2.enums import ToolCallStatus
from contracts.agent_v2.models import ToolCall


def _dispatch(service, conn, method: str, params: dict, *, rid: str = "req-1", idem: str | None = None):
    """在给定连接上发一条请求（同步包装，便于断言结构化错误）。"""
    loop = asyncio.new_event_loop()
    try:
        context = conn.context()
        request = Request(
            id=rid, method=method, params=params, idempotency_key=idem, contract=PROTOCOL_VERSION
        )
        return loop.run_until_complete(service.facade.dispatch(request, context)).to_dict()
    finally:
        loop.close()


def test_readonly_connection_can_read_but_not_mutate(service) -> None:
    conn = service.connect(owner=False)
    read = _dispatch(service, conn, "thread/list", {})
    assert read["ok"] is True, "只读连接必须能读"

    write = _dispatch(service, conn, "thread/start", {"name": "不该建成"}, idem="ro-1")
    assert write["ok"] is False
    assert write["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
    assert write["error"]["data"]["required"] == "owner"
    # 只读拒绝不得产生任何副作用
    assert service.repo.list_threads() == []
    service.disconnect(conn.connection_id)


def test_readonly_denial_has_user_facing_message(service) -> None:
    conn = service.connect(owner=False)
    write = _dispatch(service, conn, "turn/start", {"thread_id": "th_x", "text": "hi"}, idem="ro-2")
    message = write["error"]["message"]
    assert "只读" in message or "研究者" in message
    assert "owner_mode" not in message and "token" not in message.lower()
    service.disconnect(conn.connection_id)


def test_runtime_unavailable_is_structured(api, service) -> None:
    thread = api.start_thread("运行时不可用", scenario="text_multi_turn")["thread"]
    api.run(service.host.shutdown())
    error = api.fails(
        "turn/start",
        {"thread_id": thread["thread_id"], "text": "再跑一轮"},
        expect=ErrorCode.RUNTIME_UNAVAILABLE.value,
        idem="ru-1",
    )
    assert error["data"]["method"] == "turn/start"


def test_steer_on_interrupted_turn_reports_turn_interrupted(api) -> None:
    thread = api.start_thread("中断后追加", scenario="turn_interrupt")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "长回答"}, idem="si-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"running"})
    api.call("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}, idem="si-int")

    error = api.fails(
        "turn/steer",
        {"thread_id": thread_id, "turn_id": turn_id, "text": "补充"},
        expect=ErrorCode.TURN_INTERRUPTED.value,
        idem="si-1",
    )
    assert error["data"]["status"] == "interrupted"
    assert "恢复" in error["message"], "错误信息要给出下一步动作"


def test_continue_on_failed_turn_reports_turn_failed(api) -> None:
    thread = api.start_thread("失败后继续", scenario="error_fatal")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "会失败"}, idem="cf-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_idle(thread_id)

    error = api.fails(
        "turn/continue",
        {"thread_id": thread_id, "turn_id": turn_id, "text": "继续"},
        expect=ErrorCode.TURN_FAILED.value,
        idem="cf-1",
    )
    assert error["data"]["status"] == "failed"
    assert "重试" in error["message"]


def test_control_while_waiting_approval_reports_approval_pending(api) -> None:
    thread = api.start_thread("待审批不可追加", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="ap-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    approval = next(iter(api.state(thread_id).approvals.values()))

    error = api.fails(
        "turn/steer",
        {"thread_id": thread_id, "turn_id": turn_id, "text": "补充"},
        expect=ErrorCode.APPROVAL_PENDING.value,
        idem="ap-1",
    )
    assert error["data"]["approval_id"] == approval.approval_id
    assert "审批" in error["message"]

    api.call(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "deny",
        },
        idem="ap-cleanup",
    )
    api.wait_idle(thread_id)


def test_resolve_approval_after_interrupt_reports_state(api) -> None:
    """审批挂起期间中断回合：再提交审批结论必须得到「已中断」，而不是笼统的非法迁移。"""
    thread = api.start_thread("中断后的审批", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="ai-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    approval = next(iter(api.state(thread_id).approvals.values()))
    api.call("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id}, idem="ai-int")

    error = api.fails(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        expect=ErrorCode.TURN_INTERRUPTED.value,
        idem="ai-1",
    )
    assert error["data"]["turn_id"] == turn_id


def test_resolve_approval_when_call_already_failed_reports_tool_failed(api, service) -> None:
    """工具执行层（Agent 2）先报失败、审批结论后到：必须明确「没有可执行的步骤」。

    这里直接按 Agent 2 的契约把调用置为失败态（`upsert_tool_call`），
    模拟「审批还在挂起，但调用已经被执行层判定失败」的竞态。
    """
    thread = api.start_thread("调用已失败", scenario="approval_flow")["thread"]
    thread_id = thread["thread_id"]
    started = api.call("turn/start", {"thread_id": thread_id, "text": "执行命令"}, idem="tf-t1")
    turn_id = started["turn"]["turn_id"]
    api.wait_status(thread_id, turn_id, {"waiting_approval"})
    state = api.state(thread_id)
    approval = next(iter(state.approvals.values()))
    call = state.tool_calls[approval.call_id]
    failed = call.to_dict()
    failed["status"] = ToolCallStatus.FAILED.value
    failed["error"] = {"code": "execution_failed", "message": "宿主进程启动失败"}
    service.repo.upsert_tool_call(
        thread_id, ToolCall.from_dict(failed), event_type="tool/failed"
    )

    error = api.fails(
        "approval/resolve",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "approval_id": approval.approval_id,
            "decision": "approve_once",
        },
        expect=ErrorCode.TOOL_FAILED.value,
        idem="tf-1",
    )
    assert error["data"]["call_id"] == approval.call_id
    assert error["data"]["error"]["code"] == "execution_failed"


def test_ws_anonymous_public_demo_connects_readonly(monkeypatch, service_factory) -> None:
    """匿名公开面：能建立会话（可读），变更类请求得到 permission_denied。"""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from api.v2.agent import auth
    from api.v2.agent.mount import install
    from api.v2.agent.router import WS_PATH
    from api.v2.agent.service import set_service

    monkeypatch.setattr(auth, "_matcher", lambda: (lambda token: False))
    svc = service_factory(heartbeat_s=300.0)
    application = FastAPI()
    install(application, service=svc)
    try:
        with TestClient(application) as client, client.websocket_connect(WS_PATH) as ws:
            ready = ws.receive_json()
            assert ready["method"] == "protocol/ready"
            assert ready["params"]["owner"] is False

            ws.send_json(
                {
                    "contract": PROTOCOL_VERSION,
                    "kind": "request",
                    "id": "ro-read",
                    "method": "thread/list",
                    "params": {},
                }
            )
            ws.send_json(
                {
                    "contract": PROTOCOL_VERSION,
                    "kind": "request",
                    "id": "ro-write",
                    "method": "thread/start",
                    "params": {"name": "不该建成"},
                    "idempotency_key": "ro-ws-1",
                }
            )
            seen: dict[str, dict] = {}
            for _ in range(40):
                message = ws.receive_json()
                if message.get("kind") == "response":
                    seen[message["id"]] = message
                if len(seen) == 2:
                    break
            assert seen["ro-read"]["ok"] is True
            assert seen["ro-write"]["ok"] is False
            assert seen["ro-write"]["error"]["code"] == ErrorCode.PERMISSION_DENIED.value
            assert svc.repo.list_threads() == []
    finally:
        set_service(None)
