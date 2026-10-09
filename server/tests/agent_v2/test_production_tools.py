"""Production tool assembly tests (all data lives in a temporary directory)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from contracts.agent_v2.enums import ToolCallStatus, ToolKind
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import ToolCall
from services.agent_memory_v2 import MemoryStore
from services.agent_runtime_v2.production_tools import build_tool_bundle
from services.agent_threads_v2 import AgentTree, ThreadRepository
from services.host_execution_v2 import HostExecutionManager


def _bundle(tmp_path: Path):
    repo = ThreadRepository(tmp_path / "conversations")
    thread = repo.create_thread("production", cwd=str(tmp_path))
    tree = AgentTree(repo)
    memory = MemoryStore(tmp_path / "memories")
    host = HostExecutionManager(tmp_path / "executions")
    return repo, thread, build_tool_bundle(repo, tree, memory, thread.thread_id, host)


def _call(thread_id: str, name: str, arguments: dict, *, turn_id: str = "turn_test") -> ToolCall:
    return ToolCall(
        call_id=new_id("call"),
        name=name,
        kind=ToolKind.SIDE_EFFECT if name.startswith("host.command") else ToolKind.READ_ONLY,
        status=ToolCallStatus.REQUESTED,
        thread_id=thread_id,
        turn_id=turn_id,
        arguments=arguments,
    )


def test_bundle_exposes_real_tools_and_file_operations(tmp_path: Path) -> None:
    repo, thread, bundle = _bundle(tmp_path)
    assert "host.command" in bundle.registry.names()
    assert "knowledge.write" in bundle.registry.names()
    path = tmp_path / "note.txt"

    write = _call(thread.thread_id, "host.file.write", {"path": str(path), "content": "hello"})
    result = asyncio.run(bundle.execute(write))
    assert result.status is ToolCallStatus.SUCCEEDED
    read = _call(thread.thread_id, "host.file.read", {"path": str(path)})
    read_result = asyncio.run(bundle.execute(read))
    assert read_result.status is ToolCallStatus.SUCCEEDED
    assert read_result.output and read_result.output["text"] == "hello"


def test_host_command_uses_process_manager_and_emits_incremental_events(tmp_path: Path) -> None:
    repo, thread, bundle = _bundle(tmp_path)
    command = _call(thread.thread_id, "host.command", {"argv": ["python", "-c", "print('ok')"]})
    # A harmless command is allowed by the risk policy.
    assert bundle.approval_gate(command) == "allow"
    result = asyncio.run(bundle.execute(command))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert result.output and "ok" in result.output["stdout"]
    events = repo.store(thread.thread_id).read_all()
    assert any(event.type == "tool/output" for event in events)


def test_approval_id_is_adopted_and_denial_is_fed_back_once(tmp_path: Path) -> None:
    repo, thread, bundle = _bundle(tmp_path)
    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "run it"}])
    call = _call(thread.thread_id, "host.command", {"argv": ["python", "-c", "print('x')"]}, turn_id=turn.turn_id)
    assert bundle.approval_gate(call) == "allow"

    # A destructive command is suspended by the gate.  Simulate Agent 1's
    # repository approval creation, then resolve it through the production
    # bundle; both stores must retain exactly this id.
    dangerous = _call(thread.thread_id, "host.command", {"argv": ["python", "-c", "open('x','w').write('x')"]}, turn_id=turn.turn_id)
    assert bundle.approval_gate(dangerous) == "require"
    approval = repo.wait_for_approval(
        thread.thread_id,
        turn.turn_id,
        action={"tool": dangerous.name, "arguments": dangerous.arguments},
        call_id=dangerous.call_id,
    )
    assert bundle.approval_gate(dangerous) == "require"
    resolved = bundle.resolve_approval(approval.approval_id, "deny")
    assert resolved["ok"] is True
    assert bundle.approval_gate(dangerous) == "allow"
    denied = asyncio.run(bundle.execute(dangerous))
    assert denied.error is None or denied.error.get("code") != "approval_required"
    # The denied operation must not start a process.
    assert not (tmp_path / "x").exists()


def test_thread_approval_uses_thread_cwd_for_continuous_grant(tmp_path: Path) -> None:
    repo, thread, bundle = _bundle(tmp_path)
    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "run it"}])
    call = _call(
        thread.thread_id,
        "host.command",
        {"argv": ["python", "-c", "open('x','w').write('x')"]},
        turn_id=turn.turn_id,
    )
    assert bundle.approval_gate(call) == "require"
    approval = repo.wait_for_approval(
        thread.thread_id,
        turn.turn_id,
        action={"tool": call.name, "arguments": call.arguments},
        call_id=call.call_id,
    )
    bundle.approval_gate(call)
    assert bundle.resolve_approval(approval.approval_id, "approve_for_thread")["ok"] is True
    assert bundle.approvals.grants.prefixes(scope_id=thread.thread_id)


def test_memory_tool_is_backed_by_memory_store(tmp_path: Path) -> None:
    repo, thread, bundle = _bundle(tmp_path)
    call = _call(thread.thread_id, "knowledge.write", {"name": "memo", "content": "persisted"})
    result = asyncio.run(bundle.execute(call))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert bundle.gateway.list_tools()
    assert bundle.registry.services["knowledge_search"]({"query": "persisted"})["count"] == 1
