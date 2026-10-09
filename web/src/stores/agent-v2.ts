/**
 * agent.v2 Pinia store：把协议客户端与 reducer 接起来，并暴露界面需要的动作与选择器。
 *
 * 三条不变量：
 * 1. 状态只能由 reducer 产生（本文件不直接改状态字段），保证「实时推送 / 游标补拉 /
 *    快照重建」三条路径语义一致；
 * 2. 变更类请求都带稳定的幂等键：断线重试沿用同一个键，服务端不会重复产生副作用；
 * 3. 检测到事件缺口时自动 `thread/events/replay` 补拉；若服务端回 `stale_cursor`
 *    （历史已被重写），则退化为 `thread/resume` 全量快照重建，**绝不重新调用模型**。
 */

import { computed, ref, shallowRef, type Ref } from 'vue'
import { defineStore } from 'pinia'

import {
  AgentV2Client,
  AgentV2RequestError,
  isStaleCursor,
  unwrap,
  type AgentV2ClientOptions,
} from '../api/agent-v2'
import {
  METHOD,
  PROTOCOL_VERSION,
  type ApprovalDecision,
  type ApprovalView,
  type ChildView,
  type CompactionRunView,
  type ItemView,
  type MemoryRecordView,
  type NotificationFrame,
  type SummaryView,
  type ThreadView,
  type ToolCallView,
  type TurnView,
} from '../agent-v2/protocol'
import {
  activeTurn,
  applyChildList,
  applyMemoryList,
  applyNotification,
  applyReplay,
  applyResumeSnapshot,
  applySummaryList,
  assistantTextFor,
  childList,
  createAgentState,
  latestPlan,
  orderedItems,
  pendingApprovals,
  reasoningTextFor,
  setConnection,
  setThreadCursor,
  toolOutputFor,
  type AgentV2State,
} from '../agent-v2/reducer'

export type AgentV2StoreOptions = AgentV2ClientOptions

export const useAgentV2Store = defineStore('agent-v2', () => {
  const state = shallowRef<AgentV2State>(createAgentState())
  const client = shallowRef<AgentV2Client | null>(null)
  const subscribed = ref<string[]>([])
  const backfilling = new Set<string>()
  const busy = ref(false)
  /** 用户最近一次「发起 Turn」的幂等键：失败重试必须复用它。 */
  const lastTurnKey = new Map<string, string>()

  const currentThread = computed<ThreadView | null>(() => {
    const id = state.value.currentThreadId
    return id ? state.value.threads[id] ?? null : null
  })
  const threads = computed<ThreadView[]>(() =>
    state.value.threadOrder.map((id) => state.value.threads[id]).filter(Boolean),
  )
  const connection = computed(() => state.value.connection)
  const notices = computed(() => state.value.notices)
  const approvals = computed<ApprovalView[]>(() => {
    const id = state.value.currentThreadId
    return id ? pendingApprovals(state.value, id) : []
  })
  const turns = computed<TurnView[]>(() => {
    const id = state.value.currentThreadId
    if (!id) return []
    return (state.value.turnsByThread[id] ?? []).map((turnId) => state.value.turns[turnId]).filter(Boolean)
  })
  const active = computed<TurnView | null>(() => {
    const id = state.value.currentThreadId
    return id ? activeTurn(state.value, id) : null
  })
  const plan = computed(() => {
    const id = state.value.currentThreadId
    return id ? latestPlan(state.value, id) : null
  })
  const children = computed<ChildView[]>(() => {
    const id = state.value.currentThreadId
    return id ? childList(state.value, id) : []
  })
  const summaries = computed<SummaryView[]>(() => {
    const id = state.value.currentThreadId
    return id ? state.value.summaries[id] ?? [] : []
  })
  const compactionRuns = computed<CompactionRunView[]>(() => {
    const id = state.value.currentThreadId
    return id ? state.value.compactionRuns[id] ?? [] : []
  })
  const items = computed<ItemView[]>(() => {
    const id = state.value.currentThreadId
    return id ? orderedItems(state.value, id) : []
  })
  const memories = computed<MemoryRecordView[]>(() => {
    const id = state.value.currentThreadId
    if (!id) return []
    return state.value.memories[`conversation:${id}`] ?? []
  })
  const toolCalls = computed<ToolCallView[]>(() => {
    const id = state.value.currentThreadId
    if (!id) return []
    return (state.value.toolCallsByThread[id] ?? [])
      .map((callId) => state.value.toolCalls[callId])
      .filter(Boolean)
  })
  const cursor = computed<number>(() => {
    const id = state.value.currentThreadId
    return id ? state.value.cursors[id] ?? 0 : 0
  })
  /** 断线补齐进度：> 0 表示最近一次重连补过事件。 */
  const backfilled = computed<number>(() => {
    const id = state.value.currentThreadId
    return id ? state.value.backfilled[id] ?? 0 : 0
  })
  const gapFrom = computed<number | null>(() => {
    const id = state.value.currentThreadId
    return id ? state.value.gapFrom[id] ?? null : null
  })
  const lastError = computed(() => state.value.errors.at(-1) ?? null)

  // ------------------------------------------------------------------ 内部
  function set(next: AgentV2State): void {
    state.value = next
  }

  function errMessage(error: unknown): string {
    if (error instanceof AgentV2RequestError) return `${error.code}: ${error.message}`
    if (error instanceof Error) return error.message
    return String(error)
  }

  function describeThread(threadId: string): string {
    return state.value.threads[threadId]?.name ?? threadId
  }

  async function call(
    method: string,
    params: Record<string, unknown> = {},
    options: { idempotencyKey?: string; timeoutMs?: number } = {},
  ): Promise<Record<string, unknown>> {
    const active = client.value
    if (!active) throw new AgentV2RequestError('尚未连接', 'not_connected')
    return unwrap(await active.request(method, params, options))
  }

  function handleNotification(note: NotificationFrame): void {
    const before = state.value
    const next = applyNotification(before, note)
    set(next)
    if (note.method === 'event' && note.thread_id) {
      const gap = next.gapFrom[note.thread_id]
      if (gap !== null && gap !== undefined) void backfill(note.thread_id)
    }
    if (note.method === 'server/shutting_down') {
      subscribed.value = []
    }
  }

  /** 按游标补拉；服务端说游标过期就退回全量快照。 */
  async function backfill(threadId: string): Promise<void> {
    if (backfilling.has(threadId)) return
    backfilling.add(threadId)
    try {
      const from = state.value.cursors[threadId] ?? 0
      const result = await call(METHOD.threadReplay, {
        thread_id: threadId,
        after_sequence: from,
        limit: 500,
      })
      const events = (result.events ?? []) as Parameters<typeof applyReplay>[2]
      set(applyReplay(state.value, threadId, events))
      const hasMore = Boolean(result.has_more)
      if (hasMore) {
        backfilling.delete(threadId)
        await backfill(threadId)
        return
      }
      if ((state.value.gapFrom[threadId] ?? null) !== null) {
        // 补拉后仍有缺口 -> 历史被重写，改走全量快照
        await resumeThread(threadId)
      }
    } catch (error) {
      if (isStaleCursor(error)) {
        await resumeThread(threadId)
      } else {
        pushError(error)
      }
    } finally {
      backfilling.delete(threadId)
    }
  }

  function pushError(error: unknown): void {
    const message = errMessage(error)
    const code = error instanceof AgentV2RequestError ? error.code : 'client_error'
    set({
      ...state.value,
      errors: [...state.value.errors, { code, message, data: {} }].slice(-20),
    })
  }

  // ------------------------------------------------------------------ 连接
  function connect(options: AgentV2StoreOptions): void {
    if (client.value) return
    const instance = new AgentV2Client({
      ...options,
      requestTimeoutMs: options.requestTimeoutMs ?? 30000,
      onOpen: (active, reconnected) => {
        set(setConnection(state.value, { status: 'open', attempts: 0, message: null }))
        void resubscribeAll(active, reconnected)
      },
      onClose: (_active, code, reason) => {
        set(
          setConnection(state.value, {
            status: 'reconnecting',
            attempts: (state.value.connection.attempts ?? 0) + 1,
            message: `连接已断开（${code}）；正在自动重连`,
          }),
        )
        if (reason) pushError(new Error(reason))
      },
    })
    instance.subscribe((event) => {
      if (event.type === 'notification' && event.notification) handleNotification(event.notification)
      if (event.type === 'status' && event.status) {
        set(
          setConnection(state.value, {
            status: event.status,
            attempts: event.attempts ?? state.value.connection.attempts,
          }),
        )
      }
    })
    client.value = instance
    instance.connect()
  }

  function disconnect(): void {
    client.value?.close()
    client.value = null
    subscribed.value = []
    set(setConnection(state.value, { status: 'closed' }))
  }

  /** 重连后按最后游标重新订阅：只补事件，不重新调用模型。 */
  async function resubscribeAll(active: AgentV2Client, reconnected: boolean): Promise<void> {
    for (const threadId of subscribed.value) {
      try {
        const from = state.value.cursors[threadId] ?? 0
        const result = unwrap(
          await active.request(METHOD.threadSubscribe, {
            thread_id: threadId,
            after_sequence: from,
          }),
        )
        if (typeof result.cursor === 'number') {
          set(setThreadCursor(state.value, threadId, result.cursor))
        }
        if (reconnected && typeof result.pending === 'number' && result.pending > 0) {
          await backfill(threadId)
        }
      } catch (error) {
        if (isStaleCursor(error)) await resumeThread(threadId)
        else pushError(error)
      }
    }
  }

  // ------------------------------------------------------------------ Thread
  async function listThreads(includeArchived = false): Promise<ThreadView[]> {
    const result = await call(METHOD.threadList, { include_archived: includeArchived })
    const rows = (result.threads ?? []) as ThreadView[]
    let next = state.value
    for (const row of rows) {
      next = applyResumeSnapshot(next, {
        thread: {
          ...(next.threads[row.thread_id] ?? ({} as ThreadView)),
          ...row,
          settings: next.threads[row.thread_id]?.settings ?? {},
          permission_summary: next.threads[row.thread_id]?.permission_summary ?? {},
          path: [row.thread_id],
          forked_from: row.forked_from ?? null,
          contract: PROTOCOL_VERSION,
        } as ThreadView,
      })
    }
    set(next)
    return rows
  }

  async function createThread(name: string, options: { scenario?: string; cwd?: string; model?: string } = {}): Promise<ThreadView> {
    const params: Record<string, unknown> = { name }
    if (options.cwd) params.cwd = options.cwd
    if (options.model) params.model = options.model
    if (options.scenario) params.settings = { scenario: options.scenario }
    const result = await call(METHOD.threadStart, params, { idempotencyKey: `thread:${name}:${Date.now()}` })
    const thread = result.thread as ThreadView
    set(applyResumeSnapshot(state.value, { thread, last_sequence: Number(result.last_sequence ?? 0) }))
    await subscribeThread(thread.thread_id)
    return thread
  }

  async function resumeThread(threadId: string): Promise<void> {
    const snapshot = await call(METHOD.threadResume, { thread_id: threadId })
    set(
      applyResumeSnapshot(state.value, {
        thread: snapshot.thread as ThreadView,
        turns: snapshot.turns as TurnView[],
        items: snapshot.items as ItemView[],
        tool_calls: snapshot.tool_calls as ToolCallView[],
        approvals: snapshot.approvals as ApprovalView[],
        children: snapshot.children as string[],
        mailbox: snapshot.mailbox as never,
        last_sequence: Number(snapshot.last_sequence ?? 0),
        active_turn_id: (snapshot.active_turn_id as string | null) ?? null,
      }),
    )
    if (!subscribed.value.includes(threadId)) await subscribeThread(threadId)
  }

  async function selectThread(threadId: string): Promise<void> {
    set({ ...state.value, currentThreadId: threadId })
    await resumeThread(threadId)
  }

  async function subscribeThread(threadId: string): Promise<void> {
    const from = state.value.cursors[threadId] ?? 0
    const result = await call(METHOD.threadSubscribe, { thread_id: threadId, after_sequence: from })
    if (typeof result.cursor === 'number') set(setThreadCursor(state.value, threadId, result.cursor))
    if (!subscribed.value.includes(threadId)) subscribed.value = [...subscribed.value, threadId]
    if (typeof result.pending === 'number' && result.pending > 0) await backfill(threadId)
  }

  async function unsubscribeThread(threadId: string): Promise<void> {
    await call(METHOD.threadUnsubscribe, { thread_id: threadId })
    subscribed.value = subscribed.value.filter((id) => id !== threadId)
  }

  async function updateThreadSettings(
    threadId: string,
    patch: { cwd?: string; model?: string; settings?: Record<string, unknown>; permission_summary?: Record<string, unknown> },
  ): Promise<ThreadView> {
    const result = await call(METHOD.threadSettingsUpdate, { thread_id: threadId, ...patch }, {
      idempotencyKey: `settings:${threadId}:${Date.now()}`,
    })
    const thread = result.thread as ThreadView
    set({ ...state.value, threads: { ...state.value.threads, [threadId]: thread } })
    return thread
  }

  async function deleteThread(threadId: string): Promise<void> {
    await call(METHOD.threadDelete, { thread_id: threadId }, { idempotencyKey: `delete:${threadId}` })
  }

  // ------------------------------------------------------------------ Turn
  async function startTurn(
    threadId: string,
    text: string,
    options: { retryKey?: string; model?: string } = {},
  ): Promise<TurnView> {
    busy.value = true
    const key = options.retryKey ?? lastTurnKey.get(threadId) ?? `turn:${threadId}:${Date.now()}`
    lastTurnKey.set(threadId, key)
    try {
      const params: Record<string, unknown> = { thread_id: threadId, text }
      if (options.model) params.model = options.model
      const result = await call(METHOD.turnStart, params, { idempotencyKey: key })
      const turn = result.turn as TurnView
      if (result.created) lastTurnKey.delete(threadId)
      if (turn.status === 'waiting_approval') await refreshApprovals(threadId)
      return turn
    } finally {
      busy.value = false
    }
  }

  async function steerTurn(threadId: string, turnId: string, text: string): Promise<void> {
    await call(METHOD.turnSteer, { thread_id: threadId, turn_id: turnId, text }, {
      idempotencyKey: `steer:${turnId}:${Date.now()}`,
    })
  }

  async function continueTurn(threadId: string, turnId: string, text: string): Promise<void> {
    await call(METHOD.turnContinue, { thread_id: threadId, turn_id: turnId, text }, {
      idempotencyKey: `continue:${turnId}:${Date.now()}`,
    })
  }

  async function interruptTurn(threadId: string, turnId: string, reason = '用户中断'): Promise<void> {
    await call(METHOD.turnInterrupt, { thread_id: threadId, turn_id: turnId, reason }, {
      idempotencyKey: `interrupt:${turnId}`,
    })
  }

  async function recoverTurn(threadId: string, turnId: string): Promise<void> {
    await call(METHOD.turnRecover, { thread_id: threadId, turn_id: turnId }, {
      idempotencyKey: `recover:${turnId}`,
    })
  }

  async function refreshApprovals(threadId: string): Promise<ApprovalView[]> {
    const snapshot = await call(METHOD.threadResume, { thread_id: threadId })
    set(
      applyResumeSnapshot(state.value, {
        thread: snapshot.thread as ThreadView,
        turns: snapshot.turns as TurnView[],
        items: snapshot.items as ItemView[],
        tool_calls: snapshot.tool_calls as ToolCallView[],
        approvals: snapshot.approvals as ApprovalView[],
        last_sequence: Number(snapshot.last_sequence ?? 0),
      }),
    )
    return pendingApprovals(state.value, threadId)
  }

  async function resolveApproval(
    threadId: string,
    turnId: string,
    approvalId: string,
    decision: ApprovalDecision,
  ): Promise<void> {
    await call(
      METHOD.approvalResolve,
      { thread_id: threadId, turn_id: turnId, approval_id: approvalId, decision },
      { idempotencyKey: `approval:${approvalId}:${decision}` },
    )
  }

  // ------------------------------------------------------------------ 压缩
  async function compact(threadId: string, trigger = 'manual'): Promise<void> {
    await call(METHOD.threadCompact, { thread_id: threadId, trigger }, {
      idempotencyKey: `compact:${threadId}:${Date.now()}`,
    })
    await refreshSummaries(threadId)
  }

  async function refreshSummaries(threadId: string): Promise<SummaryView[]> {
    const result = await call(METHOD.compactionList, { thread_id: threadId })
    const rows = (result.summaries ?? []) as SummaryView[]
    set(applySummaryList(state.value, threadId, rows))
    return rows
  }

  async function editSummary(threadId: string, summaryId: string, text: string): Promise<void> {
    await call(METHOD.compactionEdit, { thread_id: threadId, summary_id: summaryId, text }, {
      idempotencyKey: `summary-edit:${summaryId}:${Date.now()}`,
    })
  }

  async function restoreSummary(threadId: string, summaryId: string): Promise<void> {
    await call(METHOD.compactionRestore, { thread_id: threadId, summary_id: summaryId }, {
      idempotencyKey: `summary-restore:${summaryId}:${Date.now()}`,
    })
  }

  // ------------------------------------------------------------------ 记忆
  async function listMemories(
    scope: 'user' | 'project' | 'conversation',
    scopeId: string,
    includeDeleted = false,
  ): Promise<MemoryRecordView[]> {
    const result = await call(METHOD.memoryList, {
      scope,
      scope_id: scopeId,
      include_deleted: includeDeleted,
    })
    const rows = (result.records ?? []) as MemoryRecordView[]
    set(applyMemoryList(state.value, scope, scopeId, rows))
    return rows
  }

  async function updateMemory(
    scope: 'user' | 'project' | 'conversation',
    scopeId: string,
    text: string,
    options: { memoryId?: string; tags?: string[] } = {},
  ): Promise<void> {
    await call(
      METHOD.memoryUpdate,
      {
        scope,
        scope_id: scopeId,
        text,
        memory_id: options.memoryId,
        tags: options.tags ?? [],
        thread_id: scope === 'conversation' ? scopeId : undefined,
      },
      { idempotencyKey: `memory:${options.memoryId ?? scopeId}:${Date.now()}` },
    )
    await listMemories(scope, scopeId)
  }

  async function deleteMemory(memoryId: string, scopeId: string): Promise<void> {
    await call(METHOD.memoryDelete, { memory_id: memoryId, thread_id: scopeId }, {
      idempotencyKey: `memory-del:${memoryId}`,
    })
    await listMemories('conversation', scopeId, true)
  }

  // --------------------------------------------------------------- 子 Agent
  async function refreshChildren(threadId: string): Promise<ChildView[]> {
    const result = await call(METHOD.agentList, { thread_id: threadId })
    const rows = (result.children ?? []) as ChildView[]
    set(applyChildList(state.value, threadId, rows))
    return rows
  }

  async function waitChildren(threadId: string, childIds: string[], timeoutS = 10): Promise<void> {
    await call(
      METHOD.agentWait,
      { thread_id: threadId, child_thread_ids: childIds, timeout_s: timeoutS },
      { timeoutMs: (timeoutS + 5) * 1000 },
    )
    await refreshChildren(threadId)
  }

  async function interruptChild(threadId: string, childId: string, reason = '父 Agent 收回任务'): Promise<void> {
    await call(METHOD.agentInterrupt, { thread_id: threadId, child_thread_id: childId, reason }, {
      idempotencyKey: `child-int:${childId}`,
    })
    await refreshChildren(threadId)
  }

  // ------------------------------------------------------------------ 选择器
  function itemsOfTurn(turnId: string): ItemView[] {
    return state.value.items ? Object.values(state.value.items).filter((item) => item.turn_id === turnId) : []
  }

  function assistantText(turnId: string): string {
    const id = state.value.currentThreadId
    return id ? assistantTextFor(state.value, id, turnId) : ''
  }

  function reasoningText(turnId: string): string {
    const id = state.value.currentThreadId
    return id ? reasoningTextFor(state.value, id, turnId) : ''
  }

  function toolOutput(callId: string): string {
    return toolOutputFor(state.value, callId)
  }

  function toolCall(callId: string | null): ToolCallView | null {
    return callId ? state.value.toolCalls[callId] ?? null : null
  }

  function threadName(threadId: string): string {
    return describeThread(threadId)
  }

  return {
    // 状态
    state,
    client,
    subscribed,
    busy,
    // 计算
    currentThread,
    threads,
    connection,
    notices,
    approvals,
    turns,
    active,
    plan,
    children,
    summaries,
    compactionRuns,
    items,
    memories,
    toolCalls,
    cursor,
    backfilled,
    gapFrom,
    lastError,
    // 动作
    connect,
    disconnect,
    backfill,
    listThreads,
    createThread,
    resumeThread,
    selectThread,
    subscribeThread,
    unsubscribeThread,
    updateThreadSettings,
    deleteThread,
    startTurn,
    steerTurn,
    continueTurn,
    interruptTurn,
    recoverTurn,
    refreshApprovals,
    resolveApproval,
    compact,
    refreshSummaries,
    editSummary,
    restoreSummary,
    listMemories,
    updateMemory,
    deleteMemory,
    refreshChildren,
    waitChildren,
    interruptChild,
    itemsOfTurn,
    assistantText,
    reasoningText,
    toolOutput,
    toolCall,
    threadName,
  }
})

export type AgentV2Store = ReturnType<typeof useAgentV2Store>
export type { Ref }
