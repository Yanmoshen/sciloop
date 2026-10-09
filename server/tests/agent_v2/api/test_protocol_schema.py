"""协议 Schema 与控制面的正向 / 反向测试（验收书 §2、§6）。

覆盖：
- 三类帧（request / response / notification）结构区分正确；
- 结构性非法帧被官方 jsonschema 拒绝（反向）；
- 方法表与门面实际实现一一对应（防止「注册了但没实现」）；
- 错误码枚举在 schema 与 Python 之间零漂移。
"""

from __future__ import annotations

import pytest

from api.v2.agent.facade import AgentFacade
from api.v2.agent_protocol import (
    KIND_NOTIFICATION,
    KIND_REQUEST,
    KIND_RESPONSE,
    METHOD_TABLE,
    PROTOCOL_VERSION,
    ErrorCode,
    Notification,
    ProtocolError,
    Response,
    describe_methods,
    load_protocol_schema,
    parse_client_frame,
    validate_frame,
)
from contracts.agent_v2.ids import new_id


def _request_frame(**overrides) -> dict:
    frame = {
        "contract": PROTOCOL_VERSION,
        "kind": KIND_REQUEST,
        "id": "req-1",
        "method": "thread/list",
        "params": {},
        "idempotency_key": None,
    }
    frame.update(overrides)
    return frame


def test_contract_and_schema_files_are_present() -> None:
    schema = load_protocol_schema()
    assert schema["$id"] == "agent.v2.protocol.v1/jsonrpc.schema.json"
    for branch in ("request", "response", "notification", "error", "frame"):
        assert branch in schema["$defs"], branch


def test_valid_request_passes_schema() -> None:
    validate_frame(_request_frame(), KIND_REQUEST)


def test_request_requires_contract_kind_id_and_method() -> None:
    for missing in ("contract", "kind", "id", "method"):
        frame = _request_frame()
        frame.pop(missing)
        with pytest.raises(ProtocolError) as excinfo:
            validate_frame(frame, KIND_REQUEST)
        assert excinfo.value.code == ErrorCode.INVALID_REQUEST.value


def test_request_rejects_unknown_field_and_bad_method_shape() -> None:
    with pytest.raises(ProtocolError):
        validate_frame(_request_frame(unexpected=1), KIND_REQUEST)
    with pytest.raises(ProtocolError):
        validate_frame(_request_frame(method="threadstart"), KIND_REQUEST)
    with pytest.raises(ProtocolError):
        validate_frame(_request_frame(params=[]), KIND_REQUEST)


def test_response_frame_requires_ok_result_and_error() -> None:
    ok = Response.success("req-1", {"threads": []}).to_dict()
    validate_frame(ok, KIND_RESPONSE)
    assert ok["kind"] == KIND_RESPONSE and ok["result"] == {"threads": []}

    bad = Response.failure("req-2", ProtocolError(ErrorCode.THREAD_NOT_FOUND, "no such thread")).to_dict()
    validate_frame(bad, KIND_RESPONSE)
    assert bad["ok"] is False
    assert bad["error"]["code"] == ErrorCode.THREAD_NOT_FOUND.value
    assert bad["result"] is None

    broken = dict(ok)
    broken.pop("error")
    with pytest.raises(ProtocolError):
        validate_frame(broken, KIND_RESPONSE)


def test_notification_carries_event_id_sequence_and_thread_id() -> None:
    note = Notification(
        method="event",
        event_id=new_id("event"),
        sequence=7,
        thread_id=new_id("thread"),
        params={"type": "item/added", "payload": {}},
    )
    payload = note.to_dict()
    validate_frame(payload, KIND_NOTIFICATION)
    assert payload["event_id"].startswith("ev_")
    assert payload["sequence"] == 7
    assert payload["thread_id"].startswith("th_")


def test_connection_level_notification_keeps_thread_id_field() -> None:
    from api.v2.agent_protocol import control_notification

    payload = control_notification("server/shutting_down", sequence=3).to_dict()
    validate_frame(payload, KIND_NOTIFICATION)
    assert "thread_id" in payload and payload["thread_id"] is None


def test_notification_rejects_unknown_method_and_bad_ids() -> None:
    base = {
        "contract": PROTOCOL_VERSION,
        "kind": KIND_NOTIFICATION,
        "method": "event",
        "event_id": new_id("event"),
        "sequence": 1,
        "thread_id": None,
        "params": {},
    }
    validate_frame(dict(base), KIND_NOTIFICATION)
    with pytest.raises(ProtocolError):
        validate_frame({**base, "method": "server/whatever"}, KIND_NOTIFICATION)
    with pytest.raises(ProtocolError):
        validate_frame({**base, "event_id": "ev_not-a-real-id"}, KIND_NOTIFICATION)
    with pytest.raises(ProtocolError):
        validate_frame({**base, "sequence": -1}, KIND_NOTIFICATION)


def test_parse_client_frame_accepts_only_requests() -> None:
    request = parse_client_frame(_request_frame())
    assert request.method == "thread/list"
    assert request.fingerprint() == parse_client_frame(_request_frame()).fingerprint()

    for kind in (KIND_RESPONSE, KIND_NOTIFICATION):
        with pytest.raises(ProtocolError) as excinfo:
            parse_client_frame(_request_frame(kind=kind))
        assert excinfo.value.code == ErrorCode.INVALID_REQUEST.value


def test_parse_client_frame_rejects_non_json_and_non_object() -> None:
    with pytest.raises(ProtocolError) as excinfo:
        parse_client_frame("{not json")
    assert excinfo.value.code == ErrorCode.INVALID_REQUEST.value
    with pytest.raises(ProtocolError):
        parse_client_frame("[1, 2, 3]")


def test_fingerprint_ignores_nothing_but_tracks_params() -> None:
    first = parse_client_frame(_request_frame(params={"limit": 1}))
    second = parse_client_frame(_request_frame(params={"limit": 2}))
    same = parse_client_frame(_request_frame(params={"limit": 1}))
    assert first.fingerprint() != second.fingerprint()
    assert first.fingerprint() == same.fingerprint()


def test_every_registered_method_has_an_implementation() -> None:
    for name, spec in METHOD_TABLE.items():
        handler = getattr(AgentFacade, spec.handler, None)
        assert handler is not None, f"{name} -> {spec.handler} 未实现"
        assert callable(handler), name
        # 变更类方法必须要求幂等键（否则断线重试会重复产生副作用）
        if spec.mutating:
            assert spec.idempotent and "idempotency_key" not in spec.allowed


def test_describe_methods_is_stable_and_complete() -> None:
    described = describe_methods()
    assert [row["name"] for row in described] == sorted(METHOD_TABLE)
    for row in described:
        assert row["mutating"] is row["requires_idempotency_key"]
        assert isinstance(row["required"], list)


def test_error_code_enum_matches_python_enum() -> None:
    schema = load_protocol_schema()
    enum = schema["$defs"]["error"]["properties"]["code"]["enum"]
    assert set(enum) == {member.value for member in ErrorCode}, (
        "schema 与 Python 错误码枚举漂移；两边必须同时改"
    )


def test_protocol_method_names_cover_plan_requirements() -> None:
    """计划书 §4.1 列出的方法必须全部存在。"""
    required = {
        "thread/start",
        "thread/resume",
        "thread/settings/update",
        "turn/start",
        "turn/steer",
        "turn/continue",
        "turn/interrupt",
        "turn/recover",
        "thread/subscribe",
        "thread/events/replay",
        "approval/resolve",
        "thread/compact",
        "thread/compaction/edit",
        "thread/compaction/restore",
        "memory/list",
        "memory/update",
        "memory/delete",
        "agent/list",
        "agent/wait",
        "agent/interrupt",
    }
    missing = sorted(required - set(METHOD_TABLE))
    assert not missing, f"计划书要求的方法缺失：{missing}"
