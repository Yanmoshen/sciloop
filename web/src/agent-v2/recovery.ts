/**
 * 快照持久化与恢复（WP-03 的 `recovery.ts`、WP-06「页面卸载后保留快照和 cursor」）。
 *
 * 设计取舍：
 * - **只持久化可序列化的纯数据**（线程、Turn、Item、工具调用、审批、摘要、计划、子 Agent、游标）；
 * - **只保留当前会话的明细**，其他线程只留索引与游标，避免把整库塞进 localStorage；
 * - 恢复时**先渲染快照，再由 store 用游标补事件**——绝不通过重新调用模型来恢复界面；
 * - 存储不可用（隐私模式、配额满）时静默降级，界面仍能从服务端快照重建。
 */

import type { AgentV2State } from './types'

export const SNAPSHOT_VERSION = 1
export const SNAPSHOT_KEY = 'sciloop.agent-v2.snapshot'

/** 只依赖这三个方法，便于在无浏览器的测试里注入替身。 */
export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

export interface PersistedSnapshot {
  version: number
  saved_at: string
  current_thread_id: string | null
  thread_order: string[]
  threads: AgentV2State['threads']
  cursors: Record<string, number>
  turns: AgentV2State['turns']
  turns_by_thread: Record<string, string[]>
  items: AgentV2State['items']
  items_by_thread: Record<string, string[]>
  tool_calls: AgentV2State['toolCalls']
  tool_calls_by_thread: Record<string, string[]>
  approvals: AgentV2State['approvals']
  approvals_by_thread: Record<string, string[]>
  summaries: AgentV2State['summaries']
  plan: AgentV2State['plan']
  children: AgentV2State['children']
  child_info: AgentV2State['childInfo']
  memories: AgentV2State['memories']
}

/** 从内存状态切出快照（当前会话明细 + 全部线程索引与游标）。 */
export function toPersisted(state: AgentV2State, now: string = new Date().toISOString()): PersistedSnapshot {
  return {
    version: SNAPSHOT_VERSION,
    saved_at: now,
    current_thread_id: state.currentThreadId,
    thread_order: [...state.threadOrder],
    threads: { ...state.threads },
    cursors: { ...state.cursors },
    turns: { ...state.turns },
    turns_by_thread: { ...state.turnsByThread },
    items: { ...state.items },
    items_by_thread: { ...state.itemsByThread },
    tool_calls: { ...state.toolCalls },
    tool_calls_by_thread: { ...state.toolCallsByThread },
    approvals: { ...state.approvals },
    approvals_by_thread: { ...state.approvalsByThread },
    summaries: { ...state.summaries },
    plan: { ...state.plan },
    children: { ...state.children },
    child_info: { ...state.childInfo },
    memories: { ...state.memories },
  }
}

export interface RestoredSnapshot {
  currentThreadId: string | null
  savedAt: string
  applied: Partial<AgentV2State>
}

/** 校验并还原快照；版本不符或结构损坏时返回 null（绝不抛异常打断启动）。 */
export function fromPersisted(raw: unknown): RestoredSnapshot | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Partial<PersistedSnapshot>
  if (value.version !== SNAPSHOT_VERSION) return null
  if (!value.threads || !value.cursors) return null
  return {
    currentThreadId: value.current_thread_id ?? null,
    savedAt: value.saved_at ?? '',
    applied: {
      threads: value.threads,
      threadOrder: value.thread_order ?? [],
      cursors: value.cursors,
      turns: value.turns ?? {},
      turnsByThread: value.turns_by_thread ?? {},
      items: value.items ?? {},
      itemsByThread: value.items_by_thread ?? {},
      toolCalls: value.tool_calls ?? {},
      toolCallsByThread: value.tool_calls_by_thread ?? {},
      approvals: value.approvals ?? {},
      approvalsByThread: value.approvals_by_thread ?? {},
      summaries: value.summaries ?? {},
      plan: value.plan ?? {},
      children: value.children ?? {},
      childInfo: value.child_info ?? {},
      memories: value.memories ?? {},
    },
  }
}

export function saveSnapshot(storage: StorageLike | null, state: AgentV2State): boolean {
  if (!storage) return false
  try {
    storage.setItem(SNAPSHOT_KEY, JSON.stringify(toPersisted(state)))
    return true
  } catch {
    return false
  }
}

export function loadSnapshot(storage: StorageLike | null): RestoredSnapshot | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(SNAPSHOT_KEY)
    if (!raw) return null
    return fromPersisted(JSON.parse(raw))
  } catch {
    return null
  }
}

export function clearSnapshot(storage: StorageLike | null): void {
  if (!storage) return
  try {
    storage.removeItem(SNAPSHOT_KEY)
  } catch {
    /* 存储不可用时无事可做 */
  }
}

/** 浏览器环境下的 localStorage（不可用时返回 null，例如隐私模式或非浏览器环境）。 */
export function browserStorage(): StorageLike | null {
  try {
    if (typeof globalThis === 'undefined') return null
    const candidate = (globalThis as unknown as { localStorage?: StorageLike }).localStorage
    if (!candidate) return null
    const probe = '__sciloop_probe__'
    candidate.setItem(probe, '1')
    candidate.removeItem(probe)
    return candidate
  } catch {
    return null
  }
}
