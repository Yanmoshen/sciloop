/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究节点编排层：七节点图的程序主控执行。
 *
 * 端点（后端 `app/api/v1/research.py`）：
 * - `GET  /research/nodes`                        节点定义与校验规则目录（公开）
 * - `GET  /research/projects/{id}/state`          链状态 + 迁移留痕（公开）
 * - `POST /research/projects/{id}/run`            执行节点（SSE：meta/node/attempt/
 *                                                 validation/notice/revert/migrated/
 *                                                 waiting_human/done/error，需 Owner）
 * - `POST /research/projects/{id}/revert`         研究者发起回退（同一套闸门，需 Owner）
 * - `GET  /research/projects/{id}/preflight`      预检记录（公开）
 * - `POST /research/projects/{id}/preflight`      执行一次预检（需 Owner）
 * - `GET|PUT /research/projects/{id}/access`      执行授权模式
 *
 * 流式为什么裸用 fetch：与 `chat.ts` 同因——`request()` 会等完整响应体，
 * 那等于没有流式。这里只用 `apiUrl()` 与 `parseError()`，baseURL 与错误口径仍由 client.ts 独占。
 */

import { apiUrl, getOwnerToken, parseError } from '@/api/client'
import { liveStatus, writeDenied } from '@/utils/messages'

/** 程序内部六态（存储用）与文档五态（展示用）——展示映射由后端给出，前端不另写一份 */
export interface ResearchNode {
  node: string
  label: string
  status: string
  display_status: string
  implemented: boolean
  entry_index: number
  retry_count: number
  max_retry: number
  cost_usd: number
  llm_call_count: number
  validation: ValidationDetail | null
  finished_at: string | null
}

export interface RuleItem {
  rule: string
  level: string
  message: string
  path?: string | null
}

export interface ValidationDetail {
  ok: boolean
  level: string | null
  rules: string[]
  items: RuleItem[]
}

export interface Transition {
  id: number
  from_node: string | null
  to_node: string
  kind: 'advance' | 'revert' | 'retry' | 'stop'
  trigger: 'program' | 'model' | 'researcher'
  reason: string
  required_carried: Record<string, unknown> | null
  created_at: string | null
}

export interface PreflightRecord {
  level: string
  command: string
  exit_code: number
  duration_ms: number
  artifact_path: string | null
  log_path: string
  note: string
  ok?: boolean
}

export interface ChainState {
  conversation_id: string
  project_id: number | null
  has_chain: boolean
  nodes: ResearchNode[]
  current_node: string
  transitions: Transition[]
  total_reverts: number
  max_total_reverts: number
  preflight: PreflightRecord | null
}

export interface NodeRule {
  rule: string
  level: string
  message: string
}

export interface NodeDefinition {
  node: string
  label: string
  implemented: boolean
  next: string | null
  revert_targets: string[]
  rules: NodeRule[]
  min_evidence_count: number
}

export interface NodesCatalog {
  nodes: NodeDefinition[]
  max_retry: number
  max_revisit: number
  max_total_reverts: number
  gates: string[]
  revert_requirements: Record<string, string>
  retrospective: { node: string; label: string; note: string }
}

export interface PreflightInput {
  command: string
  cwd?: string | null
  timeout_s?: number
  approved?: boolean
}

/** 节点执行期间从流里推出来的一条事件（前端只用于渲染，不是持久化记录） */
export type ResearchEvent =
  | { type: 'meta'; payload: Record<string, unknown> }
  | { type: 'node'; payload: Record<string, unknown> }
  | { type: 'attempt'; payload: Record<string, unknown> }
  | { type: 'validation'; payload: ValidationDetail & { attempt: number; max_attempts: number } }
  | { type: 'notice'; payload: { code: string; message: string; detail?: string } }
  | { type: 'revert'; payload: Record<string, unknown> }
  | { type: 'migrated'; payload: Record<string, unknown> }
  | { type: 'waiting_human'; payload: Record<string, unknown> }
  | { type: 'done'; payload: Record<string, unknown> }
  | { type: 'error'; payload: { code: string; message: string } }

function ownerHeader(): Record<string, string> {
  const token = getOwnerToken()
  return token ? { 'X-Owner-Token': token } : {}
}

export async function fetchNodes(): Promise<NodesCatalog> {
  const response = await fetch(apiUrl('/research/nodes'), {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as NodesCatalog
}

export async function fetchChainState(conversationId: string): Promise<ChainState> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/state`), {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as ChainState
}

export async function fetchPreflights(conversationId: string): Promise<PreflightRecord[]> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/preflight`), {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await parseError(response)
  const body = (await response.json()) as { records?: PreflightRecord[] }
  return body.records ?? []
}

export async function runPreflight(
  conversationId: string,
  input: PreflightInput,
): Promise<PreflightRecord> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/preflight`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...ownerHeader() },
    body: JSON.stringify(input),
  })
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as PreflightRecord
}

export async function requestRevert(
  conversationId: string,
  input: {
    from_node: string
    target: string
    reason: string
    carried: Record<string, unknown>
  },
): Promise<Record<string, unknown>> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/revert`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...ownerHeader() },
    body: JSON.stringify(input),
  })
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as Record<string, unknown>
}

export async function fetchAccessMode(
  conversationId: string,
): Promise<{ execution_access: 'ask' | 'trusted' }> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/access`), {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as { execution_access: 'ask' | 'trusted' }
}

export async function setAccessMode(
  conversationId: string,
  mode: 'ask' | 'trusted',
): Promise<void> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/access`), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...ownerHeader() },
    body: JSON.stringify({ execution_access: mode }),
  })
  if (!response.ok) throw await parseError(response)
}

export interface RunNodeInput {
  node?: string | null
  text?: string
}

export interface RunNodeHandlers {
  onEvent: (event: ResearchEvent) => void
}

/**
 * 执行一次研究节点（真流式）。返回时代表整个响应已消费完。
 *
 * 网络层异常会抛出 `ApiError`（由视图层提示）；执行过程中的失败以后端
 * `error` 事件回来（此时 HTTP 已是 200，因为响应头早已发送）。
 */
export async function streamRunNode(
  conversationId: string,
  input: RunNodeInput,
  handlers: RunNodeHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(apiUrl(`/research/conversations/${conversationId}/run`), {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      ...ownerHeader(),
    },
    body: JSON.stringify({ node: input.node ?? null, text: input.text ?? '' }),
    signal,
  })
  if (!response.ok) throw await parseError(response)
  if (!response.body) throw new Error('浏览器不支持流式响应（response.body 为空）')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const parsed = parseFrame(frame)
        if (parsed) handlers.onEvent(parsed)
        boundary = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) {
      const parsed = parseFrame(buffer)
      if (parsed) handlers.onEvent(parsed)
    }
  } finally {
    reader.releaseLock()
  }
}

function parseFrame(frame: string): ResearchEvent | null {
  let event = ''
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!event || !dataLines.length) return null
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
  } catch {
    return null
  }
  const known = [
    'meta',
    'node',
    'attempt',
    'validation',
    'notice',
    'revert',
    'migrated',
    'waiting_human',
    'done',
    'error',
  ]
  if (!known.includes(event)) return null
  return { type: event, payload } as ResearchEvent
}

/** 节点状态的展示文案（含说明「点了会发生什么」，不暴露内部字段名） */
export function nodeStatusText(node: ResearchNode): string {
  switch (node.status) {
    case 'running':
      return '进行中'
    case 'done':
      return '已通过'
    case 'waiting_human':
      return '待人工介入'
    case 'failed':
      return '执行失败'
    case 'blocked':
      return '被阻塞'
    default:
      return '未开始'
  }
}

/** 节点状态的语义色（走设计令牌，不写死颜色） */
export function nodeStatusTone(node: ResearchNode): string {
  switch (node.status) {
    case 'running':
      return 'info'
    case 'done':
      return 'ok'
    case 'waiting_human':
      return 'warn'
    case 'failed':
    case 'blocked':
      return 'err'
    default:
      return 'idle'
  }
}

export const RESEARCH_TEXT = {
  /** 未选中项目时不给按钮，避免"点了没反应" */
  needProject: '先在左栏选择或新建一个项目，研究流程会挂在项目上',
  runIdle: '运行本节点',
  runBusy: '正在执行…',
  /** 运行是写操作且会产生真实费用 */
  runDenied: writeDenied('执行研究节点'),
  preflightDenied: writeDenied('执行预检'),
  accessDenied: writeDenied('切换执行授权模式'),
  streamOpen: liveStatus('open'),
  evidenceLabel: '证据',
  transitionLabel: '迁移留痕',
  preflightLabel: '小规模预检',
  preflightEmpty: '尚未执行预检；实验准备节点需要一次真实跑通的预检才能通过',
  gateLabel: '回退闸门',
  accessAsk: '每次执行前确认',
  accessTrusted: '本项目免确认',
}
