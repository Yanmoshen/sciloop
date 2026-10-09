/**
 * Agent v2 工作台的独立预览入口（验收取证用）。
 *
 * 为什么需要它：工作台页面按计划书归协调 Agent 接路由，本线不能用改 `router/index.ts`
 * 的方式把它挂进主应用。于是这里提供一个**只挂载 v2 工作台**的最小入口：
 * 既能给验收出一张真实页面截图，也能把连接/停止/补齐的时序日志导出成可复核的证据。
 *
 * 它不进入生产构建（只在 `web/tests/agent-v2/preview/` 里使用）。
 */

import { createApp, watch } from 'vue'
import { createPinia } from 'pinia'

// 验收截图必须反映真实观感：把主应用的设计令牌引进来（只读引用，不改动它）
import '../../../src/styles/tokens.css'
import AgentWorkbench from '../../../src/views/agent-v2/AgentWorkbench.vue'
import { useAgentV2Store } from '../../../src/stores/agent-v2'

interface LogEntry {
  at: string
  kind: string
  detail: string
}

const log: LogEntry[] = []
const record = (kind: string, detail: string): void => {
  log.push({ at: new Date().toISOString(), kind, detail })
}

const params = new URLSearchParams(window.location.search)
const wsUrl = params.get('ws') ?? 'ws://127.0.0.1:8100/api/v2/agent/ws'

const app = createApp(AgentWorkbench, {
  wsUrl,
  ownerToken: params.get('token') ?? '',
})
const pinia = createPinia()
app.use(pinia)
app.mount('#app')

const store = useAgentV2Store(pinia)

// ---- 证据采集：把关键时序记成结构化日志（页面外可用 CDP 读出来） -------------
watch(
  () => store.connection.status,
  (status, previous) => {
    if (status !== previous) record('connection', `${previous ?? 'idle'} -> ${status}`)
  },
)
watch(
  () => store.connection.owner,
  (owner) => record('permission', owner ? '可写' : '只读'),
)
watch(
  () => store.backfilled,
  (count, previous) => {
    if (count > (previous ?? 0)) record('backfill', `通过游标补齐 ${count} 条事件（未调用模型）`)
  },
)
watch(
  () => store.stopping,
  (turnId) => {
    if (turnId) record('stop', '停止按钮：界面立刻收束（三点/计时/生成中立即消失）')
  },
)
watch(
  () => store.state.applied,
  (count) => record('events', `已应用事件 ${count} 条`),
)

interface ProbeApi {
  log: LogEntry[]
  store: typeof store
  /** 模拟网络中断（不触发「主动关闭」，用于验证自动重连与游标补拉）。 */
  dropNetwork: () => void
  /** 直接发起一轮（跳过研究确认卡），便于取证脚本编排。 */
  startTurn: (text: string, scenario?: string) => Promise<void>
  /** 用真实输入框提交（会走研究意图确认卡这条 UI 路径）。 */
  type: (text: string) => void
  /** 点击发送按钮。 */
  send: () => void
  /** 停止当前回合（等价于点「中断」按钮）。 */
  stopTurn: () => Promise<void>
  snapshots: () => Record<string, unknown>
}

const api: ProbeApi = {
  log,
  store,
  dropNetwork: () => {
    record('connection', '模拟网络中断（底层连接被断开）')
    const raw = store.client as unknown as { socket: { close: () => void } | null }
    raw?.socket?.close()
  },
  startTurn: async (text: string, scenario?: string) => {
    let thread = store.currentThread
    if (!thread) {
      thread = await store.createThread(scenario ? `${scenario} 会话` : '验收会话', { scenario })
    }
    await store.startTurn(thread.thread_id, text)
    record('turn', `已发起一轮：${text.slice(0, 24)}`)
  },
  type: (text: string) => {
    const input = document.querySelector('.composer__input') as HTMLTextAreaElement | null
    if (!input) return
    input.value = text
    input.dispatchEvent(new Event('input', { bubbles: true }))
    record('input', `在输入框写入：${text.slice(0, 24)}`)
  },
  send: () => {
    const button = document.querySelector('.composer__send') as HTMLButtonElement | null
    button?.click()
    record('input', '点击发送')
  },
  stopTurn: async () => {
    const turn = store.active
    const threadId = store.state.currentThreadId
    if (turn && threadId) await store.stopTurn(threadId, turn.turn_id)
  },
  snapshots: () => ({
    currentThreadId: store.state.currentThreadId,
    cursor: store.cursor,
    applied: store.state.applied,
    duplicates: store.state.duplicates,
    lateDropped: store.state.lateDropped,
    items: store.items.length,
    toolCalls: store.toolCalls.length,
    approvals: store.approvals.length,
    connection: store.connection.status,
    owner: store.connection.owner,
  }),
}

;(window as unknown as { __agentV2: ProbeApi }).__agentV2 = api
record('boot', `预览已加载，目标 ${wsUrl}`)
