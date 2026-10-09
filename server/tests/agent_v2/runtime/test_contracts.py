"""契约验收（对应验收书 §2 契约验收）。

覆盖：稳定 ID、Schema 往返、非法枚举与缺失关联字段必须失败、
事件必备元信息、ToolCall 统一结构、schema 与 Python 枚举零漂移。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from contracts.agent_v2 import (
    CONTRACT_VERSION,
    SCHEMA_FILES,
    ApprovalRequest,
    ContractViolation,
    Event,
    EventType,
    Item,
    ItemType,
    MemoryOrigin,
    MemoryRecord,
    MemoryScope,
    ModelStreamKind,
    ModelRequest,
    SchemaValidator,
    StopReason,
    TextDelta,
    Thread,
    ToolCall,
    ToolCallCompleted,
    ToolCallDelta,
    ToolCallStatus,
    ToolKind,
    ToolSpec,
    Turn,
    TurnStatus,
    Usage,
    StreamCompleted,
    StreamError,
    error_response,
    is_valid_id,
    load_schema,
    new_id,
    parse_id,
    reasoning_response,
    schema_enum,
    text_response,
    tool_call_response,
    validate_all_present,
    validator_for,
)
from contracts.agent_v2.enums import (
    TERMINAL_TURN_STATUSES,
    TURN_TRANSITIONS,
    ErrorClass,
    can_transition,
)


# --------------------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------------------
def _now() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _thread() -> Thread:
    return Thread(thread_id=new_id("thread"), name="契约线程", created_at=_now(), updated_at=_now())


def _turn(thread_id: str, status: TurnStatus = TurnStatus.RUNNING) -> Turn:
    return Turn(
        turn_id=new_id("turn"),
        thread_id=thread_id,
        status=status,
        created_at=_now(),
        updated_at=_now(),
        sequence_start=1,
    )


# --------------------------------------------------------------------------------------
# §2.1 稳定 ID
# --------------------------------------------------------------------------------------
def test_all_contract_objects_have_stable_prefixed_ids():
    pairs = [
        ("thread", "th"),
        ("turn", "tu"),
        ("item", "it"),
        ("call", "call"),
        ("event", "ev"),
        ("approval", "ap"),
        ("memory", "mem"),
        ("compaction", "cmp"),
        ("summary", "sum"),
    ]
    seen = set()
    for kind, prefix in pairs:
        value = new_id(kind)
        assert value.startswith(prefix + "_"), (kind, value)
        assert is_valid_id(kind, value), (kind, value)
        assert parse_id(value)[1] > 0
        seen.add(value)
    assert len(seen) == len(pairs), "id 必须唯一"


def test_id_is_monotonic_by_time_and_rejects_foreign_prefix():
    early = new_id("thread", now_ms=1_700_000_000_000)
    late = new_id("thread", now_ms=1_700_000_001_000)
    assert early < late, "同类型 ID 字典序必须等价于时间序"
    assert not is_valid_id("thread", new_id("turn"))
    assert not is_valid_id("thread", "th_nothex")


def test_unknown_id_kind_is_rejected():
    with pytest.raises(KeyError):
        new_id("nope")


# --------------------------------------------------------------------------------------
# §2.2 Schema 往返 + 非法枚举 / 缺失关联字段必须失败
# --------------------------------------------------------------------------------------
def test_all_schema_files_exist_and_parse():
    assert validate_all_present() == []
    for name in SCHEMA_FILES:
        doc = load_schema(name)
        assert isinstance(doc, dict) and doc.get("$schema"), name
        json.dumps(doc)


def test_thread_turn_item_round_trip_through_schema():
    th = _thread()
    turn = _turn(th.thread_id)
    item = Item(
        item_id=new_id("item"),
        thread_id=th.thread_id,
        turn_id=turn.turn_id,
        type=ItemType.ASSISTANT_TEXT,
        created_at=_now(),
        sequence=3,
        payload={"text": "hello"},
    )
    for name, obj in (("thread", th), ("turn", turn), ("items", item)):
        data = obj.to_dict()
        validator_for(name).check(data, label=name)
        assert type(obj).from_dict(data) == obj, f"{name} 往返失败"
        json.dumps(data)


def test_illegal_enum_fails_on_model_and_schema_layer():
    payload = _turn("th_" + "0" * 23).to_dict()
    payload["status"] = "definitely_not_a_status"
    with pytest.raises(ContractViolation) as exc:
        Turn.from_dict(payload)
    assert "not a valid TurnStatus" in str(exc.value)

    ev = {
        "contract": CONTRACT_VERSION,
        "event_id": new_id("event"),
        "sequence": 1,
        "type": "made/up",
        "created_at": _now(),
        "thread_id": "th_" + "0" * 23,
        "payload": {},
    }
    assert validator_for("events").errors(ev), "schema 必须拒绝非法事件类型"


def test_missing_association_fields_fail():
    # 事件缺 thread_id：事件必须能关联到 Thread
    with pytest.raises(ContractViolation) as exc:
        Event.from_dict(
            {
                "contract": CONTRACT_VERSION,
                "event_id": new_id("event"),
                "sequence": 1,
                "type": EventType.TURN_STARTED.value,
                "created_at": _now(),
                "payload": {},
            }
        )
    assert "thread_id" in str(exc.value)

    # Turn 缺 thread_id
    with pytest.raises(ContractViolation):
        Turn.from_dict(
            {
                "contract": CONTRACT_VERSION,
                "turn_id": new_id("turn"),
                "status": TurnStatus.RUNNING.value,
                "created_at": _now(),
                "updated_at": _now(),
                "sequence_start": 1,
            }
        )

    # Item 缺 type
    with pytest.raises(ContractViolation):
        Item.from_dict(
            {
                "contract": CONTRACT_VERSION,
                "item_id": new_id("item"),
                "thread_id": "th_" + "0" * 23,
                "created_at": _now(),
                "sequence": 1,
            }
        )


def test_unknown_field_is_rejected_in_strict_mode_but_allowed_when_opting_out():
    data = _thread().to_dict()
    data["future_field"] = 1
    with pytest.raises(ContractViolation) as exc:
        Thread.from_dict(data)
    assert "unexpected field" in str(exc.value)
    # 显式放宽时允许（供未来小版本前向读取）
    assert Thread.from_dict(data, strict=False).thread_id == data["thread_id"]


def test_contract_version_is_enforced():
    data = _thread().to_dict()
    data["contract"] = "agent.v2.contract.v99"
    with pytest.raises(ContractViolation):
        Thread.from_dict(data)


def test_event_sequence_must_be_positive():
    with pytest.raises(ContractViolation):
        Event(
            event_id=new_id("event"),
            sequence=0,
            type=EventType.TURN_STARTED.value,
            created_at=_now(),
            thread_id="th_" + "0" * 23,
        )


# --------------------------------------------------------------------------------------
# §2.3 事件必备元信息
# --------------------------------------------------------------------------------------
def test_event_carries_version_sequence_time_association_and_idempotency():
    th = _thread()
    turn = _turn(th.thread_id)
    ev = Event(
        event_id=new_id("event"),
        sequence=7,
        type=EventType.TOOL_STARTED.value,
        created_at=_now(),
        thread_id=th.thread_id,
        turn_id=turn.turn_id,
        item_id=new_id("item"),
        call_id=new_id("call"),
        idempotency_key="run-7",
        payload={"name": "read_file"},
    )
    data = ev.to_dict()
    assert data["contract"] == CONTRACT_VERSION
    assert data["sequence"] == 7
    assert data["thread_id"] == th.thread_id
    assert data["turn_id"] == turn.turn_id
    assert data["call_id"]
    assert data["idempotency_key"] == "run-7"
    validator_for("events").check(data)
    assert Event.from_dict(data) == ev
    # 时间字段必须可解析
    datetime.strptime(data["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")


# --------------------------------------------------------------------------------------
# §2.4 ToolCall 统一结构（输入 / 输出 / 失败 / 取消）
# --------------------------------------------------------------------------------------
def test_toolcall_unified_structure_covers_success_failure_timeout_cancel():
    th, turn = _thread(), None
    turn = _turn(th.thread_id)
    common = dict(
        call_id=new_id("call"),
        name="write_file",
        kind=ToolKind.SIDE_EFFECT,
        thread_id=th.thread_id,
        turn_id=turn.turn_id,
    )
    variant = validator_for("tool_call")

    requested = ToolCall(status=ToolCallStatus.REQUESTED, arguments={"path": "a.txt"}, **common)
    variant.check(requested.to_dict())

    succeeded = ToolCall(status=ToolCallStatus.SUCCEEDED, output={"bytes": 3}, **common)
    variant.check(succeeded.to_dict())
    assert succeeded.error is None

    failed = ToolCall(
        status=ToolCallStatus.FAILED, error={"code": "io_error", "message": "disk full"}, **common
    )
    variant.check(failed.to_dict())
    assert failed.output is None

    timed_out = ToolCall(
        status=ToolCallStatus.TIMEOUT, error={"code": "timeout", "message": "12s"}, **common
    )
    variant.check(timed_out.to_dict())

    cancelled = ToolCall(
        status=ToolCallStatus.CANCELLED, error={"code": "cancelled", "message": "user"}, **common
    )
    variant.check(cancelled.to_dict())

    bad_args = ToolCall(
        status=ToolCallStatus.INVALID_ARGUMENTS,
        error={"code": "invalid_arguments", "message": "missing path"},
        **common,
    )
    variant.check(bad_args.to_dict())

    # 失败态必须带错误详情，成功态不得带错误
    with pytest.raises(ContractViolation):
        ToolCall(status=ToolCallStatus.FAILED, **common)
    with pytest.raises(ContractViolation):
        ToolCall(
            status=ToolCallStatus.SUCCEEDED,
            error={"code": "x", "message": "y"},
            **common,
        )


def test_tool_spec_declares_parallelism_policy():
    spec_variant = SchemaValidator.branch("tool_call", "$defs/toolSpec")
    spec = ToolSpec(name="read_file", kind=ToolKind.READ_ONLY)
    spec_variant.check(spec.to_dict())
    assert not spec_variant.errors({"name": "read_file", "kind": "read_only"})
    assert spec.kind is ToolKind.READ_ONLY
    with pytest.raises(ContractViolation):
        ToolSpec(name="x", kind="sometimes")  # type: ignore[arg-type]
    # 未知 kind 也不得通过 schema
    assert spec_variant.errors({"name": "x", "kind": "sometimes"})


# --------------------------------------------------------------------------------------
# §2.5 schema 与 Python 枚举零漂移
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "schema_name,field_path,enum_cls",
    [
        ("events", "type", EventType),
        ("turn", "status", TurnStatus),
        ("items", "type", ItemType),
        ("toolKind", "toolKind", ToolKind),
        ("toolCallStatus", "toolCallStatus", ToolCallStatus),
        ("memory", "scope", MemoryScope),
        ("memory", "origin", MemoryOrigin),
    ],
)
def test_schema_enum_matches_python_enum(schema_name, field_path, enum_cls):
    if schema_name == "toolKind":
        doc = load_schema("tool_call")
    elif schema_name == "toolCallStatus":
        doc = load_schema("tool_call")
    else:
        doc = load_schema(schema_name)
    in_schema = set(schema_enum(doc, field_path) or [])
    in_python = {m.value for m in enum_cls}
    assert in_schema == in_python, (
        f"{enum_cls.__name__} 枚举漂移: schema-only={sorted(in_schema - in_python)} "
        f"py-only={sorted(in_python - in_schema)}"
    )


def test_model_stream_schema_covers_every_stream_kind():
    doc = load_schema("model_stream")
    consts = set()
    for name, sub in (doc.get("$defs") or {}).items():
        if name == "modelRequest":
            continue
        kind = ((sub.get("properties") or {}).get("kind") or {}).get("const")
        if kind:
            consts.add(kind)
    assert consts == {m.value for m in ModelStreamKind}, sorted(consts)

    error_classes = set(
        doc["$defs"]["error"]["properties"]["error_class"]["enum"]
    )
    assert error_classes == {m.value for m in ErrorClass}


def test_every_stream_item_round_trips_through_schema():
    variant = validator_for("model_stream")
    items = [
        TextDelta("hi"),
        reasoning_response("因为", "所以")[0],
        ToolCallDelta(call_id="c1", name="read_file", arguments_delta="{"),
        ToolCallCompleted(call_id="c1", name="read_file", arguments={"p": "a"}),
        Usage(input_tokens=1, output_tokens=2),
        StreamCompleted(stop_reason=StopReason.TOOL_USE),
        StreamError(error_class=ErrorClass.RETRYABLE, message="429"),
    ]
    for item in items:
        variant.check(item.to_dict(), label=item.kind)
    # 便利构造器的产物也必须合规
    for item in text_response("x") + tool_call_response("read_file", {"p": "a"}, call_id="c1"):
        variant.check(item.to_dict(), label=item.kind)
    for item in error_response(ErrorClass.FATAL, "boom"):
        variant.check(item.to_dict())


def test_model_request_and_memory_record_validate():
    req = ModelRequest(request_id="r1", messages=[{"role": "user", "content": "hi"}])
    SchemaValidator.branch("model_stream", "$defs/modelRequest").check(req.to_dict())
    mem = MemoryRecord(
        memory_id=new_id("memory"),
        scope=MemoryScope.CONVERSATION,
        scope_id="th_" + "0" * 23,
        text="记录",
        version=2,
        created_at=_now(),
        updated_at=_now(),
        origin=MemoryOrigin.AUTO,
        source_event_ids=[new_id("event")],
    )
    validator_for("memory").check(mem.to_dict())
    assert MemoryRecord.from_dict(mem.to_dict()) == mem


# --------------------------------------------------------------------------------------
# §2.6 状态机封闭性
# --------------------------------------------------------------------------------------
def test_turn_state_machine_is_total_and_terminals_are_closed():
    assert set(TURN_TRANSITIONS) == {m for m in TurnStatus}, "迁移表必须覆盖全部状态"
    for terminal in TERMINAL_TURN_STATUSES:
        assert TURN_TRANSITIONS[terminal] == frozenset(), f"{terminal} 必须是终态"
    for status, allowed in TURN_TRANSITIONS.items():
        assert status not in allowed, f"{status} 不允许自迁移"


def test_illegal_transitions_are_rejected_by_the_predicate():
    assert not can_transition(TurnStatus.COMPLETED, TurnStatus.RUNNING)
    assert not can_transition(TurnStatus.FAILED, TurnStatus.RUNNING)
    assert not can_transition(TurnStatus.QUEUED, TurnStatus.COMPLETED)
    # 中断后不得直接置成功（验收书 §3）
    assert not can_transition(TurnStatus.INTERRUPTED, TurnStatus.COMPLETED)
    assert can_transition(TurnStatus.INTERRUPTED, TurnStatus.RUNNING)
    assert can_transition(TurnStatus.RUNNING, TurnStatus.WAITING_APPROVAL)
    assert can_transition(TurnStatus.WAITING_APPROVAL, TurnStatus.RUNNING)


def test_approval_request_schema_and_enum():
    th = _thread()
    turn = _turn(th.thread_id)
    ap = ApprovalRequest(
        approval_id=new_id("approval"),
        thread_id=th.thread_id,
        turn_id=turn.turn_id,
        created_at=_now(),
        status="pending",
        action={"tool": "run_command", "command": "rm -rf /", "risk": "high"},
        risk="high",
    )
    validator_for("approval_request").check(ap.to_dict())
    with pytest.raises(ContractViolation):
        ApprovalRequest(
            approval_id=new_id("approval"),
            thread_id=th.thread_id,
            turn_id=turn.turn_id,
            created_at=_now(),
            status="maybe",
            action={},
        )
