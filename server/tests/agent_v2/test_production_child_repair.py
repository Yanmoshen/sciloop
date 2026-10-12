"""A production child must run and publish its actual result."""
import asyncio

from api.v2.agent.production_host import ProductionRuntimeHost
from contracts.agent_v2 import FakeClock, StreamCompleted, TextDelta
from contracts.agent_v2.enums import ToolCallStatus, ToolKind
from contracts.agent_v2.ids import new_id
from contracts.agent_v2.models import ToolCall
from services.agent_memory_v2 import MemoryStore
from services.agent_threads_v2 import AgentTree, ThreadRepository


def test_production_spawn_schedules_child_and_send_restarts_idle_child(tmp_path, monkeypatch):
    async def answer(self, request, cancel=None):
        yield TextDelta("项目提供知识库和科研助手。")
        yield StreamCompleted("end_turn")

    monkeypatch.setattr("services.model_gateway_v2.provider.RegisteredModelProvider.stream", answer)
    repo = ThreadRepository(tmp_path / "conversations")
    parent = repo.create_thread("说明整理", cwd=str(tmp_path))
    parent_turn = repo.start_turn(parent.thread_id, inputs=[{"text": "请整理平台说明"}])
    tree = AgentTree(repo)
    host = ProductionRuntimeHost(repo=repo, tree=tree, memory=MemoryStore(tmp_path / "memory"), clock=FakeClock())

    def call(name, args):
        return ToolCall(call_id=new_id("call"), name=name, arguments=args,
                        thread_id=parent.thread_id, turn_id=parent_turn.turn_id,
                        kind=ToolKind.SIDE_EFFECT, status=ToolCallStatus.REQUESTED)

    async def journey():
        bundle = host.runtime_for(parent.thread_id).bundle
        spawn_call = call("agent.spawn", {"name": "说明摘要", "task": "说明平台用途"})
        approval = repo.wait_for_approval(parent.thread_id, parent_turn.turn_id,
            action={"tool": spawn_call.name, "arguments": spawn_call.arguments}, call_id=spawn_call.call_id)
        assert bundle.resolve_approval(approval.approval_id, "approve_once")["ok"]
        spawned = await bundle.execute(spawn_call)
        assert spawned.status is ToolCallStatus.SUCCEEDED
        child_id = spawned.output["agent_id"]
        await host.await_idle(child_id)
        assert tree.child_status(child_id) == "completed"
        assert repo.state(child_id).thread.cwd == str(tmp_path)
        outcomes = await tree.wait_for([child_id], timeout_s=1)
        assert "知识库" in outcomes[child_id].summary
        old_count = len(repo.state(child_id).turn_order)
        send_call = call("agent.send", {"agent_id": child_id, "content": "再缩短成一句话"})
        approval = repo.wait_for_approval(parent.thread_id, parent_turn.turn_id,
            action={"tool": send_call.name, "arguments": send_call.arguments}, call_id=send_call.call_id)
        assert bundle.resolve_approval(approval.approval_id, "approve_once")["ok"]
        sent = await bundle.execute(send_call)
        assert sent.status is ToolCallStatus.SUCCEEDED
        await host.await_idle(child_id)
        assert len(repo.state(child_id).turn_order) == old_count + 1
        assert tree.child_status(child_id) == "completed"
        await host.shutdown()

    asyncio.run(journey())
