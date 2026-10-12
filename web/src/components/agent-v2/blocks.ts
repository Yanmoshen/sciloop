/**
 * 事件流 -> 渲染块的归约（纯函数，便于单测）。
 *
 * 为什么单独一层：把「什么事件渲染成什么」与「长什么样」分开。
 * 组件只负责画，规则可以脱离 Vue 测试。
 */

import type { ApprovalView, ItemView, PlanStepView, PlanView, ToolCallView } from '../../agent-v2/protocol'
import { sanitizeAssistantText } from '../../utils/messages'

/** 研究意图（确认卡的数据形状，WP-05）。 */
export interface ResearchIntent {
  question: string
  scope?: string | null
  sources?: string[]
  budget?: string | null
}

/**
 * 只有明确的执行表达才进入研究路径。
 * 单独提到「文献」「研究」「分析」仍是普通对话，不能在前端擅自
 * 弹出确认卡或改变提交路径。
 */
export function looksLikeResearch(text: string): boolean {
  const value = text.trim().replace(/[？?。！!]+$/u, '')
  if (value.length < 4) return false
  const topic = '(?:调研|检索|查找|搜索|研究|综述|找论文|查文献|实验设计|复现)'
  return new RegExp(
    `^(?:开始|启动|进行|执行)\\s*(?:文献|论文|研究|${topic})|^从(?:文献|论文|研究)${topic}开始|^(?:帮我|请(?:你)?|现在(?:就)?|直接)\\s*(?:开始|启动|进行|执行)?\\s*${topic}`,
    'u',
  ).test(value)
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

/**
 * 思考区只接受模型明确标记的短摘要。
 * `payload.text` 可能是供应商返回的完整 reasoning_content，它只用于
 * 服务端继续生成，不能在浏览器里展开。旧事件没有摘要字段时直接隐藏。
 */
function reasoningSummary(payload: Record<string, unknown>): string {
  const summary = payload.summary ?? payload.reasoning_summary ?? payload.display_summary
  return typeof summary === 'string' ? summary.trim() : ''
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
      case 'reasoning': {
        const summary = reasoningSummary(item.payload)
        if (summary) out.push({ kind: 'reasoning', key, text: summary })
        break
      }
      case 'assistant_text':
        out.push({ kind: 'assistant', key, text: sanitizeAssistantText(asText(item.payload.text)) })
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

/** 将流式缓冲与已落地正文对齐，只显示尚未落地的后缀。 */
export function pendingAssistantText(blocks: Block[], streamText: string): string {
  const safeStream = sanitizeAssistantText(streamText)
  if (!safeStream) return ''
  const landed = landedAssistantText(blocks)
  if (!landed) return safeStream
  if (landed === safeStream || landed.includes(safeStream)) return ''
  if (safeStream.startsWith(landed)) return safeStream.slice(landed.length)
  return safeStream
}
