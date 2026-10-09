/**
 * 事件流 -> 渲染块的归约（纯函数，便于单测）。
 *
 * 为什么单独一层：把「什么事件渲染成什么」与「长什么样」分开。
 * 组件只负责画，规则可以脱离 Vue 测试。
 */

import type { ApprovalView, ItemView, PlanStepView, PlanView, ToolCallView } from '../../agent-v2/protocol'

/** 研究意图（确认卡的数据形状，WP-05）。 */
export interface ResearchIntent {
  question: string
  scope?: string | null
  sources?: string[]
  budget?: string | null
}

/**
 * 研究意图的关键词（**只用于决定要不要先弹确认卡**，不用于拦截）。
 *
 * 两个口径必须守住：
 * 1. 它不阻止任何请求——取消后用户仍可把原话直接发出去，跑不跑由模型决定；
 * 2. 确认前不得显示任何「检索中」进度，因为那时确实还没有任何动作发生。
 */
const RESEARCH_HINTS = [
  '调研',
  '检索',
  '文献',
  '综述',
  '综述一下',
  '找论文',
  '查文献',
  '相关研究',
  '研究现状',
  '复现',
  '实验设计',
  '创新点',
]

export function looksLikeResearch(text: string): boolean {
  const value = text.trim()
  if (value.length < 4) return false
  return RESEARCH_HINTS.some((hint) => value.includes(hint))
}

export type Block =
  | { kind: 'user'; key: string; text: string }
  | { kind: 'reasoning'; key: string; text: string }
  | { kind: 'assistant'; key: string; text: string }
  | { kind: 'tool'; key: string; call: ToolCallView; output: string }
  | { kind: 'approval'; key: string; approval: ApprovalView }
  | { kind: 'plan'; key: string; plan: PlanView }
  | { kind: 'subagent'; key: string; status: string; threadId: string; summary: string }
  | { kind: 'error'; key: string; code: string; message: string }

export interface BlockLookups {
  toolCallOf: (callId: string | null) => ToolCallView | null
  toolOutputOf: (callId: string) => string
  approvalOf: (approvalId: string) => ApprovalView | null
}

function asText(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

export function planOf(item: ItemView): PlanView {
  const steps = Array.isArray(item.payload.steps) ? (item.payload.steps as PlanStepView[]) : []
  return {
    item_id: item.item_id,
    title: asText(item.payload.title, '任务计划'),
    steps,
    note: (item.payload.note as string | null) ?? null,
    created_at: item.created_at,
  }
}

/**
 * 把 Item 列表归约成渲染块。
 *
 * `tool_result` 与 `compaction` 刻意不单独成块：前者已在对应工具卡片里展示，
 * 后者由压缩面板负责——同一个事实不在界面上出现两次。
 */
export function toBlocks(items: ItemView[], lookups: BlockLookups): Block[] {
  const out: Block[] = []
  for (const item of items) {
    const key = item.item_id
    switch (item.type) {
      case 'user_input':
        out.push({ kind: 'user', key, text: asText(item.payload.text) })
        break
      case 'reasoning':
        out.push({ kind: 'reasoning', key, text: asText(item.payload.text) })
        break
      case 'assistant_text':
        out.push({ kind: 'assistant', key, text: asText(item.payload.text) })
        break
      case 'tool_call': {
        const call = lookups.toolCallOf(item.call_id)
        if (call) out.push({ kind: 'tool', key, call, output: lookups.toolOutputOf(call.call_id) })
        break
      }
      case 'approval': {
        const approval = lookups.approvalOf(asText(item.payload.approval_id))
        if (approval) out.push({ kind: 'approval', key, approval })
        break
      }
      case 'plan':
        out.push({ kind: 'plan', key, plan: planOf(item) })
        break
      case 'subagent_result':
        out.push({
          kind: 'subagent',
          key,
          status: asText(item.payload.status, 'completed'),
          threadId: asText(item.subagent_thread_id),
          summary: asText(item.payload.summary),
        })
        break
      case 'error':
        out.push({
          kind: 'error',
          key,
          code: asText(item.payload.code, 'error'),
          message: asText(item.payload.message),
        })
        break
      default:
        break
    }
  }
  return out
}

/** 已落地的助手正文（用于判断流式缓冲是否还要显示）。 */
export function landedAssistantText(blocks: Block[]): string {
  return blocks
    .filter((block): block is Extract<Block, { kind: 'assistant' }> => block.kind === 'assistant')
    .map((block) => block.text)
    .join('')
}
