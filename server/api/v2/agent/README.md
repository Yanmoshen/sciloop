# Agent v2 API 层与前端协议（Agent 3 交付说明）

本文件是 Agent 3（WP-09 WebSocket 协议与 API + WP-10 前端运行时）的**交付说明**，
与代码同目录存放，随仓库分发。计划书/验收书见 `docs/Agent3计划书-API前端与集成协议.md`、
`docs/Agent3验收书-API前端与集成协议.md`（该目录为本地未跟踪目录，不随仓库分发）。

## 1. 隔离与基线

| 项目 | 值 |
|---|---|
| 分支 | `codex/agent-ui` |
| 内容提交（本次交付） | `f3212d8`（68 个新文件 / 13096 行；已推送 `origin/codex/agent-ui`） |
| worktree | `D:\aicoding竞赛-worktrees\agent-ui` |
| 契约基线（冻结标签） | `agent-v2-contract-v1` → `6a9fa00`（`server/contracts/agent_v2/` 只读） |
| 实现基线 | `codex/agent-runtime` 的 `4d7bec5`（以 `6a9fa00` 为父提交） |
| 祖先校验 | `git merge-base --is-ancestor agent-v2-contract-v1 HEAD` → 真 |
| 是否依赖 Agent 2 | **否**。本线使用冻结契约、Agent 1 的 fake provider/tool executor 与自己的 JSONL fixture 完成全部测试 |

复现本次交付内容：`git log --oneline -1 4d7bec5..codex/agent-ui`（除元数据追加外，内容提交即 `f3212d8`）。

未修改：`server/contracts/agent_v2/`、`server/services/{agent_runtime,agent_events,agent_threads,model_gateway,agent_compaction,agent_memory}_v2/`、
`server/api/v1/`、`web/src/router/`、`web/src/layouts/`、旧对话数据与数据库迁移。

## 2. 交付物清单

### 2.1 后端（协议层 + API 层）

```text
server/api/v2/__init__.py
server/api/v2/agent_protocol/      # 协议层：与业务无关
  version.py           协议版本 agent.v2.protocol.v1
  errors.py            结构化错误码 + 领域异常映射
  envelope.py          request / response / notification 三类帧
  jsonrpc.schema.json  协议 JSON Schema（官方 jsonschema 校验）
  schema.py            schema 加载与帧校验
  methods.py           方法注册表（白名单 + 参数契约 + 幂等纪律）
  idempotency.py       客户端幂等键缓存
  subscriptions.py     线程订阅与游标推进
server/api/v2/agent/               # API 层：只做校验、转发、订阅、返回
  facade.py            AgentFacade：26 个协议方法 -> 领域调用
  host.py              离线运行时宿主（fake provider/executor、审批门、子 Agent、计划）
  service.py           服务装配、连接管理、事件推送、关闭语义
  ws.py                WebSocket 会话循环
  router.py            /health /protocol /rpc /ws
  mount.py             挂载补丁（协调 Agent 使用）
  auth.py              访问控制（复用 core.security.owner_token_matches）
  fixtures.py          JSONL fixture 加载
  fixtures/*.jsonl     10 个固定场景
```

### 2.2 前端

```text
web/src/agent-v2/protocol.ts        协议与契约类型、方法名、幂等键纪律
web/src/agent-v2/reducer.ts         纯函数状态机 + 选择器
web/src/agent-v2/index.ts           模块出口
web/src/agent-v2/README.md          挂载补丁与设计说明
web/src/api/agent-v2.ts             WebSocket 客户端（重连、游标续订、幂等键、超时）
web/src/stores/agent-v2.ts          Pinia store（状态 + 动作 + 选择器）
web/src/components/agent-v2/        ConnectionBanner / TurnStream / ToolCallCard / ApprovalCard /
                                    PlanPanel / CompactionPanel / MemoryPanel / AgentTreePanel /
                                    TurnControls / ThreadRail / Composer
web/src/views/agent-v2/AgentWorkbench.vue   工作台页面（自身不含路由）
web/tests/agent-v2/                 reducer / protocol / client 测试（零新增依赖）
```

## 3. 协议

### 3.1 帧结构（`agent.v2.protocol.v1`）

| 帧 | 必填字段 | 说明 |
|---|---|---|
| `request` | `contract` `kind=request` `id` `method` | 连接内唯一 `id`；变更类方法必须带 `idempotency_key` |
| `response` | `contract` `kind=response` `id` `ok` `result` `error` | 成功带 `result`，失败带结构化 `error`；`replayed=true` 表示来自幂等缓存 |
| `notification` | `contract` `kind=notification` `method` `event_id` `sequence` `thread_id` `params` | **每条通知都含事件 ID、序号、线程 ID 三个字段** |

通知通道：`event`（线程事件，参与游标）、`subscription/started`、`subscription/cancelled`、
`thread/closed`、`server/shutting_down`、`heartbeat`、`protocol/error`。

> ⚠️ **两个序列空间**：`event` 通知的 `sequence` 是**线程内事件序号**（可作游标）；
> 其余控制通知用的是**连接内自增序号**。客户端只能用 `method === "event"` 的通知推进线程游标，
> 混用会把控制通知误判成重复事件或缺口。前端 reducer 已按此实现并有测试覆盖。

### 3.2 方法表（26 个）

- thread：`thread/start`、`thread/list`、`thread/resume`、`thread/settings/update`、`thread/delete`
- turn：`turn/start`、`turn/steer`、`turn/continue`、`turn/interrupt`、`turn/recover`
- 订阅与补拉：`thread/subscribe`、`thread/unsubscribe`、`thread/events/replay`
- 审批：`approval/resolve`（`approve_once` / `approve_conversation` / `full_access` / `deny` / `cancel`）
- 压缩：`thread/compact`、`thread/compaction/list`、`thread/compaction/edit`、`thread/compaction/restore`
- 记忆：`memory/list`、`memory/update`、`memory/delete`
- 子 Agent：`agent/list`、`agent/wait`、`agent/interrupt`
- 元信息：`protocol/describe`

变更类方法（15 个）必须带 `idempotency_key`；断线重试复用同一个键即返回首次结果（`replayed=true`）。
同一个键配不同载荷报 `duplicate_request`——不静默接受，否则重放语义不可信。

### 3.3 结构化错误码

传输/会话：`invalid_request`、`invalid_params`、`unknown_method`、`contract_mismatch`、
`duplicate_request`、`stale_cursor`、`not_subscribed`、`runtime_unavailable`、`shutting_down`、`internal_error`。

领域（与 `contracts.agent_v2.errors` 一一对应）：`invalid_id`、`contract_violation`、
`thread_not_found`、`turn_not_found`、`approval_not_found`、`summary_not_found`、
`illegal_turn_transition`、`concurrent_turn`、`lease_error`、`memory_overwrite_denied`、
`corrupted_event`、`model_stream_error`、`cancelled`。

`stale_cursor` 的 `data.reason` 为 `cursor_ahead`（游标越过末尾）或
`below_replay_floor`（事件流被修复/重写后游标过期）；两者都会触发前端退化为
`thread/resume` 全量快照重建，**不重新调用模型**。

## 4. 挂载补丁

服务端（协调 Agent 在 `server/main.py` 的 `ROUTER_REGISTRY` 之后加两行）：

```python
from api.v2.agent.mount import install as install_agent_v2
install_agent_v2(app)
```

挂载后可用端点：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v2/agent/health` | 服务自检（host=fake、场景清单、关闭状态） |
| GET | `/api/v2/agent/protocol` | 协议 Schema + 方法清单 |
| POST | `/api/v2/agent/rpc` | 单请求通道（无 WebSocket 的客户端 / 集成测试） |
| WS | `/api/v2/agent/ws` | 双向会话主通道 |

`install()` 只做「初始化单例 + `include_router`」，不碰 v1 路由、CORS 与中间件，
且重复调用幂等。本线**未修改** `server/main.py`。

前端（协调 Agent 在 `web/src/router/index.ts` 加一条路由 + 在导航数组加一项），
完整片段见 `web/src/agent-v2/README.md`。

## 5. fixture 与假运行时

`server/api/v2/agent/fixtures/*.jsonl`（行式 JSON，每行一个独立对象）：

| fixture | 覆盖场景 |
|---|---|
| `text_multi_turn.jsonl` | 纯文本多轮（含推理增量与 usage） |
| `tools_parallel.jsonl` | 只读工具并行 + 副作用工具串行、工具增量输出 |
| `approval_flow.jsonl` | 审批等待与解决（批准后执行 / 拒绝后回喂） |
| `interrupt.jsonl` | 可中断的长回答（模型条目之间带延迟） |
| `compaction.jsonl` | 长上下文压缩 |
| `subagent.jsonl` | 子 Agent 创建 / 运行 / 失败 / 完成 |
| `plan.jsonl` | 计划展示与更新（计划只是事件流里的一个 Item） |
| `resume_after_disconnect.jsonl` | 断线后按游标补齐事件 |
| `error_retry.jsonl` | 可重试错误 -> 重试成功 |
| `error_fatal.jsonl` | 不可恢复错误 -> Turn 失败 |

假运行时（`api/v2/agent/host.py`）不触碰宿主机文件系统：`read_file`/`list_dir`/`write_file`/
`run_command`/`spawn_agent`/`update_plan` 全部由 fixture 脚本驱动，只产生事件与 Item。

## 6. 测试

### 6.1 后端（权威环境：容器 Python 3.11 + jsonschema，不用宿主 python）

```bash
# 全量（Agent 1 运行时 + 本线 API）
docker run --rm -e APP_ACCESS_MODE=owner_mode \
  -v "<repo>:/src" -w /src/server sciloop-backend:latest \
  python -m pytest tests/agent_v2 -q

# 只跑本线
docker run --rm -e APP_ACCESS_MODE=owner_mode \
  -v "<repo>:/src" -w /src/server sciloop-backend:latest \
  python -m pytest tests/agent_v2/api -q
```

> `-e APP_ACCESS_MODE=owner_mode` 是必需的：容器里没有 `.env`，默认 `public_demo` 面
> 会（正确地）拒绝匿名 WebSocket 连接。

覆盖：协议 Schema 正反向、幂等与重复提交、游标重放与分页、订阅与取消订阅、
非法 ID/非法状态/过期游标的结构化错误、Turn 控件全流程、审批五种决策、
压缩与三级记忆、Agent Tree、10 个 fixture 的端到端、WebSocket 会话
（流式推送、重连续订游标、畸形帧、订阅取消、服务关闭）。

### 6.2 前端

```bash
node web/tests/agent-v2/run.mjs      # 用 web 已有 typescript 编译后跑断言，零新增依赖
```

覆盖：事件去重、乱序缓冲与补拉排空、缺口检测、快照重建、协议方法与幂等键纪律、
客户端自动重连与「重发复用同一幂等键」、请求超时、令牌拼装。

### 6.3 结果

| 项目 | 命令 | 结果 |
|---|---|---|
| 后端（全量） | 见 6.1 第一条 | **235 passed / 0 failed**（其中本线 130 条） |
| 后端 lint | `ruff check api/v2 tests/agent_v2/api`（镜像内 0.16.8） | All checks passed |
| 前端类型 | `vue-tsc --noEmit -p tsconfig.json` | 通过（0 error） |
| 前端 lint | `eslint src/agent-v2 src/api/agent-v2.ts src/stores/agent-v2.ts src/components/agent-v2 src/views/agent-v2 --max-warnings 0` | 0 problem |
| 前端测试 | 见 6.2 | **42 passed / 0 failed** |

断线重连的可复核证据（自动化断言，非人工观察）：

- `test_ws_reconnect_resumes_from_last_cursor`：断开后用最后游标重连，`pending == 0`
  且新事件 `min(sequence) == cursor + 1`；
- `test_resume_after_disconnect_fixture`：补拉期间 `gateway.call_count` 不变（**没有**重新调用模型）；
- `client.test.mjs`：断线后未决请求**复用同一个幂等键**重发，服务端返回 `replayed=true`；
- `test_pump_is_not_repeating_old_events`：游标推进后不重复投递。

## 7. 兼容限制与已知边界

1. **不等待 Agent 2**：工具事件的字段一律以冻结契约为准；Agent 2 的真实工具完成后由协调
   Agent 做适配。本线的 `FakeRuntimeHost` 与 `AgentFacade` 之间是纯接口依赖，
   换成真实宿主时 API 层与前端无需改动。
2. **假运行时是离线的**：`read_file` 等工具不触碰宿主机；审批规则、命令前缀规范化都真实实现了，
   但真正的进程生命周期管理属于 Agent 2。
3. **`waiting_input` 的来源**：进入 `waiting_input` 需要工具层主动 `request_input`；
   本线的 fixture 不产生该状态，测试通过领域接口构造后验证 API 层的「继续」语义。
4. **控制通知的序号空间**：见 §3.1 的告警，客户端只能按 `method === "event"` 推进游标。
5. **压缩摘要是确定性的假摘要**：`service.SummarizerProvider` 走同一套 `ModelGateway`，
   但内容是稳定生成的，便于离线复现；接真实模型只需替换该 provider。
6. **单进程内存态**：幂等缓存与订阅都在进程内（事件流本身是文件）；多实例部署需要外部
   共享的幂等存储，本线未实现（也不在本任务包范围内）。
7. **前端页面尚未接入路由**：按计划书 §3，全局路由由协调 Agent 集成；因此本阶段无法提供
   「页面截图」，验收证据为测试输出与断言。
8. **`thread/delete` 是软删除**：写 `thread/archived` 事件，事件流与审计记录完整保留
   （计划书禁止本线删除旧对话）。
