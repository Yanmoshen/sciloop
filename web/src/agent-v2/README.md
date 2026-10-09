# agent-v2 前端模块（Agent 3 / WP-10）

本目录与 `web/src/{api/agent-v2.ts,stores/agent-v2.ts,components/agent-v2/,views/agent-v2/,tests/agent-v2/}`
一起构成 Agent v2 的前端交付。**不修改**旧路由、旧页面与全局样式。

## 文件清单

| 文件 | 作用 |
|---|---|
| `protocol.ts` | 协议与契约类型、方法名常量、需幂等键的方法集合 |
| `reducer.ts` | 纯函数状态机：去重 / 乱序缓冲 / 缺口检测 / 快照重建 / 选择器 |
| `../api/agent-v2.ts` | WebSocket 客户端：自动重连、游标续订、请求幂等键、超时 |
| `../stores/agent-v2.ts` | Pinia store：把客户端与 reducer 接起来，暴露动作与选择器 |
| `../components/agent-v2/*.vue` | 连接条 / 工具卡片 / 审批卡片 / 计划 / 压缩 / 记忆 / 子 Agent 树 / Turn 控件 / 输入框 / 流渲染 |
| `../views/agent-v2/AgentWorkbench.vue` | 工作台页面（自身不含路由） |
| `../tests/agent-v2/` | reducer / 协议 / 客户端的 node 测试（不新增依赖） |

## 三条状态不变量

1. **只有 reducer 能改状态**：实时推送、游标补拉、快照重建三条路径共用同一套应用逻辑，
   语义不会漂移。
2. **只有 `method === "event"` 的通知参与游标**：心跳、订阅确认、关闭提示用的是
   **连接级自增序号**，与线程事件序号是两个序列空间，混用会导致误判重复或缺口。
3. **恢复界面不调用模型**：刷新页面走 `thread/resume`（服务端快照）+ `thread/events/replay`
   （按游标补事件）；服务端回 `stale_cursor` 时退化为全量快照重建。

## 挂载补丁（由协调 Agent 执行）

本模块不自带路由，需要协调 Agent 在全局路由与导航里登记一次：

```ts
// web/src/router/index.ts —— 新增一条路由（不要改动既有条目）
{
  path: '/agent-v2',
  name: 'agent-v2',
  component: () => import('../views/agent-v2/AgentWorkbench.vue'),
  meta: { title: 'Agent 工作台' },
}
```

```vue
<!-- 侧栏导航入口（HomeLayout.vue 的导航数组）新增一项，示例 -->
{ path: '/agent-v2', label: 'Agent 工作台' }
```

服务端侧对应挂载补丁见 `server/api/v2/agent/mount.py` 的 `PATCH_SNIPPET`：

```python
from api.v2.agent.mount import install as install_agent_v2
install_agent_v2(app)
```

WebSocket 地址默认 `同源 /api/v2/agent/ws`；如服务端在别的域，给页面传 `ws-url`。
`owner_mode` 之外的访问面需要令牌，通过 `owner-token` prop 传入（会拼成查询参数）。

## 测试

```bash
# 在仓库任意位置执行；用 web 已有的 typescript 编译后跑断言，**不新增任何依赖**
node web/tests/agent-v2/run.mjs
```

覆盖：事件去重、乱序缓冲与补拉排空、缺口检测、快照重建、协议方法与幂等键纪律、
客户端自动重连与「重发复用同一幂等键」。测试**不依赖网络、不依赖浏览器**；
编译产物落在 `web/tests/agent-v2/.test-build/`（已随目录 .gitignore 忽略，成功后自动清理）。
