/**
 * agent.v2 WebSocket 客户端。
 *
 * 职责边界（对应验收书 §6）：
 *
 * - **自动重连**：指数退避，连接恢复后按上层给出的最后游标重新订阅；
 * - **请求幂等**：变更类请求自动生成幂等键；断线时未得到响应的请求会**带着同一个键**
 *   重发，服务端据此返回首次结果而不是产生第二次副作用；
 * - **只做传输**：不解析业务语义、不改状态——通知交给 reducer，状态交给 store。
 *
 * 为便于测试，socket 通过工厂注入；默认工厂直接使用浏览器原生 `WebSocket`。
 */

import { PROTOCOL_VERSION, isMutating, type NotificationFrame, type ResponseFrame } from '../agent-v2/protocol'

/** 客户端只依赖这组最小 socket 能力（浏览器 WebSocket / 测试替身都满足）。 */
export interface AgentSocketLike {
  send(data: string): void
  close(code?: number, reason?: string): void
  onopen: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
  onclose: ((event: { code?: number; reason?: string }) => void) | null
  onerror: ((event: unknown) => void) | null
}

export type SocketFactory = (url: string) => AgentSocketLike

export type ClientStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed'

export interface PendingRequest {
  id: string
  method: string
  params: Record<string, unknown>
  idempotencyKey: string | null
  resolve: (response: ResponseFrame) => void
  reject: (error: Error) => void
  timer: ReturnType<typeof setTimeout> | null
  attempts: number
}

export interface ClientEvent {
  type: 'status' | 'notification' | 'response' | 'error'
  status?: ClientStatus
  attempts?: number
  message?: string
  notification?: NotificationFrame
  response?: ResponseFrame
}

export interface AgentV2ClientOptions {
  url: string
  token?: string
  socketFactory?: SocketFactory
  /** 请求超时（毫秒）。对 `agent/wait` 这类长命令请在单次调用里覆盖。 */
  requestTimeoutMs?: number
  maxBackoffMs?: number
  /** 退避基数（毫秒），测试里调小。 */
  baseBackoffMs?: number
  /** 客户端 id：参与幂等键命名，避免多标签页互相重放。 */
  clientId?: string
  /** 连接建立后回调（用于重新订阅并补事件）。 */
  onOpen?: (client: AgentV2Client, reconnected: boolean) => void
  /** 连接断开回调。 */
  onClose?: (client: AgentV2Client, code: number, reason: string) => void
}

function defaultSocketFactory(url: string): AgentSocketLike {
  return new WebSocket(url) as unknown as AgentSocketLike
}

function randomId(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 10)
  return `${prefix}-${Date.now().toString(36)}-${rand}`
}

export class AgentV2Client {
  readonly url: string
  readonly clientId: string
  status: ClientStatus = 'idle'
  attempts = 0

  private readonly options: AgentV2ClientOptions
  private readonly factory: SocketFactory
  private socket: AgentSocketLike | null = null
  private readonly pending = new Map<string, PendingRequest>()
  private readonly handlers = new Set<(event: ClientEvent) => void>()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private manualClose = false
  private seq = 0

  constructor(options: AgentV2ClientOptions) {
    this.options = options
    this.factory = options.socketFactory ?? defaultSocketFactory
    this.clientId = options.clientId ?? randomId('client')
    this.url = this.buildUrl(options.url, options.token)
  }

  private buildUrl(url: string, token?: string): string {
    if (!token) return url
    const separator = url.includes('?') ? '&' : '?'
    return `${url}${separator}owner_token=${encodeURIComponent(token)}`
  }

  // ------------------------------------------------------------------ 生命周期
  connect(): void {
    this.manualClose = false
    if (this.socket) return
    this.setStatus(this.attempts > 0 ? 'reconnecting' : 'connecting')
    const socket = this.factory(this.url)
    this.socket = socket
    socket.onopen = () => this.handleOpen()
    socket.onmessage = (event) => this.handleMessage(event?.data)
    socket.onclose = (event) => this.handleClose(event?.code ?? 1006, event?.reason ?? '')
    socket.onerror = () => this.emit({ type: 'error', message: 'WebSocket 连接错误' })
  }

  close(code = 1000, reason = 'client close'): void {
    this.manualClose = true
    this.clearReconnect()
    this.socket?.close(code, reason)
    this.socket = null
    this.setStatus('closed')
  }

  isOpen(): boolean {
    return this.status === 'connected'
  }

  private handleOpen(): void {
    const reconnected = this.attempts > 0
    this.attempts = 0
    this.setStatus('connected')
    this.options.onOpen?.(this, reconnected)
    this.resendPending()
  }

  private handleClose(code: number, reason: string): void {
    this.socket = null
    this.options.onClose?.(this, code, reason)
    if (this.manualClose) {
      this.setStatus('closed')
      return
    }
    this.attempts += 1
    this.setStatus('reconnecting')
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    const base = this.options.baseBackoffMs ?? 400
    const max = this.options.maxBackoffMs ?? 8000
    const delay = Math.min(max, base * 2 ** Math.min(this.attempts - 1, 5))
    this.clearReconnect()
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private setStatus(status: ClientStatus): void {
    this.status = status
    this.emit({ type: 'status', status, attempts: this.attempts })
  }

  // ------------------------------------------------------------------ 订阅
  subscribe(handler: (event: ClientEvent) => void): () => void {
    this.handlers.add(handler)
    return () => this.handlers.delete(handler)
  }

  onNotification(handler: (notification: NotificationFrame) => void): () => void {
    return this.subscribe((event) => {
      if (event.type === 'notification' && event.notification) handler(event.notification)
    })
  }

  private emit(event: ClientEvent): void {
    for (const handler of [...this.handlers]) handler(event)
  }

  // ------------------------------------------------------------------ 发送
  private handleMessage(raw: unknown): void {
    if (typeof raw !== 'string') return
    let frame: ResponseFrame | NotificationFrame
    try {
      frame = JSON.parse(raw) as ResponseFrame | NotificationFrame
    } catch {
      this.emit({ type: 'error', message: '收到无法解析的帧' })
      return
    }
    if (frame.kind === 'response') {
      const pending = this.pending.get(frame.id)
      if (pending) {
        this.pending.delete(frame.id)
        if (pending.timer !== null) clearTimeout(pending.timer)
        pending.resolve(frame)
      }
      this.emit({ type: 'response', response: frame })
      return
    }
    if (frame.kind === 'notification') {
      this.emit({ type: 'notification', notification: frame })
    }
  }

  /**
   * 发送一条请求。
   *
   * 变更类方法自动带幂等键；调用方也可显式传入（断线重试必须复用同一个键）。
   */
  request(
    method: string,
    params: Record<string, unknown> = {},
    options: { idempotencyKey?: string; timeoutMs?: number } = {},
  ): Promise<ResponseFrame> {
    const id = randomId('req')
    const key =
      options.idempotencyKey ?? (isMutating(method) ? `${this.clientId}:${id}` : null)
    const timeoutMs = options.timeoutMs ?? this.options.requestTimeoutMs ?? 30000
    return new Promise((resolve, reject) => {
      const entry: PendingRequest = {
        id,
        method,
        params,
        idempotencyKey: key,
        resolve,
        reject,
        timer: null,
        attempts: 0,
      }
      entry.timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`${method} 超时（${timeoutMs}ms）`))
      }, timeoutMs)
      this.pending.set(id, entry)
      this.flush(entry)
    })
  }

  private flush(entry: PendingRequest): void {
    if (!this.socket || this.status !== 'connected') {
      // 未连上：等 onOpen 时统一重发
      return
    }
    entry.attempts += 1
    const frame = {
      contract: PROTOCOL_VERSION,
      kind: 'request',
      id: entry.id,
      method: entry.method,
      params: entry.params,
      idempotency_key: entry.idempotencyKey,
    }
    this.seq += 1
    this.socket.send(JSON.stringify(frame))
  }

  private resendPending(): void {
    for (const entry of this.pending.values()) this.flush(entry)
  }

  pendingCount(): number {
    return this.pending.size
  }

  pendingMethods(): string[] {
    return [...this.pending.values()].map((entry) => entry.method)
  }
}

export function createAgentV2Client(options: AgentV2ClientOptions): AgentV2Client {
  return new AgentV2Client(options)
}

/** 把响应解包成结果对象；失败时抛带 code 的错误（供 store 统一处理）。 */
export class AgentV2RequestError extends Error {
  readonly code: string
  readonly data: Record<string, unknown>

  constructor(message: string, code: string, data: Record<string, unknown> = {}) {
    super(message)
    this.name = 'AgentV2RequestError'
    this.code = code
    this.data = data
  }
}

export function unwrap<T extends Record<string, unknown>>(frame: ResponseFrame): T {
  if (frame.ok) return (frame.result ?? {}) as T
  const error = frame.error
  throw new AgentV2RequestError(
    error?.message ?? '请求失败',
    error?.code ?? 'unknown_error',
    error?.data ?? {},
  )
}

/** 服务端明确说「游标过期」时需要走全量快照重建。 */
export function isStaleCursor(error: unknown): boolean {
  return error instanceof AgentV2RequestError && error.code === 'cursor_expired'
}

export function isDuplicate(error: unknown): boolean {
  return error instanceof AgentV2RequestError && error.code === 'idempotency_conflict'
}
