/**
 * agent.v2 前端协议类型（与冻结契约 `agent.v2.contract.v1`、协议 `agent.v2.protocol.v1` 对应）。
 *
 * 这里只声明结构，不做任何运行时校验：服务端是单一事实来源，前端的职责是
 * 忠实渲染事件流。字段名一律 snake_case，与服务端保持一致，避免两套命名互相翻译。
 */

/** 协议版本（与 `server/api/v2/agent_protocol/version.py` 一致）。 */
export const PROTOCOL_VERSION = 'agent.v2.protocol.v1'

/** 契约版本（与 `server/contracts/agent_v2/version.py` 一致）。 */
export const CONTRACT_VERSION = 'agent.v2.contract.v1'

export type FrameKind = 'request' | 'response' | 'notification'

export interface ErrorPayload {
  code: string
  message: string
  data: Record<string, unknown>
}

export interface RequestFrame {
  contract: string
  kind: 'request'
  id: string
  method: string
  params: Record<string, unknown>
  idempotency_key?: string | null
}

export interface ResponseFrame {
  contract: string
  kind: 'response'
  id: string
  ok: boolean
  result: Record<string, unknown> | null
  error: ErrorPayload | null
  replayed?: boolean
}

/** 通知通道。只有 `event` 参与事件游标；其余是连接/订阅级控制通知。 */
export type NotificationMethod =
  | 'event'
  | 'protocol/ready'
  | 'subscription/started'
  | 'subscription/cancelled'
  | 'thread/closed'
  | 'server/shutting_down'
  | 'heartbeat'
  | 'protocol/error'

export interface NotificationFrame {
  contract: string
  kind: 'notification'
  method: NotificationMethod
  event_id: string
  /**
   * 事件通知：该线程内的事件序号（可作游标）。
   * 控制通知：连接内自增序号，**不参与事件游标**（绝不能拿它推进 thread 游标）。
   */
  sequence: number
  thread_id: string | null
  turn_id?: string | null
  item_id?: string | null
  call_id?: string | null
  params: Record<string, unknown>
}

export type TurnStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'waiting_input'
  | 'interrupted'
  | 'failed'
  | 'completed'

export type ToolCallStatus =
  | 'requested'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | 'invalid_arguments'

export type ItemType =
  | 'user_input'
  | 'assistant_text'
  | 'reasoning'
  | 'tool_call'
  | 'tool_result'
  | 'plan'
  | 'compaction'
  | 'subagent_activity'
  | 'subagent_result'
  | 'approval'
  | 'error'

export interface ThreadView {
  thread_id: string
  name: string
  status: 'active' | 'archived'
  settings: Record<string, unknown>
  cwd: string | null
  model: string | null
  permission_summary: Record<string, unknown>
  active_turn_id: string | null
  last_sequence: number
  parent_thread_id: string | null
  path: string[]
  forked_from: Record<string, unknown> | null
  created_at: string
  updated_at: string
  contract: string
}

export interface TurnView {
  turn_id: string
  thread_id: string
  status: TurnStatus
  created_at: string
  updated_at: string
  sequence_start: number
  sequence_end: number | null
  input_item_ids: string[]
  idempotency_key: string | null
  attempt: number
  error: Record<string, unknown> | null
  waiting: Record<string, unknown> | null
  cancel_reason: string | null
  contract: string
}

export interface ItemView {
  item_id: string
  thread_id: string
  turn_id: string | null
  type: ItemType
  sequence: number
  call_id: string | null
  subagent_thread_id: string | null
  payload: Record<string, unknown>
  created_at: string
  contract: string
}

export interface ToolCallView {
  call_id: string
  name: string
  kind: 'read_only' | 'side_effect'
  status: ToolCallStatus
  thread_id: string
  turn_id: string
  item_id?: string | null
  arguments: Record<string, unknown>
  output: Record<string, unknown> | null
  error: ErrorPayload | null
  requested_at: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  attempt: number
  truncated: boolean
  contract: string
}

export type ApprovalStatus = 'pending' | 'granted' | 'denied' | 'expired'

/** 审批决策：批准一次 / 本对话始终批准 / 完全访问 / 拒绝 / 取消。 */
export type ApprovalDecision =
  | 'approve_once'
  | 'approve_conversation'
  | 'full_access'
  | 'deny'
  | 'cancel'

export interface ApprovalView {
  approval_id: string
  thread_id: string
  turn_id: string
  call_id: string | null
  status: ApprovalStatus
  action: {
    tool?: string
    arguments?: Record<string, unknown>
    kind?: string
    risk?: string
  }
  risk: string
  created_at: string
  decided_at: string | null
  decided_by: string | null
  decision_scope: string | null
  contract: string
}

export interface MemoryRecordView {
  memory_id: string
  scope: 'user' | 'project' | 'conversation'
  scope_id: string
  text: string
  version: number
  created_at: string
  updated_at: string
  origin: 'auto' | 'user'
  edited_by_user: boolean
  deleted: boolean
  source_event_ids: string[]
  tags: string[]
  contract: string
}

export interface EventView {
  contract: string
  event_id: string
  sequence: number
  type: string
  created_at: string
  thread_id: string
  turn_id: string | null
  item_id: string | null
  call_id: string | null
  idempotency_key: string | null
  payload: Record<string, unknown>
}

export interface SummaryView {
  summary_id: string
  text: string | null
  covered_until: number | null
  snapshot_sequence: number | null
  trigger: string | null
  created_at: string | null
  edited: boolean
  active: boolean
  updated_at?: string
  edited_by?: string
}

export interface ChildView {
  thread_id: string
  name: string
  parent_thread_id: string | null
  path: string[]
  status: string
  running: boolean
  summary: string | null
  result_item_id: string | null
  error: ErrorPayload | null
  last_sequence: number
  mailbox: MailboxMessageView[]
}

export interface MailboxMessageView {
  sequence: number
  created_at: string
  from_thread_id: string
  to_thread_id: string
  content: string
  kind: string
}

export interface PlanStepView {
  index: number
  title: string
  status: string
}

export interface PlanView {
  item_id: string
  title: string
  steps: PlanStepView[]
  note?: string | null
  created_at: string
}

export interface UsageView {
  input_tokens?: number
  output_tokens?: number
  cached_tokens?: number
  cost_usd?: number | null
}

export interface CompactionRunView {
  ok: boolean
  trigger: string
  summary_id: string | null
  covered_until: number | null
  snapshot_sequence: number | null
  tokens_before: number
  tokens_after: number
  error: ErrorPayload | null
}

/** 协议方法名（与 `server/api/v2/agent_protocol/methods.py` 的注册表一一对应）。 */
export const METHOD = {
  describe: 'protocol/describe',
  threadStart: 'thread/start',
  threadList: 'thread/list',
  threadResume: 'thread/resume',
  threadSettingsUpdate: 'thread/settings/update',
  threadDelete: 'thread/delete',
  turnStart: 'turn/start',
  turnSteer: 'turn/steer',
  turnContinue: 'turn/continue',
  turnInterrupt: 'turn/interrupt',
  turnRecover: 'turn/recover',
  threadSubscribe: 'thread/subscribe',
  threadUnsubscribe: 'thread/unsubscribe',
  threadReplay: 'thread/events/replay',
  approvalResolve: 'approval/resolve',
  threadCompact: 'thread/compact',
  compactionList: 'thread/compaction/list',
  compactionEdit: 'thread/compaction/edit',
  compactionRestore: 'thread/compaction/restore',
  memoryList: 'memory/list',
  memoryUpdate: 'memory/update',
  memoryDelete: 'memory/delete',
  agentList: 'agent/list',
  agentWait: 'agent/wait',
  agentInterrupt: 'agent/interrupt',
} as const

export type MethodName = (typeof METHOD)[keyof typeof METHOD]

/** 需要幂等键的方法（变更类）：断线重试必须复用同一个键。 */
export const MUTATING_METHODS: readonly string[] = [
  METHOD.threadStart,
  METHOD.threadSettingsUpdate,
  METHOD.threadDelete,
  METHOD.turnStart,
  METHOD.turnSteer,
  METHOD.turnContinue,
  METHOD.turnInterrupt,
  METHOD.turnRecover,
  METHOD.approvalResolve,
  METHOD.threadCompact,
  METHOD.compactionEdit,
  METHOD.compactionRestore,
  METHOD.memoryUpdate,
  METHOD.memoryDelete,
  METHOD.agentInterrupt,
]

export function isMutating(method: string): boolean {
  return MUTATING_METHODS.includes(method)
}
