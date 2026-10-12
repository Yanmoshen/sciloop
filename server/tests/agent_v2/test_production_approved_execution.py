"""Real approval-to-execution coverage for the production tool seam."""

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


def _setup(tmp_path: Path, *, full_access: bool = False):
    repo = ThreadRepository(tmp_path / "conversations")
    thread = repo.create_thread("approved execution", cwd=str(tmp_path), settings={"full_access": full_access})
    bundle = build_tool_bundle(
        repo, AgentTree(repo), MemoryStore(tmp_path / "memories"), thread.thread_id,
        HostExecutionManager(tmp_path / "executions"),
    )
    turn = repo.start_turn(thread.thread_id, inputs=[{"text": "执行操作"}])
    return repo, thread, turn, bundle


def _call(thread_id: str, turn_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(
        call_id=new_id("call"), name=name, arguments=arguments,
        thread_id=thread_id, turn_id=turn_id,
        kind=ToolKind.SIDE_EFFECT if name.startswith("host.") else ToolKind.READ_ONLY,
        status=ToolCallStatus.REQUESTED,
    )


def _approve(repo, bundle, call: ToolCall, *, decision: str = "approve_once"):
    approval = repo.wait_for_approval(
        call.thread_id, call.turn_id,
        action={"tool": call.name, "arguments": call.arguments}, call_id=call.call_id,
    )
    assert bundle.approval_gate(call) == "require"
    assert bundle.resolve_approval(approval.approval_id, decision)["ok"]


def test_approve_once_executes_external_write_and_is_idempotent(tmp_path: Path) -> None:
    repo, thread, turn, bundle = _setup(tmp_path)
    target = tmp_path.parent / f"approved-{thread.thread_id}.txt"
    call = _call(thread.thread_id, turn.turn_id, "host.file.write", {"path": str(target), "content": "approved"})
    assert bundle.approval_gate(call) == "require"
    assert not target.exists()
    _approve(repo, bundle, call)
    result = asyncio.run(bundle.execute(call))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert target.read_text(encoding="utf-8") == "approved"
    # Repeating the same identity returns the persisted execution outcome and
    # does not alter the file or create a second process/result.
    again = asyncio.run(bundle.execute(call))
    assert again.status is ToolCallStatus.SUCCEEDED


def test_approved_command_runs_outside_workspace_and_records_policy(tmp_path: Path) -> None:
    repo, thread, turn, bundle = _setup(tmp_path)
    target = tmp_path.parent / f"approved-cmd-{thread.thread_id}.txt"
    code = f"from pathlib import Path; Path(r'{target}').write_text('ok', encoding='utf-8')"
    call = _call(thread.thread_id, turn.turn_id, "host.command", {"argv": ["python", "-c", code]})
    _approve(repo, bundle, call)
    result = asyncio.run(bundle.execute(call))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert target.read_text(encoding="utf-8") == "ok"


def test_granted_call_cannot_change_arguments_or_cross_thread(tmp_path: Path) -> None:
    repo, thread, turn, bundle = _setup(tmp_path)
    target = tmp_path.parent / f"mismatch-{thread.thread_id}.txt"
    call = _call(thread.thread_id, turn.turn_id, "host.file.write", {"path": str(target), "content": "a"})
    _approve(repo, bundle, call)
    changed = _call(thread.thread_id, turn.turn_id, "host.file.write", {"path": str(target), "content": "b"})
    changed.call_id = call.call_id
    mismatch = asyncio.run(bundle.execute(changed))
    assert mismatch.error and mismatch.error["code"] == "approval_mismatch"
    assert not target.exists()

    foreign = _call("other-thread", turn.turn_id, "host.file.write", {"path": str(target), "content": "c"})
    foreign.call_id = call.call_id
    mismatch = asyncio.run(bundle.execute(foreign))
    assert mismatch.error and mismatch.error["code"] == "approval_mismatch"


def test_full_access_executes_same_operation_without_approval(tmp_path: Path) -> None:
    _repo, thread, turn, bundle = _setup(tmp_path, full_access=True)
    target = tmp_path.parent / f"full-{thread.thread_id}.txt"
    call = _call(thread.thread_id, turn.turn_id, "host.file.write", {"path": str(target), "content": "full"})
    assert bundle.approvals.full_access is True
    assert bundle.approval_gate(call) == "allow"
    result = asyncio.run(bundle.execute(call))
    assert result.status is ToolCallStatus.SUCCEEDED
    assert target.read_text(encoding="utf-8") == "full"
