/**
 * agent.v2 前端状态类型（WP-03 的 `types.ts`）。
 *
 * 与 `protocol.ts` 的分工：`protocol.ts` 描述**服务端发来的帧**，
 * 这里描述**前端自己维护的视图状态**。两者都不含 Vue 依赖，便于纯函数测试。
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
  PlanView,
  SummaryView,
  ThreadView,
  ToolCallView,
  TurnView,
  UsageView,
} from './protocol'

export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed'

export interface ConnectionState {
  status: ConnectionStatus
  /** 已尝试重连次数（界面只在异常时提示，不展示具体数字）。 */
  attempts: number
  /** 服务端已声明关闭中。 */
  shuttingDown: boolean
  /** 本连接是否具备写权限（服务端在 protocol/ready 里告知）。 */
  owner: boolean
  lastHeartbeat: string | null
  message: string | null
}

/** 连接状态的局部更新（reducer 的 setConnection 入参）。 */
export interface ConnectionPatch {
  status?: ConnectionStatus
  attempts?: number
  shuttingDown?: boolean
  owner?: boolean
  lastHeartbeat?: string | null
  message?: string | null
}

/** 正在流式中的文本缓冲（每个 Turn 一份）。 */
export interface StreamBuffer {
  text: string
  reasoning: string
  updatedAt: string
}

export type NoticeLevel = 'info' | 'warning' | 'error'

/** 面向用户的提示（界面只展示这些，不展示内部状态）。 */
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
  /** thread -> 已连续应用的最大事件序号（游标）。 */
  cursors: Record<string, number>
  /** thread -> 乱序待应用的事件（sequence -> 事件）。 */
  pending: Record<string, Record<number, EventView>>
  /** thread -> 当前缺口起始序号（null 表示无缺口）。 */
  gapFrom: Record<string, number | null>
  /** thread -> 已通过补拉补齐的事件条数。 */
  backfilled: Record<string, number>
  notices: Notice[]
  errors: ErrorPayload[]
  /** 已应用事件数（测试与诊断用）。 */
  applied: number
  /** 因重复被丢弃的事件数。 */
  duplicates: number
  /** 因乱序进入缓冲的事件数。 */
  buffered: number
  /** 因所属 Turn 已终结而被丢弃的迟到事件数（WP-03 规则 4/5）。 */
  lateDropped: number
  lastEventAt: string | null
}
