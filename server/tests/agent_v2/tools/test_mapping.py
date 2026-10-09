"""工具清单与映射表验收（对应计划书 §4.1「必须提供旧能力到新能力的映射表」）。

同时把权限矩阵本身作为回归基线：类别 / 并行 / 幂等 / 超时任何一项被改都要在测试里显式体现。
"""

from __future__ import annotations

from services.tool_registry_v2 import (
    LEGACY_TOOLS,
    NEW_ONLY_TOOLS,
    IdempotencyMode,
    PermissionClass,
    legacy_tools_for,
    mapping_table,
    new_tools_for,
)
from services.tool_registry_v2.builtin import default_tool_definitions


def test_every_legacy_tool_is_mapped() -> None:
    for legacy in LEGACY_TOOLS:
        assert new_tools_for(legacy), f"旧工具 {legacy} 没有映射到任何新工具"
    # 反向也要能查（集成阶段需要）
    assert legacy_tools_for("knowledge.search") == ("query_library",)
    assert set(legacy_tools_for("host.command")) == {"run_command", "run_on_computer"}
    assert set(legacy_tools_for("search.query")) == {"search_academic", "search_web"}


def test_mapping_table_is_deliverable_ready() -> None:
    rows = mapping_table()
    assert rows
    assert any(row["direction"] == "replaced" for row in rows)
    assert any(row["direction"] == "new" for row in rows)
    assert any("notes" in row for row in rows), "映射表要带说明"


def test_required_capabilities_are_provided(registry) -> None:
    names = set(registry.names())
    required = {
        # 宿主机命令
        "host.command",
        # 宿主机文件
        "host.file.list",
        "host.file.read",
        "host.file.write",
        "host.file.move",
        "host.file.delete",
        # 搜索与抓取
        "search.query",
        "search.fetch",
        # 知识库
        "knowledge.search",
        "knowledge.write",
        # 技能
        "skill.list",
        "skill.run",
        # 子 Agent
        "agent.spawn",
        "agent.send",
        "agent.wait",
        "agent.interrupt",
        "agent.close",
        # MCP 桥接
        "mcp.call",
    }
    assert required <= names, f"缺少能力：{sorted(required - names)}"
    assert set(NEW_ONLY_TOOLS) <= names


def test_permission_matrix_baseline(registry) -> None:
    matrix = {item["name"]: item for item in registry.permission_matrix()}

    read_only = {
        "host.file.list",
        "host.file.read",
        "search.query",
        "search.fetch",
        "knowledge.search",
        "skill.list",
    }
    for name in read_only:
        assert matrix[name]["permission_class"] == PermissionClass.READ.value
        assert matrix[name]["parallelizable"] is True
        # 网络读取仍是"只读"（side_effect=network），本地只读是 none
        assert matrix[name]["side_effect"] in {"none", "network"}
        assert matrix[name]["audit_fields"]

    assert matrix["host.file.write"]["permission_class"] == PermissionClass.WORKSPACE_WRITE.value
    assert matrix["host.file.move"]["permission_class"] == PermissionClass.WORKSPACE_WRITE.value
    assert matrix["host.file.delete"]["permission_class"] == PermissionClass.DANGEROUS.value
    assert matrix["host.command"]["permission_class"] == PermissionClass.EXEC.value
    assert matrix["host.command"]["side_effect"] == "process"
    assert matrix["skill.run"]["permission_class"] == PermissionClass.EXEC.value

    for name in ("agent.spawn", "agent.send", "agent.wait", "agent.interrupt", "agent.close"):
        assert matrix[name]["permission_class"] == PermissionClass.EXEC.value
        assert matrix[name]["side_effect"] == "external"

    # 文件/进程/外部副作用一律不并行（联网读取仍可并行）
    assert all(
        item["parallelizable"] is False
        for name, item in matrix.items()
        if item["side_effect"] in {"filesystem", "process", "external"}
    )


def test_idempotency_declared_for_side_effect_tools() -> None:
    for definition in default_tool_definitions():
        if definition.permission is PermissionClass.READ:
            assert definition.idempotency is IdempotencyMode.NONE
        elif definition.name == "knowledge.write":
            assert definition.idempotency is IdempotencyMode.CALL_ID
        else:
            assert definition.idempotency in (
                IdempotencyMode.CALL_ID,
                IdempotencyMode.EXECUTION,
            ), definition.name


def test_contract_specs_projection_is_consistent(registry) -> None:
    """投影给 Agent 1 的 ToolSpec 必须与权限类别一致（只读可并行、其余串行）。"""
    from contracts.agent_v2 import ToolKind

    for spec in registry.specs():
        definition = registry.definition(spec.name)
        assert spec.kind is definition.kind
        assert spec.parameters == definition.input_schema
        if definition.permission is PermissionClass.READ:
            assert spec.kind is ToolKind.READ_ONLY
        else:
            assert spec.kind is ToolKind.SIDE_EFFECT
