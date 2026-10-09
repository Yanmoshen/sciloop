/**
 * agent.v2 前端模块入口（Agent 3 / WP-10）。
 *
 * 只依赖 `protocol.ts`（类型）与 `reducer.ts`（纯状态机）；Vue 组件与 Pinia store
 * 分别在 `components/agent-v2/`、`views/agent-v2/`、`stores/agent-v2.ts`。
 */

export * from './protocol'
export * from './reducer'
