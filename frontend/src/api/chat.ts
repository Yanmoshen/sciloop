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

import { apiUrl, getOwnerToken, parseError } from '@/api/client'

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

/** 节点执行的紧凑系统行 */
export interface SystemRow {
  kind: 'system'
  label: string
  text: string
  tone?: 'idle' | 'info' | 'ok' | 'warn' | 'err'
}

export interface StreamHandlers {
  onMeta?: (meta: StreamMeta) => void
  onDelta?: (text: string) => void
  onDone?: (done: StreamDone) => void
  onTitle?: (title: StreamTitle) => void
  /** 结构化块：引导词的可点选项 / 查询结果卡片 */
  onBlocks?: (blocks: ChatBlock[]) => void
  /** 节点执行过程的一条系统行 */
  onRow?: (row: SystemRow) => void
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
      if ((payload as { row?: SystemRow }).row) {
        handlers.onRow?.((payload as { row: SystemRow }).row)
      }
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
