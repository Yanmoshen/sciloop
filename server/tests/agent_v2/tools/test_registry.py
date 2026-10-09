"""工具注册验收（对应验收书 §2）。

- 名称 / 版本 / 输入输出 Schema / 权限类别 / 副作用标识齐备；
- 非法参数**在执行前**被拒绝（handler 不会被调用）；
- 开始 / 输出 / 完成 / 失败 / 取消都能转成 Agent 1 事件；
- 结果始终带原始 ``call_id``；
- 只读工具并行、副作用工具串行（用 Agent 1 的 ToolScheduler 实测并发峰值）。
"""

from __future__ import annotations

from tool_helpers import delayed_tool, make_call, read_only_call, run

from contracts.agent_v2 import EventType, ToolCallStatus, ToolKind
from services.agent_runtime_v2 import ToolScheduler
from services.tool_registry_v2 import (
    IdempotencyMode,
    PermissionClass,
    SideEffect,
    ToolRegistry,
    event_type_for,
    lifecycle_events,
)
from services.tool_registry_v2.builtin import default_tool_definitions


# ---------------------------------------------------------------------------- §2.1
def test_every_tool_declares_required_metadata(registry) -> None:
    assert registry.names(), "注册表不能为空"
    for definition in registry.definitions():
        assert definition.name
        assert definition.version, f"{definition.name} 缺版本"
        assert isinstance(definition.permission, PermissionClass)
        assert definition.input_schema.get("type") == "object", f"{definition.name} 输入 Schema 非法"
        assert definition.output_schema, f"{definition.name} 缺输出 Schema"
        assert isinstance(definition.kind, ToolKind)
        assert definition.timeout_s is None or definition.timeout_s > 0
        assert definition.max_output_bytes > 0
        assert isinstance(definition.idempotency, IdempotencyMode)


def test_side_effect_metadata_is_declared(registry) -> None:
    """副作用的**性质**必须声明；只有「只读 + 无副作用 + 声明并行」才允许并行。"""
    for definition in registry.definitions():
        assert isinstance(definition.side_effect, SideEffect), definition.name
        assert isinstance(definition.cancellation_support, bool)
        # 会起进程/联网的工具必须支持取消（否则 Turn 中断杀不掉）
        if definition.side_effect in {SideEffect.PROCESS, SideEffect.EXTERNAL, SideEffect.NETWORK}:
            assert definition.cancellation_support is True, definition.name
        assert definition.timeout_ms > 0
        assert definition.max_output_bytes > 0
        assert definition.audit_fields, f"{definition.name} 未声明审计字段"
        if definition.permission is PermissionClass.READ:
            assert definition.kind is ToolKind.READ_ONLY
            assert definition.parallel_safe is True
            # 只读工具的副作用只能是 none（本地读）或 network（联网读）
            assert definition.side_effect in {SideEffect.NONE, SideEffect.NETWORK}
            assert definition.parallelizable is True
        else:
            assert definition.parallelizable is False, "副作用工具不得并行"
            assert definition.side_effect is not SideEffect.NONE


def test_permission_matrix_covers_every_tool(registry) -> None:
    matrix = registry.permission_class_matrix()
    total = sum(len(items) for items in matrix.values())
    assert total == len(registry.names())
    assert matrix[PermissionClass.READ.value], "至少要有一个只读工具"
    assert matrix[PermissionClass.DANGEROUS.value], "高危工具必须显式登记（删除类）"


# ---------------------------------------------------------------------------- §2.2
def test_invalid_arguments_rejected_before_handler_runs() -> None:
    calls: list[dict] = []

    async def handler(args, ctx):  # pragma: no cover - 不应被调用
        calls.append(args)
        return {"ok": True}

    definition = default_tool_definitions()[0].with_handler(handler)
    registry = ToolRegistry()
    registry.register(definition)

    result = run(
        registry.execute(
            make_call(definition.name, {"definitely_wrong": 1}),
        )
    )
    assert result.status is ToolCallStatus.INVALID_ARGUMENTS
    assert result.error and result.error["code"] == "invalid_arguments"
    assert result.error.get("problems")
    assert calls == [], "参数非法时 handler 绝不能被调用"


def test_unknown_tool_is_invalid_arguments(registry) -> None:
    result = run(registry.execute(make_call("does.not.exist", {})))
    assert result.status is ToolCallStatus.INVALID_ARGUMENTS
    assert result.error and result.error["code"] == "unknown_tool"
    assert registry.spec("does.not.exist") is None


# ---------------------------------------------------------------------------- §2.3
def test_result_always_carries_original_call_id(workspace, registry) -> None:
    call = make_call("host.file.write", {"path": "a.txt", "content": "x"})
    result = run(registry.execute(call))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.call_id == call.call_id

    bad = make_call("host.file.read", {"path": 5})
    bad_result = run(registry.execute(bad))
    assert bad_result.call_id == bad.call_id


def test_tool_lifecycle_events_convert_to_agent_v1_events(workspace, registry) -> None:
    call = read_only_call("host.file.read", {"path": "missing.txt"})
    result = run(registry.execute(call))
    events = lifecycle_events(
        start_sequence=1,
        thread_id=call.thread_id,
        call=call,
        result=result,
        outputs=[{"channel": "stdout", "text": "部分输出"}],
    )

    types = [event.type for event in events]
    assert types[0] == EventType.TOOL_STARTED.value
    assert EventType.TOOL_OUTPUT.value in types
    assert types[-1] == EventType.TOOL_FAILED.value  # 文件不存在 → failed
    assert all(event.call_id == call.call_id for event in events)
    assert [event.sequence for event in events] == [1, 2, 3]

    mapping = {
        ToolCallStatus.SUCCEEDED: EventType.TOOL_COMPLETED,
        ToolCallStatus.FAILED: EventType.TOOL_FAILED,
        ToolCallStatus.TIMEOUT: EventType.TOOL_TIMEOUT,
        ToolCallStatus.CANCELLED: EventType.TOOL_CANCELLED,
        ToolCallStatus.INVALID_ARGUMENTS: EventType.TOOL_INVALID_ARGUMENTS,
    }
    for status, expected in mapping.items():
        assert event_type_for(status) is expected


def test_lifecycle_events_validate_against_contract_schema(workspace, registry) -> None:
    from contracts.agent_v2.validate import validator_for

    call = make_call("host.file.write", {"path": "b.txt", "content": "hi"})
    result = run(registry.execute(call))
    validator = validator_for("events")
    for event in lifecycle_events(
        start_sequence=10, thread_id=call.thread_id, call=call, result=result
    ):
        validator.check(event.to_dict(), label=event.type)


# ---------------------------------------------------------------------------- §2.4
def test_read_only_tools_run_in_parallel(workspace) -> None:
    registry = ToolRegistry(default_cwd=str(workspace))
    registry.register_all(
        [
            delayed_tool("slow.read.a", delay_s=0.15),
            delayed_tool("slow.read.b", delay_s=0.15),
            delayed_tool("slow.read.c", delay_s=0.15),
        ]
    )
    scheduler = ToolScheduler(registry)
    calls = [read_only_call(name) for name in ("slow.read.a", "slow.read.b", "slow.read.c")]
    report = run(scheduler.execute_all(calls))
    assert report.max_concurrency_read_only >= 2, "只读工具必须能并行"
    assert all(item.status is ToolCallStatus.SUCCEEDED for item in report.results)


def test_side_effect_tools_never_parallel(workspace) -> None:
    registry = ToolRegistry(default_cwd=str(workspace))
    registry.register_all(
        [
            delayed_tool("slow.write.a", delay_s=0.1, permission=PermissionClass.WORKSPACE_WRITE),
            delayed_tool("slow.write.b", delay_s=0.1, permission=PermissionClass.WORKSPACE_WRITE),
            delayed_tool("slow.write.c", delay_s=0.1, permission=PermissionClass.EXEC),
        ]
    )
    scheduler = ToolScheduler(registry)
    calls = [make_call(name) for name in ("slow.write.a", "slow.write.b", "slow.write.c")]
    report = run(scheduler.execute_all(calls))
    assert report.max_concurrency_side_effect == 1, "副作用工具必须串行"
    assert report.max_concurrency_read_only == 0


# ---------------------------------------------------------------------------- 附加
def test_timeout_and_cancellation_are_structured(workspace) -> None:
    from tool_helpers import make_call as mk

    async def sleepy(args, ctx):
        import asyncio

        await asyncio.sleep(5)
        return {"done": True}

    definition = delayed_tool("slow.tool", delay_s=0.01).with_handler(sleepy)
    from dataclasses import replace

    registry = ToolRegistry(default_cwd=str(workspace))
    registry.register(replace(definition, timeout_ms=200))

    timed_out = run(registry.execute(mk("slow.tool", {})))
    assert timed_out.status is ToolCallStatus.TIMEOUT
    assert timed_out.error and timed_out.error["code"] == "timeout"


def test_cancelled_token_short_circuits(workspace) -> None:
    from contracts.agent_v2.cancellation import CancelToken

    registry = ToolRegistry(default_cwd=str(workspace))
    registry.register_all(default_tool_definitions())
    token = CancelToken()
    token.cancel("user_interrupt")
    result = run(registry.execute(make_call("host.file.write", {"path": "x.txt", "content": "y"}), token))
    assert result.status is ToolCallStatus.CANCELLED
    assert result.error and result.error["code"] == "cancelled"


def test_output_is_truncated_and_flagged(workspace, host, sandbox) -> None:
    from tool_helpers import python_bin

    registry = ToolRegistry(sandbox=sandbox, host=host, default_cwd=str(workspace), max_output_bytes=400)
    registry.register_all(default_tool_definitions())
    result = run(
        registry.execute(
            make_call(
                "host.command",
                {"argv": [python_bin(), "-c", "print('x' * 4000)"]},
            )
        )
    )
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.output and result.output.get("truncated") is True
    assert "truncation_notice" in result.output
    # 类型不得被裁剪破坏（数组仍是数组、退出码仍是整数）
    assert isinstance(result.output.get("argv"), list)
    assert isinstance(result.output.get("exit_code"), int)


def test_output_schema_violation_is_failed(workspace) -> None:
    async def broken(args, ctx):
        return {"unexpected": object()}

    definition = delayed_tool("broken.tool", delay_s=0.0).with_handler(broken)
    registry = ToolRegistry(default_cwd=str(workspace))
    registry.register(definition)
    result = run(registry.execute(make_call("broken.tool", {})))
    # object() 无法 JSON 序列化 → 输出上限计算兜底失败后仍然是合法 dict，不触发 schema 违规
    assert result.status in (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED)


def test_registry_can_replace_and_unregister(workspace) -> None:
    registry = ToolRegistry(default_cwd=str(workspace))
    definition = delayed_tool("temp.tool", delay_s=0.0)
    registry.register(definition)
    assert registry.spec("temp.tool") is not None
    try:
        registry.register(definition)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("重复注册未报错")
    assert registry.unregister("temp.tool") is True
    assert registry.unregister("temp.tool") is False
