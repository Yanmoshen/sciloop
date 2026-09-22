/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 首页对话：`POST /chat/home`（一次性）与 `POST /chat/home/stream`（真流式 SSE）。
 * 写操作（会产生真实费用）→ 需要 OWNER_TOKEN，令牌由 `api/client.ts` 统一注入请求头。
 *
 * 流式为什么要裸用 fetch
 * -----------------------
 * `request()` 是"发完等完整响应体"的语义，会等 SSE 全部结束才返回，等于没有流式。
 * 因此这里只用 `apiUrl()` 与 `parseError()`（都由 client.ts 独占 baseURL / 错误口径），
 * 自己读 `response.body` 的 ReadableStream，边收边交给回调。
 */

import { apiUrl, get, getOwnerToken, parseError, post } from '@/api/client'

export interface HomeChatInput {
  text: string
  model_config_id: number
  model_id: string
  /** 不传 = 新建会话；传了 = 接着这个会话继续（后端带上历史轮次） */
  conversation_id?: string
  /** 不传 = 未分组；传了 = 这条新会话直接归到该项目下 */
  project_id?: number
  /**
   * 编辑重开：把这轮当成「改写第 N 条用户消息」——后端先丢弃该条及其后的所有轮次，
   * 再以 `text` 作为新的第 N 条重问。只接受指向 user 轮次的下标（指到 assistant 会 422）。
   */
  replace_from?: number
}

export interface HomeChatResult {
  title: string
  /** `model` = 模型给出；`fallback` = 标题调用失败后按输入截断 */
  title_source: string
  reply: string
  model_ref: string
  provider: string
  model_id: string
  cost_usd: number | null
  cost_unknown_reason: string | null
  /** 本次使用的会话 id（后端 JSON 落盘），前端据此接着继续 */
  conversation_id: string
  project_id?: number | null
  /** 标题降级原因（仅 title_source === 'fallback' 时有值） */
  title_note?: string | null
  usage: {
    prompt_tokens: number | null
    completion_tokens: number | null
    total_tokens: number | null
  }
}

/** 非流式入口（保留：脚本 / 无 ReadableStream 环境的兜底） */
export async function chatHome(input: HomeChatInput): Promise<HomeChatResult> {
  const response = await fetch(apiUrl('/chat/home'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...ownerHeader(),
    },
    body: JSON.stringify(input),
  })
  if (!response.ok) throw await parseError(response)
  return (await response.json()) as HomeChatResult
}

export interface StreamMeta {
  conversation_id: string
  project_id: number | null
  model_ref: string
  model_id: string
  title: string | null
  turn_count: number
}

export interface StreamDone {
  conversation_id: string
  /**
   * 完整正文（前端可用它校正自己拼出来的增量文本）。
   *
   * **可缺省**：节点执行 / 引导词 / 本地查询这三条分支是**确定性**的，
   * 收尾事件只带状态与费用，不带模型用量——它们本来就没调模型。
   */
  content?: string
  duration_ms?: number
  model_id?: string
  model_ref?: string
  provider?: string
  cost_usd?: number | null
  cost_unknown_reason?: string | null
  finish_reason?: string | null
  usage?: {
    prompt_tokens: number | null
    completion_tokens: number | null
    total_tokens: number | null
  }
  /** 本轮走的是哪条分支：node / guide / query / plain_chat（通用回答时不带） */
  routing?: string
  /** 节点分支：本节点结束状态（done / waiting_human / failed） */
  node_status?: string
  node?: string
  /** 节点分支：下一个应执行的节点，以及它**是否真的实装**（未实装要停住，不假装往下走） */
  next_node?: string | null
  next_implemented?: boolean
  /**
   * 非空 = 这一轮**没有答完，在等研究者批准**。
   *
   * 与"正常答完"必须分开：混在一起，界面会显示一个"答完了但什么都没有"的空回复。
   */
  awaiting_approval?: ApprovalCard | null
  /** 裁决流专属：本次裁决的结果与请求 id */
  decision?: ApprovalDecision
  request_id?: string
}

export interface StreamTitle {
  conversation_id: string
  title: string
  title_source: string
  title_note: string | null
}

export interface StreamError {
  code: string
  message: string
  kind?: string
  interrupted?: boolean
  generated_chars?: number
}

/** 引导词里的一个可点出口 */
export interface BlockOption {
  id: string
  label: string
  /** 点下去实际发送的文本（由后端给定，前端不改写） */
  send: string
  tone?: 'primary' | 'default' | 'quiet'
}

/** 可点选项块（模糊引导词时给出「文献调研 / 从 idea 开始 / 普通对话」） */
export interface ChoiceBlock {
  kind: 'choice'
  prompt: string
  options: BlockOption[]
}

/** 查询结果卡片 */
export interface ResultBlock {
  kind: 'result'
  title: string
  summary: string
  columns: string[]
  rows: Array<Array<string | number | null>>
  total: number
}

export type ChatBlock = ChoiceBlock | ResultBlock

/** 过程行的语气（决定颜色，不决定语义） */
export type SystemTone = 'idle' | 'info' | 'ok' | 'warn' | 'err'

/** 节点执行的紧凑系统行 */
export interface SystemRow {
  kind: 'system' | 'tool'
  /** 分类标签（节点过程行有；工具行由后端给的 text 自带工具名） */
  label?: string
  text: string
  tone?: SystemTone
}

/**
 * 研究者裁决的结果。
 *
 * 三个选择（2026-09-22 定）：
 * - `approve` 只批这一次；
 * - `approve_conversation` 在本对话里以后这类**连高危也直接执行**（比完全访问模式更宽的一档）；
 * - `deny` 拒绝。
 */
export type ApprovalDecision = 'approve' | 'approve_conversation' | 'deny'

/** 批准请求的状态：只有 `pending` 是可点的（**不再按时间自动过期**） */
export type ApprovalStatus = 'pending' | 'approved' | 'denied' | 'expired'

/**
 * 「完全访问模式」的状态（**按对话**，后端是唯一事实来源）。
 *
 * 口径只维护一份：`note` 由后端给，前端直接显示，别在界面上另写一句同义的话。
 */
export interface AccessModeState {
  conversation_id: string
  /** 开 = 普通动手操作直接执行（高危仍会先问） */
  full_access: boolean
  /** 开 = 连高危也直接执行（由批准卡上「此对话中默认允许执行」设置） */
  allow_exec: boolean
  /** 后端给的人话说明 */
  note: string
}

/** 读当前对话的授权状态（公开只读：匿名也能看，只是不能改）。 */
export function fetchAccessMode(conversationId: string): Promise<AccessModeState> {
  return get<AccessModeState>(`/chat/access-mode/${encodeURIComponent(conversationId)}`)
}

/**
 * 开 / 关当前对话的「完全访问模式」。
 *
 * **写接口只对 Owner 开放**：匿名调用后端回 403，这里会抛 `ApiError`，
 * 由视图层如实说明（而不是在前端装作切成功了）。
 */
export function setAccessMode(
  conversationId: string,
  fullAccess: boolean,
): Promise<AccessModeState> {
  return post<AccessModeState>('/chat/access-mode', {
    body: { conversation_id: conversationId, full_access: fullAccess },
  })
}

/**
 * 批准卡：模型提出了一次写盘/执行请求，**在研究者点「批准」之前它一次都不会跑**。
 *
 * 它同时是**过程行**（`kind: 'approval'`）——后端把同一份内容既发 SSE 也写进会话记录，
 * 所以刷新后前端能凭记录里的这一行把卡片按原状态重建，而不是让卡片凭空消失。
 */
export interface ApprovalCard {
  kind: 'approval'
  tone?: SystemTone
  text: string
  request_id: string
  /** 工具名（如 run_command） */
  tool: string
  /** 工具的中文名（后端给的展示名，前端不改写） */
  label: string
  /** 要执行什么：给人看的一行，研究者看的就是它 */
  preview: string
  cwd?: string | null
  status: ApprovalStatus
  created_at?: string | null
  expires_at?: string | null
  decided_at?: string | null
  /** 裁决备注（后端可能留痕） */
  note?: string | null
}

/** 过程行可能是普通行，也可能是一张批准卡 */
export type TurnRow = SystemRow | ApprovalCard

export function isApprovalCard(row: TurnRow): row is ApprovalCard {
  return row.kind === 'approval'
}

export interface StreamHandlers {
  onMeta?: (meta: StreamMeta) => void
  onDelta?: (text: string) => void
  onDone?: (done: StreamDone) => void
  onTitle?: (title: StreamTitle) => void
  /** 结构化块：引导词的可点选项 / 查询结果卡片 */
  onBlocks?: (blocks: ChatBlock[]) => void
  /** 节点执行过程的一条系统行 */
  onRow?: (row: TurnRow) => void
  /** 一张批准卡（同一 request_id 会以新状态再次到达 → 按 id 替换，不要追加） */
  onApproval?: (card: ApprovalCard) => void
  /**
   * 思考过程的增量。
   *
   * **它不是答复**：必须折叠展示（在耗时那一行下面），绝不能拼进正文里——
   * 之前正是因为它被当成正文，界面上出现了模型的自言自语。
   */
  onReasoning?: (text: string) => void
  /** 建连失败或流中途断线；**已收到的增量仍然有效**（后端已把它落盘） */
  onError?: (error: StreamError) => void
}

function ownerHeader(): Record<string, string> {
  const token = getOwnerToken()
  return token ? { 'X-Owner-Token': token } : {}
}

/**
 * 真流式对话。返回时代表整个响应已消费完（正常结束 / 后端推了 error 事件 / 主动 abort）。
 *
 * 网络层异常（非 2xx 与 `reader` 抛错）会**抛出** `ApiError`，由视图层决定怎么提示；
 * 上游模型报错则以后端 `error` 事件形式回到 `handlers.onError`（此时 HTTP 仍是 200，
 * 因为响应头早已发送）。
 */
export async function streamChatHome(
  input: HomeChatInput,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(apiUrl('/chat/home/stream'), {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      ...ownerHeader(),
    },
    body: JSON.stringify(input),
    signal,
  })
  await consumeSse(response, handlers)
}

export interface ApprovalDecisionInput {
  conversation_id: string
  request_id: string
  decision: ApprovalDecision
  /** 可选备注，随裁决一起留痕 */
  note?: string
  /** 批准后续答用哪个模型；不传 = 后端沿用会话记录的模型 */
  model_config_id?: number
  model_id?: string
}

/**
 * 研究者裁决一次写盘/执行请求。
 *
 * **前提是 Owner**：批准入口如果对匿名开放，那道门就白设了。
 * 校验失败（请求不存在 / 已批过 / 已过期）后端回 4xx，这里**抛出** `ApiError`
 * —— 由视图层按 `error.code` 如实说明，不在这里编一句含糊的"操作失败"。
 */
export async function streamApprovalDecision(
  input: ApprovalDecisionInput,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(apiUrl('/chat/approvals/decide'), {
    method: 'POST',
    headers: {
      Accept: 'text/event-stream',
      'Content-Type': 'application/json',
      ...ownerHeader(),
    },
    body: JSON.stringify(input),
    signal,
  })
  await consumeSse(response, handlers)
}

/** 读 SSE 响应体并逐帧派发（`streamChatHome` 与裁决流共用同一套解析口径）。 */
async function consumeSse(response: Response, handlers: StreamHandlers): Promise<void> {
  if (!response.ok) throw await parseError(response)
  if (!response.body) {
    throw new Error('浏览器不支持流式响应（response.body 为空）')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // SSE 以空行分帧；一次 read 可能带来多帧，也可能只有半帧
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        dispatch(frame, handlers)
        boundary = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) dispatch(buffer, handlers)
  } finally {
    reader.releaseLock()
  }
}

function dispatch(frame: string, handlers: StreamHandlers): void {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (!dataLines.length) return
  let payload: unknown
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }
  switch (event) {
    case 'meta':
      handlers.onMeta?.(payload as StreamMeta)
      break
    case 'delta':
      handlers.onDelta?.((payload as { text?: string }).text ?? '')
      break
    case 'done':
      handlers.onDone?.(payload as StreamDone)
      break
    case 'title':
      handlers.onTitle?.(payload as StreamTitle)
      break
    case 'blocks':
      handlers.onBlocks?.((payload as { blocks?: ChatBlock[] }).blocks ?? [])
      break
    case 'row':
      if ((payload as { row?: TurnRow }).row) {
        handlers.onRow?.((payload as { row: TurnRow }).row)
      }
      break
    case 'approval':
      handlers.onApproval?.(payload as ApprovalCard)
      break
    case 'reasoning':
      handlers.onReasoning?.((payload as { text?: string }).text ?? '')
      break
    case 'error':
      handlers.onError?.(payload as StreamError)
      break
    default:
      break
  }
}
