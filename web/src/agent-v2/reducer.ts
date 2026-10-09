/**
 * agent.v2 前端状态 reducer（纯函数，无框架依赖）。
 *
 * 三条硬规则（对应验收书 §6「乱序和重复事件不会重复渲染」「重连使用最后游标补齐事件」）：
 *
 * 1. **按序号去重**：`sequence <= cursors[thread]` 的事件直接丢弃，不重复渲染；
 * 2. **乱序缓冲**：`sequence > cursors[thread] + 1` 时先存进 pending，并记录缺口起点，
 *    由上层（store）触发 `thread/events/replay` 补拉；补齐后按序一次性排空；
 * 3. **只有 event 通知参与游标**：心跳 / 订阅确认 / 关闭提示用的是连接级序号，
 *    是另一个序列空间，绝不能用来推进线程游标。
 *
 * reducer 不发起任何请求、不调用模型：刷新页面时先 `thread/resume` 拿快照，
 * 再用游标补事件，绝不通过「重新调用模型」来恢复界面。
 */

import type {
  ApprovalView,
  ChildView,
  CompactionRunView,
  ErrorPayload,
  EventView,
  ItemView,
  MailboxMessageView,
  MemoryRecordView,
  NotificationFrame,
  PlanStepView,
  PlanView,
  SummaryView,
  ThreadView,
  ToolCallView,
  TurnStatus,
  TurnView,
  UsageView,
} from './protocol'

export type ConnectionStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface ConnectionState {
  status: ConnectionStatus
  attempts: number
  /** 服务端是否在关闭中（收到 server/shutting_down） */
  shuttingDown: boolean
  lastHeartbeat: string | null
  message: string | null
}

export interface StreamBuffer {
  text: string
  reasoning: string
  updatedAt: string
}

export type NoticeLevel = 'info' | 'warning' | 'error'

export interface Notice {
  level: NoticeLevel
  text: string
  threadId: string | null
  at: string
}

export interface AgentV2State {
  connection: ConnectionState
  threads: Record<string, ThreadView>
  threadOrder: string[]
  currentThreadId: string | null
  turns: Record<string, TurnView>
  turnsByThread: Record<string, string[]>
  items: Record<string, ItemView>
  itemsByThread: Record<string, string[]>
  toolCalls: Record<string, ToolCallView>
  toolCallsByThread: Record<string, string[]>
  toolOutputChunks: Record<string, string[]>
  approvals: Record<string, ApprovalView>
  approvalsByThread: Record<string, string[]>
  compactionRuns: Record<string, CompactionRunView[]>
  summaries: Record<string, SummaryView[]>
  children: Record<string, string[]>
  childInfo: Record<string, ChildView>
  mailbox: Record<string, MailboxMessageView[]>
  memories: Record<string, MemoryRecordView[]>
  plan: Record<string, PlanView>
  stream: Record<string, StreamBuffer>
  usage: Record<string, UsageView>
  /** thread -> 已连续应用的最大事件序号 */
  cursors: Record<string, number>
  /** thread -> 乱序待应用的事件（sequence -> 通知） */
  pending: Record<string, Record<number, EventView>>
  /** thread -> 当前缺口的起始序号（null 表示无缺口） */
  gapFrom: Record<string, number | null>
  /** thread -> 已补拉的事件条数（用于「已补齐 N 条事件」提示） */
  backfilled: Record<string, number>
  notices: Notice[]
  errors: ErrorPayload[]
  applied: number
  duplicates: number
  /** 乱序缓冲接收过的事件数（补拉前后都可以观测） */
  buffered: number
  lastEventAt: string | null
}

export function createAgentState(): AgentV2State {
  return {
    connection: {
      status: 'idle',
      attempts: 0,
      shuttingDown: false,
      lastHeartbeat: null,
      message: null,
    },
    threads: {},
    threadOrder: [],
    currentThreadId: null,
    turns: {},
    turnsByThread: {},
    items: {},
    itemsByThread: {},
    toolCalls: {},
    toolCallsByThread: {},
    toolOutputChunks: {},
    approvals: {},
    approvalsByThread: {},
    compactionRuns: {},
    summaries: {},
    children: {},
    childInfo: {},
    mailbox: {},
    memories: {},
    plan: {},
    stream: {},
    usage: {},
    cursors: {},
    pending: {},
    gapFrom: {},
    backfilled: {},
    notices: [],
    errors: [],
    applied: 0,
    duplicates: 0,
    buffered: 0,
    lastEventAt: null,
  }
}

function nowIso(): string {
  return new Date().toISOString()
}

/** 浅拷贝顶层容器，保证 reducer 保持纯函数（不改入参）。 */
function clone(state: AgentV2State): AgentV2State {
  return {
    ...state,
    connection: { ...state.connection },
    threads: { ...state.threads },
    threadOrder: [...state.threadOrder],
    turns: { ...state.turns },
    turnsByThread: { ...state.turnsByThread },
    items: { ...state.items },
    itemsByThread: { ...state.itemsByThread },
    toolCalls: { ...state.toolCalls },
    toolCallsByThread: { ...state.toolCallsByThread },
    toolOutputChunks: { ...state.toolOutputChunks },
    approvals: { ...state.approvals },
    approvalsByThread: { ...state.approvalsByThread },
    compactionRuns: { ...state.compactionRuns },
    summaries: { ...state.summaries },
    children: { ...state.children },
    childInfo: { ...state.childInfo },
    mailbox: { ...state.mailbox },
    memories: { ...state.memories },
    plan: { ...state.plan },
    stream: { ...state.stream },
    usage: { ...state.usage },
    cursors: { ...state.cursors },
    pending: { ...state.pending },
    gapFrom: { ...state.gapFrom },
    backfilled: { ...state.backfilled },
    notices: [...state.notices],
    errors: [...state.errors],
  }
}

function pushNotice(
  state: AgentV2State,
  level: NoticeLevel,
  text: string,
  threadId: string | null = null,
): void {
  state.notices = [...state.notices, { level, text, threadId, at: nowIso() }].slice(-50)
}

function pushUnique(state: AgentV2State, list: Record<string, string[]>, key: string, id: string): void {
  const current = list[key] ? [...list[key]] : []
  if (!current.includes(id)) {
    current.push(id)
    list[key] = current
  }
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {}
}

// --------------------------------------------------------------------------- //
// 连接
// --------------------------------------------------------------------------- //
export interface ConnectionPatch {
  status?: ConnectionStatus
  attempts?: number
  shuttingDown?: boolean
  lastHeartbeat?: string | null
  message?: string | null
}

export function setConnection(state: AgentV2State, patch: ConnectionPatch): AgentV2State {
  const next = clone(state)
  next.connection = { ...next.connection, ...patch }
  return next
}

/** 记录某线程的事件游标基线（`thread/resume` / `thread/subscribe` 之后必须调用）。 */
export function setThreadCursor(
  state: AgentV2State,
  threadId: string,
  sequence: number,
): AgentV2State {
  const next = clone(state)
  const current = next.cursors[threadId]
  if (current === undefined || sequence > current) {
    next.cursors[threadId] = Math.max(0, Math.floor(sequence))
  }
  return next
}

export function requestBackfill(state: AgentV2State, threadId: string): AgentV2State {
  const next = clone(state)
  const cursor = next.cursors[threadId] ?? 0
  next.gapFrom[threadId] = cursor + 1
  return next
}

export function clearBackfill(state: AgentV2State, threadId: string): AgentV2State {
  const next = clone(state)
  next.gapFrom[threadId] = null
  return next
}

// --------------------------------------------------------------------------- //
// 通知入口
// --------------------------------------------------------------------------- //
export function applyNotification(state: AgentV2State, note: NotificationFrame): AgentV2State {
  if (!note || note.kind !== 'notification') return state
  switch (note.method) {
    case 'event':
      return applyEventNotification(state, note)
    case 'subscription/started': {
      const next = clone(state)
      const params = asRecord(note.params)
      const cursor = typeof params.cursor === 'number' ? params.cursor : null
      if (note.thread_id && cursor !== null) {
        const current = next.cursors[note.thread_id]
        if (current === undefined || cursor > current) next.cursors[note.thread_id] = cursor
      }
      next.connection = { ...next.connection, status: 'open', message: null }
      return next
    }
    case 'subscription/cancelled':
    case 'thread/closed': {
      const next = clone(state)
      const reason = asString(asRecord(note.params).reason, 'closed')
      pushNotice(
        next,
        note.method === 'thread/closed' ? 'warning' : 'info',
        note.method === 'thread/closed'
          ? `线程订阅已关闭（${reason}），已改用全量快照重建`
          : '已取消线程订阅',
        note.thread_id,
      )
      return next
    }
    case 'server/shutting_down': {
      const next = clone(state)
      next.connection = {
        ...next.connection,
        status: 'closed',
        shuttingDown: true,
        message: '服务正在关闭，稍后会自动重连',
      }
      pushNotice(next, 'warning', '服务正在关闭：新请求会被拒绝，界面保持现状')
      return next
    }
    case 'heartbeat': {
      const next = clone(state)
      next.connection = { ...next.connection, status: 'open', lastHeartbeat: nowIso() }
      return next
    }
    case 'protocol/error': {
      const next = clone(state)
      const error = asRecord(asRecord(note.params).error) as unknown as ErrorPayload
      if (error && typeof error.code === 'string') next.errors = [...next.errors, error]
      pushNotice(next, 'error', error?.message ? String(error.message) : '协议错误')
      return next
    }
    default:
      return state
  }
}

function applyEventNotification(state: AgentV2State, note: NotificationFrame): AgentV2State {
  const threadId = note.thread_id
  if (!threadId) {
    const next = clone(state)
    pushNotice(next, 'error', '收到没有 thread_id 的事件通知')
    return next
  }
  const event: EventView = {
    contract: note.contract,
    event_id: note.event_id,
    sequence: note.sequence,
    type: asString(asRecord(note.params).type),
    created_at: asString(asRecord(note.params).created_at),
    thread_id: threadId,
    turn_id: note.turn_id ?? null,
    item_id: note.item_id ?? null,
    call_id: note.call_id ?? null,
    idempotency_key: (asRecord(note.params).idempotency_key as string | null) ?? null,
    payload: asRecord(asRecord(note.params).payload),
  }
  return applyEvent(state, event)
}

/**
 * 应用一条事件。
 *
 * 去重 / 乱序 / 排空的全部逻辑都在这里，`applyReplay` 也复用同一函数，
 * 保证「实时推送」与「游标补拉」两条路径的语义不会漂移。
 */
export function applyEvent(state: AgentV2State, event: EventView): AgentV2State {
  const threadId = event.thread_id
  if (!threadId || typeof event.sequence !== 'number') return state
  const cursor = state.cursors[threadId] ?? 0

  if (event.sequence <= cursor) {
    const next = clone(state)
    next.duplicates += 1
    return next
  }

  if (event.sequence > cursor + 1) {
    // 乱序：先缓冲，等补拉把缺口补上再按序应用
    const next = clone(state)
    const pending = { ...(next.pending[threadId] ?? {}) }
    if (pending[event.sequence]) {
      next.duplicates += 1
      return next
    }
    pending[event.sequence] = event
    next.pending[threadId] = pending
    next.gapFrom[threadId] = cursor + 1
    next.buffered += 1
    return next
  }

  let next = clone(state)
  next = applyEventBody(next, event)
  next.cursors[threadId] = event.sequence
  next.applied += 1
  next.lastEventAt = event.created_at || nowIso()

  // 排空后续连续事件
  const pending = { ...(next.pending[threadId] ?? {}) }
  let expected = event.sequence + 1
  while (pending[expected]) {
    const buffered = pending[expected]
    delete pending[expected]
    next = applyEventBody(next, buffered)
    next.cursors[threadId] = expected
    next.applied += 1
    next.lastEventAt = buffered.created_at || next.lastEventAt
    expected += 1
  }
  next.pending[threadId] = pending
  next.gapFrom[threadId] = pending[expected] ? expected : null
  return next
}

function applyEventBody(state: AgentV2State, event: EventView): AgentV2State {
  const threadId = event.thread_id
  const payload = event.payload
  switch (event.type) {
    case 'thread/created':
    case 'thread/forked': {
      const thread = asRecord(payload.thread) as unknown as ThreadView
      if (thread && thread.thread_id) upsertThread(state, thread)
      return state
    }
    case 'thread/updated': {
      const patch = asRecord(payload.patch)
      const thread = state.threads[threadId]
      if (thread) {
        state.threads = { ...state.threads, [threadId]: { ...thread, ...patch } as ThreadView }
      }
      return state
    }
    case 'thread/archived': {
      const thread = state.threads[threadId]
      if (thread) {
        state.threads = { ...state.threads, [threadId]: { ...thread, status: 'archived' } }
      }
      return state
    }
    case 'item/added': {
      const item = asRecord(payload.item) as unknown as ItemView
      if (item && item.item_id) upsertItem(state, item)
      return state
    }
    case 'model/delta': {
      appendStream(state, threadId, event.turn_id, 'text', asString(payload.text))
      return state
    }
    case 'model/reasoning_delta': {
      appendStream(state, threadId, event.turn_id, 'reasoning', asString(payload.text))
      return state
    }
    case 'model/usage': {
      state.usage = {
        ...state.usage,
        [threadId]: {
          input_tokens: num(payload.input_tokens),
          output_tokens: num(payload.output_tokens),
          cached_tokens: num(payload.cached_tokens),
          cost_usd: typeof payload.cost_usd === 'number' ? payload.cost_usd : null,
        },
      }
      return state
    }
    case 'model/retry_scheduled': {
      pushNotice(
        state,
        'warning',
        `模型调用失败，将在 ${num(payload.delay_s)} 秒后重试（第 ${num(payload.attempt)} 次）`,
        threadId,
      )
      return state
    }
    case 'model/failed': {
      pushNotice(
        state,
        'error',
        `模型调用失败：${asString(payload.message, '未知原因')}`,
        threadId,
      )
      return state
    }
    default:
      break
  }

  if (event.type.startsWith('turn/')) {
    applyTurnEvent(state, threadId, payload)
    return state
  }
  if (event.type.startsWith('tool/')) {
    applyToolEvent(state, threadId, event)
    return state
  }
  if (event.type.startsWith('model/tool_call_')) {
    applyToolEvent(state, threadId, event)
    return state
  }
  if (event.type.startsWith('approval/')) {
    const approval = asRecord(payload.approval) as unknown as ApprovalView
    if (approval && approval.approval_id) upsertApproval(state, approval)
    return state
  }
  if (event.type.startsWith('compaction/')) {
    applyCompactionEvent(state, threadId, event)
    return state
  }
  if (event.type.startsWith('agent/')) {
    applyAgentEvent(state, threadId, event)
    return state
  }
  if (event.type === 'input/requested') {
    pushNotice(state, 'info', `等待研究者补充输入：${asString(payload.prompt)}`, threadId)
    return state
  }
  return state
}

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function upsertThread(state: AgentV2State, thread: ThreadView): void {
  if (!state.threads[thread.thread_id]) state.threadOrder = [...state.threadOrder, thread.thread_id]
  state.threads = { ...state.threads, [thread.thread_id]: { ...state.threads[thread.thread_id], ...thread } }
}

function upsertTurn(state: AgentV2State, turn: TurnView): void {
  state.turns = { ...state.turns, [turn.turn_id]: { ...state.turns[turn.turn_id], ...turn } }
  const list = state.turnsByThread[turn.thread_id] ? [...state.turnsByThread[turn.thread_id]] : []
  if (!list.includes(turn.turn_id)) {
    list.push(turn.turn_id)
    state.turnsByThread = { ...state.turnsByThread, [turn.thread_id]: list }
  }
}

function upsertItem(state: AgentV2State, item: ItemView): void {
  const existing = state.items[item.item_id]
  state.items = { ...state.items, [item.item_id]: { ...existing, ...item } }
  const list = state.itemsByThread[item.thread_id] ? [...state.itemsByThread[item.thread_id]] : []
  if (!list.includes(item.item_id)) {
    list.push(item.item_id)
    list.sort((a, b) => {
      const left = state.items[a]
      const right = state.items[b]
      return (left ? left.sequence : 0) - (right ? right.sequence : 0)
    })
    state.itemsByThread = { ...state.itemsByThread, [item.thread_id]: list }
  }
  if (item.type === 'plan') {
    const steps = Array.isArray(item.payload.steps) ? (item.payload.steps as PlanStepView[]) : []
    state.plan = {
      ...state.plan,
      [item.thread_id]: {
        item_id: item.item_id,
        title: asString(item.payload.title, '任务计划'),
        steps,
        note: (item.payload.note as string | null) ?? null,
        created_at: item.created_at,
      },
    }
  }
}

function appendStream(
  state: AgentV2State,
  threadId: string,
  turnId: string | null,
  field: 'text' | 'reasoning',
  chunk: string,
): void {
  if (!turnId || !chunk) return
  const key = `${threadId}:${turnId}`
  const current = state.stream[key] ?? { text: '', reasoning: '', updatedAt: '' }
  state.stream = {
    ...state.stream,
    [key]: { ...current, [field]: current[field] + chunk, updatedAt: nowIso() },
  }
}

function applyTurnEvent(
  state: AgentV2State,
  threadId: string,
  payload: Record<string, unknown>,
): void {
  const rawTurn = asRecord(payload.turn) as unknown as TurnView
  if (rawTurn && rawTurn.turn_id) upsertTurn(state, rawTurn)
  const turnId = rawTurn?.turn_id ?? asString(payload.turn_id)
  if (!turnId) return
  const turn = state.turns[turnId]
  if (!turn) return
  const to = asString(payload.to) as TurnStatus | ''
  const updated: TurnView = { ...turn }
  if (to) updated.status = to
  if (payload.error) updated.error = asRecord(payload.error)
  if (payload.waiting) updated.waiting = asRecord(payload.waiting)
  if (payload.cancel_reason) updated.cancel_reason = asString(payload.cancel_reason)
  upsertTurn(state, updated)

  const thread = state.threads[threadId]
  if (thread) {
    const isActive = ['running', 'waiting_approval', 'waiting_input'].includes(updated.status)
    state.threads = {
      ...state.threads,
      [threadId]: { ...thread, active_turn_id: isActive ? turnId : null },
    }
  }
  if (updated.status === 'failed') {
    pushNotice(
      state,
      'error',
      `Turn 失败：${asString(asRecord(updated.error).message, '未知原因')}`,
      threadId,
    )
  }
  if (updated.status === 'interrupted') {
    pushNotice(state, 'warning', 'Turn 已被中断（不会产生成功结果）', threadId)
  }
  if (updated.status === 'waiting_approval') {
    pushNotice(state, 'info', 'Turn 正在等待研究者审批', threadId)
  }
}

function applyToolEvent(state: AgentV2State, threadId: string, event: EventView): void {
  const call = asRecord(event.payload.tool_call) as unknown as ToolCallView
  if (event.type === 'tool/output') {
    const chunk = asString(event.payload.chunk)
    const callId = event.call_id ?? asString(event.payload.call_id)
    if (callId && chunk) {
      const chunks = state.toolOutputChunks[callId] ? [...state.toolOutputChunks[callId]] : []
      chunks.push(chunk)
      state.toolOutputChunks = { ...state.toolOutputChunks, [callId]: chunks }
    }
    return
  }
  if (!call || !call.call_id) return
  state.toolCalls = { ...state.toolCalls, [call.call_id]: { ...state.toolCalls[call.call_id], ...call } }
  pushUnique(state, state.toolCallsByThread, threadId, call.call_id)
  if (call.status === 'failed' && call.error) {
    pushNotice(
      state,
      'error',
      `工具 ${call.name} 失败（${call.error.code}）：${asString(call.error.message, '未知原因')}`,
      threadId,
    )
  }
}

function upsertApproval(state: AgentV2State, approval: ApprovalView): void {
  state.approvals = {
    ...state.approvals,
    [approval.approval_id]: { ...state.approvals[approval.approval_id], ...approval },
  }
  pushUnique(state, state.approvalsByThread, approval.thread_id, approval.approval_id)
}

function applyCompactionEvent(state: AgentV2State, threadId: string, event: EventView): void {
  const phase = event.type.split('/')[1]
  const entry = {
    phase,
    sequence: event.sequence,
    created_at: event.created_at,
    ...event.payload,
  }
  const runs = state.compactionRuns[threadId] ? [...state.compactionRuns[threadId]] : []
  runs.push(entry as unknown as CompactionRunView)
  state.compactionRuns = { ...state.compactionRuns, [threadId]: runs }

  if (phase === 'started') {
    pushNotice(state, 'info', '开始压缩上下文', threadId)
  } else if (phase === 'failed') {
    pushNotice(
      state,
      'error',
      `压缩失败：${asString(asRecord(event.payload.error).message, '未知原因')}（原上下文保持不变）`,
      threadId,
    )
  } else if (phase === 'completed') {
    const summaryId = asString(event.payload.summary_id)
    const list = state.summaries[threadId] ? [...state.summaries[threadId]] : []
    const view: SummaryView = {
      summary_id: summaryId,
      text: asString(event.payload.summary),
      covered_until: num(event.payload.covered_until),
      snapshot_sequence: num(event.payload.snapshot_sequence),
      trigger: asString(event.payload.trigger, 'auto'),
      created_at: event.created_at,
      edited: false,
      active: true,
    }
    const rest = list.map((item) => ({ ...item, active: false }))
    rest.push(view)
    state.summaries = { ...state.summaries, [threadId]: rest }
    pushNotice(state, 'info', '上下文已压缩，原始历史仍可审计与恢复', threadId)
  } else if (phase === 'edited') {
    const summaryId = asString(event.payload.summary_id)
    const list = (state.summaries[threadId] ?? []).map((item) =>
      item.summary_id === summaryId
        ? { ...item, text: asString(event.payload.text), edited: true }
        : item,
    )
    state.summaries = { ...state.summaries, [threadId]: list }
  } else if (phase === 'restored') {
    const summaryId = asString(event.payload.summary_id)
    const list = (state.summaries[threadId] ?? []).map((item) => ({
      ...item,
      active: item.summary_id === summaryId,
    }))
    state.summaries = { ...state.summaries, [threadId]: list }
  }
}

function applyAgentEvent(state: AgentV2State, threadId: string, event: EventView): void {
  switch (event.type) {
    case 'agent/child_created': {
      const childId = asString(event.payload.child_thread_id)
      if (!childId) return
      pushUnique(state, state.children, threadId, childId)
      state.childInfo = {
        ...state.childInfo,
        [childId]: {
          thread_id: childId,
          name: asString(event.payload.name, '子 Agent'),
          parent_thread_id: threadId,
          path: Array.isArray(event.payload.path) ? (event.payload.path as string[]) : [threadId, childId],
          status: 'created',
          running: false,
          summary: null,
          result_item_id: null,
          error: null,
          last_sequence: 0,
          mailbox: [],
        },
      }
      return
    }
    case 'agent/message': {
      const to = asString(event.payload.to_thread_id, threadId)
      const list = state.mailbox[to] ? [...state.mailbox[to]] : []
      list.push({
        sequence: event.sequence,
        created_at: event.created_at,
        from_thread_id: asString(event.payload.from_thread_id),
        to_thread_id: to,
        content: asString(event.payload.content),
        kind: asString(event.payload.kind, 'message'),
      })
      state.mailbox = { ...state.mailbox, [to]: list }
      return
    }
    case 'agent/child_completed':
    case 'agent/child_failed':
    case 'agent/child_interrupted': {
      const childId = asString(event.payload.child_thread_id)
      const status = asString(event.payload.status, event.type.split('/')[1].replace('child_', ''))
      const existing = state.childInfo[childId]
      const parent = existing?.parent_thread_id ?? threadId
      state.childInfo = {
        ...state.childInfo,
        [childId]: {
          thread_id: childId,
          name: existing?.name ?? '子 Agent',
          parent_thread_id: parent,
          path: existing?.path ?? [parent, childId],
          status,
          running: false,
          summary: (event.payload.summary as string | null) ?? existing?.summary ?? null,
          result_item_id: asString(event.payload.item_id) || existing?.result_item_id || null,
          error: (event.payload.error as ErrorPayload | null) ?? existing?.error ?? null,
          last_sequence: event.sequence,
          mailbox: existing?.mailbox ?? [],
        },
      }
      pushNotice(
        state,
        status === 'failed' ? 'error' : 'info',
        `子 Agent ${state.childInfo[childId].name}：${status}`,
        parent,
      )
      return
    }
    default:
      return
  }
}

// --------------------------------------------------------------------------- //
// 补拉（游标重放）
// --------------------------------------------------------------------------- //
export function applyReplay(
  state: AgentV2State,
  threadId: string,
  events: EventView[],
): AgentV2State {
  let next = state
  let applied = 0
  const sorted = [...events].sort((a, b) => a.sequence - b.sequence)
  for (const event of sorted) {
    const before = next.applied
    next = applyEvent(next, { ...event, thread_id: event.thread_id || threadId })
    if (next.applied > before) applied += 1
  }
  const result = clone(next)
  result.backfilled = { ...result.backfilled, [threadId]: (result.backfilled[threadId] ?? 0) + applied }
  if (result.gapFrom[threadId] !== null && result.gapFrom[threadId] !== undefined) {
    const pendingSequences = Object.keys(result.pending[threadId] ?? {}).map(Number)
    if (pendingSequences.length === 0) result.gapFrom[threadId] = null
  }
  return result
}

// --------------------------------------------------------------------------- //
// 快照（thread/resume）
// --------------------------------------------------------------------------- //
export interface ResumeSnapshot {
  thread: ThreadView
  turns?: TurnView[]
  items?: ItemView[]
  tool_calls?: ToolCallView[]
  approvals?: ApprovalView[]
  children?: string[]
  mailbox?: MailboxMessageView[]
  last_sequence?: number
  active_turn_id?: string | null
}

/** 用服务端快照重建线程状态（刷新页面后的唯一恢复路径，不调用模型）。 */
export function applyResumeSnapshot(state: AgentV2State, snapshot: ResumeSnapshot): AgentV2State {
  const next = clone(state)
  const threadId = snapshot.thread.thread_id
  upsertThread(next, snapshot.thread)
  if (typeof snapshot.last_sequence === 'number') {
    next.cursors = { ...next.cursors, [threadId]: snapshot.last_sequence }
  }
  for (const turn of snapshot.turns ?? []) upsertTurn(next, turn)
  for (const item of snapshot.items ?? []) upsertItem(next, item)
  for (const call of snapshot.tool_calls ?? []) {
    next.toolCalls = { ...next.toolCalls, [call.call_id]: call }
    pushUnique(next, next.toolCallsByThread, threadId, call.call_id)
  }
  for (const approval of snapshot.approvals ?? []) upsertApproval(next, approval)
  for (const childId of snapshot.children ?? []) {
    pushUnique(next, next.children, threadId, childId)
  }
  if (snapshot.mailbox && snapshot.mailbox.length) {
    next.mailbox = { ...next.mailbox, [threadId]: [...snapshot.mailbox] }
  }
  next.currentThreadId = threadId
  return next
}

export function applySummaryList(state: AgentV2State, threadId: string, summaries: SummaryView[]): AgentV2State {
  const next = clone(state)
  next.summaries = { ...next.summaries, [threadId]: summaries }
  return next
}

export function applyMemoryList(
  state: AgentV2State,
  scope: string,
  scopeId: string,
  records: MemoryRecordView[],
): AgentV2State {
  const next = clone(state)
  next.memories = { ...next.memories, [`${scope}:${scopeId}`]: records }
  return next
}

export function applyChildList(state: AgentV2State, threadId: string, children: ChildView[]): AgentV2State {
  const next = clone(state)
  const ids = children.map((child) => child.thread_id)
  next.children = { ...next.children, [threadId]: ids }
  const info = { ...next.childInfo }
  for (const child of children) info[child.thread_id] = { ...info[child.thread_id], ...child }
  next.childInfo = info
  return next
}

// --------------------------------------------------------------------------- //
// 选择器
// --------------------------------------------------------------------------- //
export function orderedItems(state: AgentV2State, threadId: string): ItemView[] {
  const ids = state.itemsByThread[threadId] ?? []
  return ids.map((id) => state.items[id]).filter((item): item is ItemView => Boolean(item))
}

export function itemsForTurn(state: AgentV2State, turnId: string): ItemView[] {
  return Object.values(state.items)
    .filter((item) => item.turn_id === turnId)
    .sort((a, b) => a.sequence - b.sequence)
}

export function activeTurn(state: AgentV2State, threadId: string): TurnView | null {
  const thread = state.threads[threadId]
  const activeId = thread?.active_turn_id ?? null
  if (activeId && state.turns[activeId]) return state.turns[activeId]
  const ids = state.turnsByThread[threadId] ?? []
  for (let i = ids.length - 1; i >= 0; i -= 1) {
    const turn = state.turns[ids[i]]
    if (turn && ['running', 'waiting_approval', 'waiting_input'].includes(turn.status)) return turn
  }
  return null
}

export function pendingApprovals(state: AgentV2State, threadId: string): ApprovalView[] {
  const ids = state.approvalsByThread[threadId] ?? []
  return ids
    .map((id) => state.approvals[id])
    .filter((approval): approval is ApprovalView => Boolean(approval) && approval.status === 'pending')
    .sort((a, b) => a.created_at.localeCompare(b.created_at))
}

export function latestPlan(state: AgentV2State, threadId: string): PlanView | null {
  return state.plan[threadId] ?? null
}

export function childList(state: AgentV2State, threadId: string): ChildView[] {
  return (state.children[threadId] ?? [])
    .map((id) => state.childInfo[id])
    .filter((child): child is ChildView => Boolean(child))
}

export function streamFor(state: AgentV2State, threadId: string, turnId: string | null): StreamBuffer {
  if (!turnId) return { text: '', reasoning: '', updatedAt: '' }
  return state.stream[`${threadId}:${turnId}`] ?? { text: '', reasoning: '', updatedAt: '' }
}

/** 助手文本：优先用已落地的 Item（权威），否则用正在流式的缓冲。 */
export function assistantTextFor(state: AgentV2State, threadId: string, turnId: string): string {
  const fromItems = itemsForTurn(state, turnId)
    .filter((item) => item.type === 'assistant_text')
    .map((item) => asString(item.payload.text))
    .join('')
  if (fromItems) return fromItems
  return streamFor(state, threadId, turnId).text
}

export function reasoningTextFor(state: AgentV2State, threadId: string, turnId: string): string {
  const fromItems = itemsForTurn(state, turnId)
    .filter((item) => item.type === 'reasoning')
    .map((item) => asString(item.payload.text))
    .join('')
  if (fromItems) return fromItems
  return streamFor(state, threadId, turnId).reasoning
}

export function toolCallFor(state: AgentV2State, callId: string | null): ToolCallView | null {
  return callId ? state.toolCalls[callId] ?? null : null
}

export function toolOutputFor(state: AgentV2State, callId: string): string {
  return (state.toolOutputChunks[callId] ?? []).join('\n')
}
