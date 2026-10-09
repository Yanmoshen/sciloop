"""agent.v2 运行时测试的公共脚手架（夹具与打包器）。

为什么需要它：契约/事件/线程/运行时/工具/树/压缩/记忆这些测试都要搭同一套
「仓库 + 假模型 + 假工具」的栈；把搭建逻辑集中在一处，测试本身才能只表达
「被测断言」，而不是重复 30 行样板。

注意：本机未安装 pytest-asyncio，因此所有异步用例统一用 :func:`run` 包一层
``asyncio.run``，不依赖任何插件。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[3]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from contracts.agent_v2 import (  # noqa: E402
    CancelToken,
    FakeClock,
    FakeProvider,
    FakeToolExecutor,
    StreamItem,
    Thread,
    ToolSpec,
    Turn,
    text_response,
)
from services.agent_compaction_v2 import CompactionService  # noqa: E402
from services.agent_memory_v2 import MemoryStore  # noqa: E402
from services.agent_runtime_v2 import ToolScheduler, TurnOutcome, TurnRuntime  # noqa: E402
from services.agent_threads_v2 import AgentTree, ThreadRepository  # noqa: E402
from services.model_gateway_v2 import ModelGateway, RetryPolicy  # noqa: E402


def run(coro: Any) -> Any:
    """同步执行一个协程（本机未装 pytest-asyncio）。"""
    return asyncio.run(coro)


@dataclass
class Harness:
    """一套完整的离线运行栈。"""

    root: Path
    clock: FakeClock
    repo: ThreadRepository
    executor: FakeToolExecutor
    scheduler: ToolScheduler
    tree: AgentTree
    memory: MemoryStore

    @classmethod
    def create(cls, tmp_path: Path, *, conversations: str = "conversations") -> Harness:
        clock = FakeClock()
        root = tmp_path / conversations
        root.mkdir(parents=True, exist_ok=True)
        repo = ThreadRepository(root, clock=clock)
        executor = FakeToolExecutor()
        scheduler = ToolScheduler(executor, clock=clock)
        tree = AgentTree(repo, clock=clock)
        memory = MemoryStore(tmp_path / "memories", clock=clock)
        return cls(
            root=root,
            clock=clock,
            repo=repo,
            executor=executor,
            scheduler=scheduler,
            tree=tree,
            memory=memory,
        )

    # ---- 基础 ----
    def thread(self, name: str = "测试线程") -> Thread:
        return self.repo.create_thread(name)

    def store_for(self, thread_id: str):
        return self.repo.store(thread_id)

    def event_types(self, thread_id: str, *, turn_id: str | None = None) -> list[str]:
        return [
            e.type
            for e in self.store_for(thread_id).read_all()
            if turn_id is None or e.turn_id == turn_id
        ]

    # ---- 运行时 ----
    def provider(self, script: Sequence[Sequence[StreamItem]]) -> FakeProvider:
        return FakeProvider(script=[list(batch) for batch in script])

    def runtime(
        self,
        provider: FakeProvider,
        *,
        compaction: CompactionService | None = None,
        retry: RetryPolicy | None = None,
        **kw: Any,
    ) -> TurnRuntime:
        return TurnRuntime(
            repo=self.repo,
            gateway=ModelGateway(provider, clock=self.clock),
            tools=self.scheduler,
            compaction=compaction,
            clock=self.clock,
            retry=retry,
            **kw,
        )

    def start(self, thread_id: str, text: str, *, key: str | None = None) -> Turn:
        return self.repo.start_turn(thread_id, inputs=[{"text": text}], idempotency_key=key)

    def play(
        self,
        thread_id: str,
        text: str,
        script: Sequence[Sequence[StreamItem]],
        *,
        key: str | None = None,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
        compaction: CompactionService | None = None,
        **kw: Any,
    ) -> tuple[TurnOutcome, FakeProvider, Turn]:
        """起一个 Turn 并跑完（最常用的三行组合）。"""
        turn = self.start(thread_id, text, key=key)
        provider = self.provider(script)
        runtime = self.runtime(provider, compaction=compaction, **kw)
        outcome = run(runtime.run(thread_id, turn.turn_id, cancel=cancel, timeout_s=timeout_s))
        return outcome, provider, self.repo.state(thread_id).turns[turn.turn_id]

    def text_script(self, *texts: str) -> list[list[StreamItem]]:
        return [text_response(t) for t in texts]

    # ---- 压缩 ----
    def compaction(
        self, provider: FakeProvider, *, token_budget: int = 6000, **kw: Any
    ) -> CompactionService:
        return CompactionService(
            repo=self.repo,
            gateway=ModelGateway(provider, clock=self.clock),
            clock=self.clock,
            token_budget=token_budget,
            **kw,
        )

    # ---- 断言助手 ----
    def assert_sequence_is_contiguous(self, thread_id: str) -> None:
        seqs = [e.sequence for e in self.store_for(thread_id).read_all()]
        assert seqs == list(range(1, len(seqs) + 1)), "事件序号必须从 1 起连续、不跳号"

    def assert_no_lease_leak(self) -> None:
        assert self.repo.held_lease_count() == 0, "取消/超时/失败路径不得遗留活动 Turn 锁"


__all__ = ["Harness", "run", "asyncio", "field"]

# 便于测试直接引用常用类型
CALL_ID = "call_" + "0" * 23


def make_tool_specs(*, read_only: Sequence[str] = ("read_file",), side_effect: Sequence[str] = ("write_file",)) -> list[ToolSpec]:
    from contracts.agent_v2 import ToolKind

    specs = [ToolSpec(name=n, kind=ToolKind.READ_ONLY) for n in read_only]
    specs += [ToolSpec(name=n, kind=ToolKind.SIDE_EFFECT) for n in side_effect]
    return specs
