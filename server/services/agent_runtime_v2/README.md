# agent_runtime_v2 接入说明（Agent 2 / Agent 3 必读）

契约版本：`agent.v2.contract.v1`　契约目录：`server/contracts/agent_v2/`（只读）

本目录是**执行内核**：不依赖 HTTP 请求、不含任何供应商协议分支、不实现具体工具。
工具能力由你（Agent 2/3）通过 :class:`ToolExecutor` 注入。

## 1. 五件套装配

```python
from services.agent_events_v2 import EventStore
from services.agent_threads_v2 import ThreadRepository
from services.agent_runtime_v2 import TurnRuntime, ToolScheduler
from services.model_gateway_v2 import ModelGateway, RetryPolicy

repo = ThreadRepository("knowledge-base/conversations")
gateway = ModelGateway(my_provider, classifier=my_error_classifier)
scheduler = ToolScheduler(my_executor)              # my_executor 实现 ToolExecutor 协议
runtime = TurnRuntime(
    repo=repo,
    gateway=gateway,
    tools=scheduler,
    compaction=compaction_service,      # 可选
    approval_gate=my_approval_gate,     # 可选：返回 ALLOW / REQUIRE
    system_prompt=my_system_prompt,
)
```

## 2. 你必须实现的两个协议

### 2.1 `ModelProvider`

```python
class MyProvider:
    async def stream(self, request: ModelRequest, cancel: CancelToken | None):
        # 你负责：鉴权、报文格式、增量解析、以及把自家异常翻译成 StreamError(error_class=...)
        yield TextDelta("...")          # 或 ReasoningDelta / ToolCallDelta / Usage / StreamCompleted / StreamError
```

**铁律**：供应商差异只能停在这一层。TurnRuntime 里没有任何供应商分支，
由 `test_model_loop.py::test_turn_runtime_has_no_vendor_specific_branches` 静态守卫。

### 2.2 `ToolExecutor`

```python
class MyExecutor:
    def specs(self) -> Sequence[ToolSpec]: ...       # kind=read_only → 并行；side_effect → 串行
    def spec(self, name) -> ToolSpec | None: ...
    async def execute(self, call: ToolCall, cancel: CancelToken | None) -> ToolResult: ...
```

- 收到取消信号时**必须**向真实进程发终止指令，然后返回 `status=cancelled`
  （验收书 §3 明确要求"中断工具时进程由执行器接口收到取消信号"）；
- 不要在协程里吞掉 `asyncio.CancelledError`（会破坏 `asyncio.wait_for` 的超时语义）。

## 3. 审批（风险自适应人机协同）

```python
def my_gate(call: ToolCall) -> str:
    return REQUIRE if is_high_risk(call) else ALLOW
```

返回 `REQUIRE` 时运行时**整批挂起**（不做半批副作用），Turn 进入 `waiting_approval`
并落 `approval/requested` 事件。研究者决策后：

```python
repo.resolve_approval(thread_id, turn_id, approval_id, granted=True, scope="conversation")
outcome = asyncio.run(runtime.run(thread_id, turn_id))   # 续跑：会先执行挂起的工具
```

拒绝（`granted=False`）也回到 `running`，拒绝结果作为 Item 进入上下文，由模型决定下一步。

## 4. 子 Agent

```python
from services.agent_threads_v2 import AgentTree
tree = AgentTree(repo)
child = tree.create_child(parent_thread_id, "文献调研子 Agent", model="...")
tree.register_cancel(child.thread_id, token)      # 让中断能下发到子线程的工具
outcomes = await tree.wait_for([child.thread_id], timeout_s=300)   # 不抛"子失败"异常
tree.report_result(child.thread_id, summary="找到 42 篇", status="completed")
```

`spawn_agent` 这类工具入口由你实现（`execute` 里调 `tree.create_child` 即可）。

## 5. 记忆与压缩

```python
from services.agent_memory_v2 import MemoryStore
memory = MemoryStore("knowledge-base/memories")
memory.write("user", user_id, "偏好中文", origin="user", source_event_ids=[event_id])
# 自动流程不得覆盖用户编辑过的记录（会抛 MemoryOverwriteDenied）——请新建记录
```

```python
from services.agent_compaction_v2 import CompactionService
svc = CompactionService(repo=repo, gateway=gateway, token_budget=6000)
if svc.should_compact(thread_id):
    result = await svc.compact(thread_id, trigger="auto")
```

## 6. 不要做的事

- 不要修改 `server/contracts/agent_v2/`（已冻结；需变更先提契约变更说明）；
- 不要直接写 `events.jsonl`（一律走 `EventStore`/`ThreadRepository`，序号与幂等由它保证）；
- 不要把历史再存一份到数据库（数据库只放索引/状态/租约）；
- 不要在运行时目录里出现供应商专有分支。

## 7. 测试与自检

```bash
# 在项目容器内（Python 3.11 + 官方 jsonschema + pytest 8.4）
docker run --rm -v "<repo>:/src" -w /src/server sciloop-backend:latest \
  python -m pytest tests/agent_v2 -q

# lint（本目录必须为零告警）
docker run --rm -v "<repo>:/src" -w /src/server sciloop-backend:latest \
  ruff check contracts services/agent_events_v2 services/agent_threads_v2 \
  services/agent_runtime_v2 services/model_gateway_v2 \
  services/agent_compaction_v2 services/agent_memory_v2 tests/agent_v2
```

测试全部离线：`FakeProvider` + `FakeToolExecutor` + `FakeClock`，不碰网络、不真等待。
