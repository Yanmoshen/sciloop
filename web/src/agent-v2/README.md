# agent-v2 前端模块（Agent 3 / WP-10）

本目录与 `web/src/{api/agent-v2.ts,stores/agent-v2.ts,components/agent-v2/,views/agent-v2/,tests/agent-v2/}`
一起构成 Agent v2 前端交付。**不修改**旧路由、旧页面与全局样式。

## 文件清单

| 文件 | 作用 |
|---|---|
| `protocol.ts` | 帧与契约类型、方法名、需幂等键的方法集合 |
| `types.ts` | 前端视图状态类型（连接状态、流缓冲、提示、整棵状态树） |
| `cursor.ts` | 去重 / 乱序 / 缺口排空（纯算法，单独可测） |
| `recovery.ts` | 快照持久化与恢复（存储可注入，浏览器不可用时静默降级） |
| `reducer.ts` | 纯函数状态机与选择器 |
| `../api/agent-v2.ts` | WebSocket 客户端：自动重连、游标续订、幂等键复用、超时 |
| `../stores/agent-v2.ts` | Pinia store：连接、会话切换、停止收束、快照恢复、全部动作 |
| `../components/agent-v2/*.vue` | `ConversationShell` / `MessageItem` / `TurnStream` / `ToolCallCard` / `ApprovalCard` / `ResearchConfirmCard` / `PlanPanel` / `CompactionPanel` / `MemoryPanel` / `AgentTreePanel` / `TurnControls` / `ThreadRail` / `Composer` / `ConnectionBanner` |
| `../components/agent-v2/blocks.ts` | 事件 → 渲染块的纯归约 + 研究意图识别 |
| `../views/agent-v2/AgentWorkbench.vue` | 工作台页面（自身不含路由） |
| `../tests/agent-v2/` | protocol / cursor / recovery / reducer / client 测试 + 取证预览入口 |

## 六条状态规则（reducer 文件头有同样一份）

1. 按序号去重：`sequence <= cursors[thread]` 的事件直接丢弃；
2. 乱序缓冲：`sequence > cursor + 1` 先入 pending 并暴露缺口，由 store 触发 replay，补齐后按序排空；
3. **只有 `method === "event"` 的通知参与游标**：心跳 / 订阅确认 / 关闭提示用的是连接级序号，
   是另一个序列空间，混用会导致误判重复或缺中文档；
4. 迟到的 delta 不能复活已终结的 Turn（终态是权威）；
5. 中断后忽略迟到的正文与工具增量，只接受终态与取消类事件；
6. 页面卸载保留快照 + 游标；重进先渲染快照，再由服务端快照与游标补齐，**绝不为恢复界面调用模型**。

## 两条产品口径

- **研究意图先确认**：`blocks.ts: looksLikeResearch()` 只决定「要不要弹确认卡」，不拦截请求；
  确认前界面不得出现任何检索/执行进度。
- **停止立刻收束**：`store.stopTurn()` 先把本地状态切到已停止（三点/计时/生成中立刻消失），
  服务端的 `turn/interrupted` 只作确认。

## 挂载补丁（由协调 Agent 执行）

```ts
// web/src/router/index.ts —— 新增一条路由（不要改动既有条目）
{
  path: '/agent-v2',
  name: 'agent-v2',
  component: () => import('../views/agent-v2/AgentWorkbench.vue'),
  meta: { title: 'Agent 工作台' },
}
```

服务端对应挂载片段见 `server/api/v2/agent/mount.py` 的 `PATCH_SNIPPET`。

## 测试

```bash
node web/tests/agent-v2/run.mjs      # 用仓库已有 typescript 编译后跑断言，零新增依赖
```

当前结果：**57 passed / 0 failed**。覆盖事件去重、乱序缓冲与补拉排空、缺口检测、快照持久化与
恢复、终态保护（迟到事件不复活）、中断后忽略迟到增量、协议方法与幂等键纪律、客户端自动重连与
「重发复用同一幂等键」。

## 验收预览（取证用）

`web/tests/agent-v2/preview/` 提供一个只挂载 v2 工作台的最小入口（自带构建配置），
配合只挂 v2 的后端即可出真实页面截图与时序日志；产物目录 `.preview/` 已被忽略。
证据见 `server/api/v2/agent/evidence/`。
