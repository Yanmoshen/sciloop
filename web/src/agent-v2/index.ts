/**
 * agent.v2 前端模块入口（Agent 3 / WP-10）。
 *
 * 分层：
 * - `protocol.ts`  服务端帧与契约对象的类型、方法名、幂等键纪律
 * - `types.ts`     前端视图状态类型
 * - `cursor.ts`    去重 / 乱序 / 缺口排空（纯算法）
 * - `recovery.ts`  快照持久化与恢复（localStorage 可注入）
 * - `reducer.ts`   纯函数状态机与选择器
 *
 * Vue 组件与 Pinia store 分别在 `components/agent-v2/`、`views/agent-v2/`、`stores/agent-v2.ts`。
 */

export * from './protocol'
export * from './cursor'
export * from './recovery'
// reducer 已经转出 types 里的视图状态类型（避免同名导出冲突）
export * from './reducer'
