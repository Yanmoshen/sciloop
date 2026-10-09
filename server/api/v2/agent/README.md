# Agent v2 API 层与前端协议（Agent 3 交付说明）

本文件是 Agent 3（WP-09 WebSocket 协议与 API + WP-10 前端运行时）的**交付说明**，与代码同目录。
本轮依据的文档是 `docs/Agent3-API协议与前端恢复-计划验收.md`（并行实施计划书 + 验收书）。

## 1. 隔离与基线

| 项目 | 值 |
|---|---|
| 分支 | `codex/agent-ui` |
| worktree | `D:\aicoding竞赛-worktrees\agent-ui` |
| 本轮提交 | 见本文件末尾「提交记录」（内容提交 + 交付说明提交） |
| 契约基线（冻结标签） | `agent-v2-contract-v1` → `6a9fa00`（`server/contracts/agent_v2/` 只读） |
| 实现基线 | `codex/agent-runtime` 的 `4d7bec5`（以 `6a9fa00` 为父提交） |
| 祖先校验 | `git merge-base --is-ancestor agent-v2-contract-v1 HEAD` → 真 |
| 本线新增范围 | `git diff --name-only 4d7bec5..HEAD`（只含本线白名单目录） |
| 是否依赖 Agent 2 | **否**。工具与审批行为全部来自冻结协议 + fake runtime + 本线 JSONL fixture |

关于计划书 §7.1 的 `git diff --name-only agent-v2-contract-v1...HEAD`：本线按 §2.1 的允许
**以 Agent 1 的实现提交 `4d7bec5` 为起点**（否则无法消费 `start_turn` 等运行时能力），
因此这个三点式 diff 会连带列出 Agent 1 的目录。等价的、更准确的隔离证据是
`git diff --name-only 4d7bec5..HEAD`，其结果只含本线目录（见上表）。

## 2. 本轮相对上一轮补齐的差异

上一轮已交付协议层 / API 层 / fake runtime / 前端 reducer 与 store / 组件与页面。
按新文档逐条核对后，本轮补齐或修正了以下内容：

| 差异点 | 处理 |
|---|---|
| 错误码词汇（WP-01） | 对齐为 `invalid_state` / `idempotency_conflict` / `cursor_expired` / `server_shutting_down`，并新增 `approval_pending` / `permission_denied` / `tool_failed` / `turn_interrupted` / `turn_failed`（每条都有真实触发点，见 §3.3） |
| `protocol/ready`（WP-02） | 连接建立后第一条通知改为 `protocol/ready`，并告知本连接能否写入 |
| 只读访问面（WP-01） | 匿名公开面允许建立**只读**会话，变更类请求返回 `permission_denied`（不再直接掐连接） |
| 连接状态命名（WP-04） | 客户端状态统一为 `idle / connecting / connected / reconnecting / closed` |
| 停止立刻收束（WP-04/§7.3） | 新增 `stopTurn()`：点击即切到「已停止」，三点/计时/生成中立刻消失，服务端中断只作确认 |
| 迟到事件规则（WP-03 规则 4/5） | reducer 增加终态保护：终态不可被迟到事件改回运行态；中断后忽略迟到的正文与工具增量 |
| 快照 + 游标持久化（WP-03 规则 6） | 新增 `recovery.ts`：快照落 localStorage，重进先渲染快照再补事件 |
| 模块拆分（WP-03 文件清单） | 拆出 `types.ts` / `cursor.ts` / `recovery.ts`（`reducer.ts` 保持纯函数） |
| 研究确认卡（WP-05） | 新增 `ResearchConfirmCard`：确认前不显示任何执行进度（关键词只用于弹卡，不拦截请求） |
| 组件拆分（WP-05 组件清单） | 新增 `ConversationShell` / `MessageItem` / `blocks.ts`（事件→渲染块的纯归约） |
| 界面去内部细节（§7.3） | 移除游标序号、重连次数、事件补齐条数、`call_id` / `turn_id` / `summary_id`、错误码、内部时间戳 |
| fixture 规范化（WP-07） | 12 个规范名全部就位（见 §4.2），另保留 5 个补充场景 |
| 重连重复投递 | 证据采集时发现「订阅推送 + 立即 replay」双路补同一段导致 7 条重复；已改为只由订阅游标推送补齐，复验重复数为 0 |

## 3. 协议（`agent.v2.protocol.v1`）

### 3.1 三类帧

| 帧 | 必填字段 | 说明 |
|---|---|---|
| `request` | `contract` `kind=request` `id` `method` | 连接内唯一 `id`；变更类方法必须带 `idempotency_key` |
| `response` | `contract` `kind=response` `id` `ok` `result` `error` | 失败带结构化 `error`；`replayed=true` 表示来自幂等缓存 |
| `notification` | `contract` `kind=notification` `method` `event_id` `sequence` `thread_id` `params` | 每条通知都含事件 ID、序号、线程 ID |

通知通道：`protocol/ready`、`event`、`subscription/started`、`subscription/cancelled`、
`thread/closed`、`server/shutting_down`、`heartbeat`、`protocol/error`。

> ⚠️ **两个序列空间**：`event` 通知的 `sequence` 是**线程内事件序号**（可作游标）；
> 其余控制通知使用**连接内自增序号**。客户端只能用 `method === "event"` 的通知推进线程游标。

### 3.2 方法（25 个）

- thread：`thread/start`、`thread/list`、`thread/resume`、`thread/settings/update`、`thread/delete`
- turn：`turn/start`、`turn/steer`、`turn/continue`、`turn/interrupt`、`turn/recover`
- 订阅与补拉：`thread/subscribe`、`thread/unsubscribe`、`thread/events/replay`
- 审批：`approval/resolve`（`approve_once` / `approve_conversation` / `full_access` / `deny` / `cancel`）
- 压缩：`thread/compact`、`thread/compaction/list`、`thread/compaction/edit`、`thread/compaction/restore`
- 记忆：`memory/list`、`memory/update`、`memory/delete`
- 子 Agent：`agent/list`、`agent/wait`、`agent/interrupt`
- 元信息：`protocol/describe`

变更类 15 个方法必须带幂等键；同键不同载荷返回 `idempotency_conflict`（不静默接受）。

### 3.3 错误码（28 个）与触发点

| code | 什么时候出现 |
|---|---|
| `invalid_request` | 帧结构不合法 / 不是 JSON / kind 不是 request |
| `invalid_params` | 参数缺失、多余或类型不对；变更类缺幂等键 |
| `invalid_id` | 非法 Thread / Turn / Call / Approval / Memory ID |
| `invalid_state` | 状态机不允许的迁移（`data.kind` 细分：`illegal_turn_transition` / `turn_completed` / …） |
| `idempotency_conflict` | 同一幂等键或 request_id 配了不同载荷 |
| `cursor_expired` | 游标越过末尾（`cursor_ahead`）或落在已重写的历史之前（`below_replay_floor`） |
| `permission_denied` | 只读访问面发起变更类请求；连接鉴权被拒 |
| `runtime_unavailable` | 运行时未装配 / 已关闭 / 场景声明不可用 |
| `server_shutting_down` | 服务关闭中，拒绝新请求 |
| `approval_pending` | 目标 Turn 正在等待审批（`turn/steer`、`turn/continue`） |
| `tool_failed` | 审批对应的调用已经以失败结束 |
| `turn_interrupted` / `turn_failed` | 对已中断 / 已失败的 Turn 做继续类操作 |
| `concurrent_turn` | 同一 Thread 上已有活动 Turn |
| `not_subscribed` | 对未订阅的线程取消订阅 |
| 其余 | `contract_mismatch`、`contract_violation`、`thread_not_found`、`turn_not_found`、`approval_not_found`、`summary_not_found`、`lease_error`、`memory_overwrite_denied`、`corrupted_event`、`model_stream_error`、`cancelled`、`unknown_method`、`internal_error` |

错误响应的 `message` 面向用户可读，**不含** traceback / 内部路径 / 令牌。

## 4. API 层与假运行时

### 4.1 边界

`AgentFacade` 只做鉴权上下文、参数与 ID 校验、领域命令调用、订阅与返回；
路由不含模型循环、工具审批、压缩算法或文件执行。

### 4.2 fixture（`server/api/v2/agent/fixtures/`）

WP-07 规定的 12 个（名字一致）：`plain_chat_hello`、`twenty_turns`、`tool_parallel`、
`tool_serial`、`approval_pending`、`approval_resolved`、`turn_interrupt`、`compaction`、
`child_agents`、`reconnect_gap`、`tool_failure`、`runtime_unavailable`。

本线补充 5 个：`text_multi_turn`、`plan`、`error_retry`、`error_fatal`、`approval_flow`。

假运行时（`host.py`）不触碰宿主机文件系统；`runtime_unavailable` 由 fixture 里的
`{"type":"runtime","available":false}` 驱动，启动 Turn 时返回结构化错误并把 Turn 收成失败终态。

## 5. 前端

| 文件 | 作用 |
|---|---|
| `web/src/agent-v2/protocol.ts` | 帧与契约类型、方法名、幂等键纪律 |
| `web/src/agent-v2/types.ts` | 前端视图状态类型 |
| `web/src/agent-v2/cursor.ts` | 去重 / 乱序 / 缺口排空（纯算法） |
| `web/src/agent-v2/recovery.ts` | 快照持久化与恢复（存储可注入） |
| `web/src/agent-v2/reducer.ts` | 纯函数状态机与选择器（六条规则见文件头注释） |
| `web/src/api/agent-v2.ts` | WebSocket 客户端：自动重连、游标续订、幂等键复用、超时 |
| `web/src/stores/agent-v2.ts` | Pinia store：连接 / 会话切换 / 停止收束 / 快照恢复 / 全部动作 |
| `web/src/components/agent-v2/blocks.ts` | 事件 → 渲染块的纯归约 + 研究意图识别 |
| 组件 | `ConversationShell`、`MessageItem`、`TurnStream`、`ToolCallCard`、`ApprovalCard`、`ResearchConfirmCard`、`PlanPanel`、`CompactionPanel`、`MemoryPanel`、`AgentTreePanel`、`TurnControls`、`ThreadRail`、`Composer`、`ConnectionBanner` |
| `web/src/views/agent-v2/AgentWorkbench.vue` | 工作台页面（**不自带路由**） |

两条产品口径：研究意图先弹确认卡（确认前无任何执行进度）；停止按钮点击后界面立刻收束。

## 6. 测试与结果

```bash
# 后端（权威环境为容器 Python 3.11 + jsonschema；本机 Docker 引擎离线时用宿主 Python 3.14 等价跑）
python -m pytest tests/agent_v2 -q            # 全量
python -m pytest tests/agent_v2/api -q        # 本线
ruff check api/v2 tests/agent_v2/api

# 前端
node web/tests/agent-v2/run.mjs               # 零新增依赖
npx vue-tsc --noEmit
npx eslint src/agent-v2 src/api/agent-v2.ts src/stores/agent-v2.ts --max-warnings 0
```

| 项目 | 结果 |
|---|---|
| 后端全量 `tests/agent_v2` | **255 passed / 0 failed / 0 skipped**（本线 150 + Agent 1 运行时 105） |
| 后端 `tests/agent_v2/api` | 150 passed / 0 failed |
| ruff 0.16.8（`api/v2` + `tests/agent_v2/api`） | All checks passed |
| 前端 `run.mjs` | **57 passed / 0 failed** |
| `vue-tsc --noEmit` | 0 error |
| eslint（v2 目录） | 0 problem |

> ⚠️ 环境说明：本机 Docker 引擎在本次会话中途离线，最后的**全量**回归是在宿主
> Python 3.14（pytest 9.0.3 + jsonschema 4.26 + fastapi/httpx 齐备）上跑的；
> 容器内回归此前为 235 passed（旧用例集），新增用例未在容器内复跑，交接时建议补跑一次。

## 7. 验收逐项结论（对照验收书 §7）

### 7.1 隔离
- [x] 分支 / worktree / 基线 / 白名单正确（§1）
- [x] Agent 2 未完成不阻塞：工具行为全部来自 fake + fixture
- [x] 本线新增文件只落白名单目录（`git diff --name-only 4d7bec5..HEAD`）
- [x] 未修改 v1 API、HomeView、全局路由、主入口、运行时、权限与真实数据
- [x] 提供挂载与路由片段，未自行改主入口（§8）

### 7.2 协议与恢复
- [x] request / response / notification 三类帧可区分（Schema 正反向测试）
- [x] 变更类请求带幂等键，重复提交不重复创建 Turn
- [x] 通知含 `event_id` / `sequence` / `thread_id` / `turn_id`
- [x] 游标 replay、乱序缓冲、缺口补拉、重复去重均有自动测试（后端 + 前端）
- [x] `cursor_expired` / `runtime_unavailable` / `server_shutting_down` 结构化错误
- [x] 切换与刷新后靠「快照 + 游标」恢复，不调用模型（后端与前端各有用例；浏览器证据见 §9）

### 7.3 Turn 与工具 UI
- [x] hello 有 `turn/completed` 终态，前端不永久 loading
- [x] 工具卡显示生命周期与真实失败原因，不伪造成功
- [x] 审批卡支持一次批准 / 持续批准 / 完全访问 / 拒绝 / 取消
- [x] Stop 后三点、计时、生成中立刻消失（浏览器实测，见 §9）
- [x] 迟到事件不会复活 interrupted/failed/completed 的 Turn
- [x] debug trace、内部思考、令牌、重试细节不进入 UI

### 7.4 研究、压缩与记忆
- [x] 研究词先显示确认卡，未确认无检索进度（浏览器实测）
- [x] 压缩开始 / 完成 / 失败可显示，摘要可查看、编辑、恢复
- [x] 用户 / 项目 / 对话记忆列表与状态可恢复
- [x] 子 Agent 状态与结果来源明确

### 7.5 交付证据
- [x] commit SHA、协议版本、方法清单、组件 / store 清单、fixture 列表、测试命令与通过数（本文件）
- [x] 断线重连日志、停止按钮日志、真实页面截图（`server/api/v2/agent/evidence/`，§9）
- [x] 挂载与路由片段（§8）

### 未通过 / 偏差项（如实列出）
1. **路径偏差**：计划书 §2.2 允许的 `tests/agent_v2/api/` 与 `docs/agent-v2-ui-*.md` 是**根锚定
   本地忽略目录**（`.gitignore:115-116` 的 `/docs/`、`/tests/`），写在那里等于不进仓库。
   本线因此沿用 Agent 1/2 已建立的 `server/tests/agent_v2/api/`，文档放在随仓库分发的
   `server/api/v2/agent/`（另在 `docs/` 留一份本地指针）。若协调 Agent 希望改路径，需要先把
   忽略规则调整过来，否则交付物不可见。
2. **容器内复跑缺失**：见 §6 的环境说明；本机 Docker 引擎离线导致最后一次全量回归跑在宿主
   Python 3.14。
3. **假 provider 无法按审批决定分支**：fixture 是固定脚本，“模型在拒绝后改道的措辞”无法按
   决定分支，因此拒绝路径断言的是**回喂证据**（工具结果 Item 携带 `approval_denied` + 模型
   继续下一轮），不是措辞。真实运行时不存在这个限制。

## 8. 挂载与路由片段

服务端（协调 Agent 在 `server/main.py` 的 `ROUTER_REGISTRY` 之后加两行）：

```python
from api.v2.agent.mount import install as install_agent_v2
install_agent_v2(app)
```

挂载后可用端点：`GET /api/v2/agent/health`、`GET /api/v2/agent/protocol`、
`POST /api/v2/agent/rpc`、`WS /api/v2/agent/ws`。`install()` 幂等，不碰 v1 路由、CORS 与中间件。

前端（协调 Agent 在 `web/src/router/index.ts` 加一条路由 + 导航入口）：

```ts
{
  path: '/agent-v2',
  name: 'agent-v2',
  component: () => import('../views/agent-v2/AgentWorkbench.vue'),
  meta: { title: 'Agent 工作台' },
}
```

## 9. 证据

`server/api/v2/agent/evidence/`（随仓库留存，由 `web/tests/agent-v2/preview/` + 本机 Chrome
调试协议实测采集，脚本见下方「复现证据」）：

| 文件 | 内容 |
|---|---|
| `01-research-confirm.png` | 研究意图确认卡（确认前无执行进度） |
| `02-approval-pending.png` | 审批卡挂起：命令、目录、风险、参数与五个动作 |
| `03-approval-approved.png` | 批准一次后工具执行完成（输出与耗时） |
| `04-stopped.png` | 停止后的收束状态 |
| `05-after-reconnect.png` | 断线重连并以游标补齐之后 |
| `evidence.json` | 实测步骤、时序日志与快照统计 |

关键时序（取自 `evidence.json`，时间取 UTC）：
`connecting -> connected` → 输入框写入 + 点击发送 → 确认卡出现 →
`connected -> reconnecting` →（另一客户端在断线期间写入一轮）→ `reconnecting -> connected`
→ 游标从 9 前进到 16、重复数 0；停止按钮点击到界面收束 ≈0.3s。

## 10. 兼容限制

1. 不等待 Agent 2：工具与审批行为来自 fake；换成真实宿主时 API 层与前端无需改动。
2. 进入 `waiting_input` 需要工具层主动请求输入；本线 fixture 不产生该状态，测试通过领域接口构造。
3. 控制通知的序号空间与事件序号不同（§3.1），客户端只按 `method === "event"` 推进游标。
4. 压缩摘要由确定性的假摘要器生成，走同一套 `ModelGateway`，便于离线复现。
5. 幂等缓存与订阅在进程内；多实例部署需要外部共享存储（本任务包范围外）。
6. 页面按计划书归协调 Agent 接路由，本线提供预览入口用于取证（`web/tests/agent-v2/preview/`）。

## 11. 复现证据 / 复跑测试

```bash
# 1) 起后端（只挂 v2，不碰 v1）与预览页
python install_env/serve_v2.py 8100                 # 脚本：.learnbuddy/tmp/serve_v2.py
node web/node_modules/vite/bin/vite.js build --config web/tests/agent-v2/preview/vite.config.ts
python -m http.server 8123 --directory web/tests/agent-v2/preview/.preview
# 2) 用本机 Chrome 调试协议采集（截图 + 时序日志）
node .learnbuddy/tmp/cdp_evidence.js                # 输出到 .learnbuddy/tmp/evidence/
```

（取证脚本放在本地 `.learnbuddy/tmp/`，不随仓库分发；步骤与断言已在 §9 与测试用例中固化。）

## 12. 提交记录

| 提交 | 说明 |
|---|---|
| `f3212d8` | 首轮交付：协议层 + API 层 + fake runtime + 前端 reducer/store/组件/页面（68 新文件） |
| `97bc170` | 交付说明补内容提交 SHA |
| 本轮 | 按 `docs/Agent3-API协议与前端恢复-计划验收.md` 补齐差异（§2）+ 证据留存（§9） |
