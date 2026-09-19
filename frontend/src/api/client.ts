/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 统一 API 客户端（WP01 公共设施，所有视图必须经由此文件发请求）。
 *
 * 契约（contracts.api_contract）：
 * - baseURL 读 `VITE_API_BASE`（默认 `/api/v1`）
 * - 写操作自动带 `X-Owner-Token`；令牌只存 sessionStorage，禁止进入前端包或数据库
 * - 错误统一解析为 `{code, message, detail}`
 *
 * **唯一职责边界（P1-1 合并后）**：本文件是前端**唯一**的传输层实现，独占
 * baseURL 解析、`fetch`、超时中断、`X-Owner-Token` 注入、错误规范化（`ApiError`）、
 * JSON 解析与降级语义。`src/api/models.ts` 与其它领域 API 文件**只允许**调用本文件的
 * `get/post/put/patch/del/request` 或 `apiUrl/streamUrl`，不得再自建 transport、不得
 * 自行读取 `VITE_API_BASE`、不得自行读写令牌。
 */

/** 后端统一错误体 */
export interface ApiErrorBody {
  code: string
  message: string
  detail?: unknown
}

/** 分页响应（contracts.api_contract.pagination） */
export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export const API_BASE = (import.meta.env?.VITE_API_BASE as string | undefined) ?? '/api/v1'

/** Owner 令牌存储键：全前端唯一定义处（models.ts 已改为再导出本模块的读写函数） */
export const OWNER_TOKEN_KEY = 'sciloop.owner_token'

const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** 统一错误类型：视图层只需判断 `err.code` / `err.status` */
export class ApiError extends Error {
  readonly code: string
  readonly status: number
  readonly detail?: unknown

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }

  /** public_demo 面发起写操作被拒 */
  get isForbidden(): boolean {
    return this.status === 401 || this.status === 403
  }

  get isDbUnavailable(): boolean {
    return this.code === 'db_unavailable' || this.status === 503
  }
}

export function getOwnerToken(): string {
  try {
    return sessionStorage.getItem(OWNER_TOKEN_KEY) ?? ''
  } catch {
    // 隐私模式 / SSR：退化为内存态
    return memoryToken
  }
}

export function setOwnerToken(token: string): void {
  const value = token.trim()
  memoryToken = value
  try {
    if (value) sessionStorage.setItem(OWNER_TOKEN_KEY, value)
    else sessionStorage.removeItem(OWNER_TOKEN_KEY)
  } catch {
    /* 忽略存储不可用 */
  }
}

export function clearOwnerToken(): void {
  setOwnerToken('')
}

let memoryToken = ''

export interface RequestOptions {
  /** 查询参数（undefined / null 自动丢弃） */
  query?: Record<string, string | number | boolean | null | undefined>
  /** JSON 请求体 */
  body?: unknown
  /** 额外请求头 */
  headers?: Record<string, string>
  /** 超时毫秒（默认 30s，长任务端点请显式放大） */
  timeoutMs?: number
  signal?: AbortSignal
}

/**
 * 拼接 baseURL + 路径 + 查询串（本文件唯一负责 baseURL 的地方）。
 * 非 `fetch` 场景（下载、`EventSource`、`<a href>` 等）请改用导出的 `apiUrl`。
 */
function buildUrl(path: string, query?: RequestOptions['query']): string {
  const base = API_BASE.replace(/\/$/, '')
  const url = path.startsWith('http') ? path : `${base}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const search = new URLSearchParams()
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.append(key, String(value))
  })
  const qs = search.toString()
  return qs ? `${url}${url.includes('?') ? '&' : '?'}${qs}` : url
}

/**
 * 只构建 URL、不发请求：供下载链接、SSE、`<a href>` 等无法走 `request()` 的场景使用，
 * 避免调用方自行读取 `VITE_API_BASE` 而出现第二套 baseURL 口径。
 */
export function apiUrl(path: string, query?: RequestOptions['query']): string {
  return buildUrl(path, query)
}

/**
 * 把非 2xx 响应规范化为 `ApiError`。
 *
 * 兼容两种真实形态（两种都要稳定给出 `code` 与可读 `message`）：
 * 1. 后端统一错误体 `{code, message, detail}`（实测 `/api/v1/models/configs` 403、
 *    `/api/v1/costs/summary?project_id=abc` 422 均为此形态）
 * 2. 防御性兜底：顶层 `code` 缺失但 `detail` 是带 `code`/`message` 的对象
 */
async function parseError(response: Response): Promise<ApiError> {
  const fallback = `请求失败（HTTP ${response.status}）`
  let text = ''
  try {
    text = await response.text()
  } catch {
    return new ApiError(response.status, `http_${response.status}`, fallback)
  }
  if (!text) return new ApiError(response.status, `http_${response.status}`, fallback)
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>
    if (parsed && typeof parsed === 'object') {
      const nested =
        parsed.detail && typeof parsed.detail === 'object' && !Array.isArray(parsed.detail)
          ? (parsed.detail as Record<string, unknown>)
          : null
      const code = parsed.code ?? nested?.code
      const message = parsed.message ?? nested?.message
      if (code !== undefined || message !== undefined) {
        return new ApiError(
          response.status,
          String(code ?? `http_${response.status}`),
          String(message ?? fallback),
          // 保持历史语义：`detail` 始终是响应体的 `detail` 字段（可能为 null），不替换为整个响应体
          parsed.detail,
        )
      }
    }
    return new ApiError(response.status, `http_${response.status}`, fallback, parsed)
  } catch {
    return new ApiError(response.status, `http_${response.status}`, text.slice(0, 500))
  }
}

/** 底层请求：自动注入 Owner 令牌、解析统一错误体、超时中断 */
export async function request<T = unknown>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const upper = method.toUpperCase()
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...options.headers,
  }
  // FormData（文件上传）必须让浏览器自己带 multipart boundary：不能手动设 Content-Type，
  // 也不能 JSON.stringify，否则后端拿到的 multipart 直接解析失败。
  const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData
  if (options.body !== undefined && !isFormData) headers['Content-Type'] = 'application/json'
  if (WRITE_METHODS.has(upper)) {
    const token = getOwnerToken()
    if (token) headers['X-Owner-Token'] = token
  }

  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), options.timeoutMs ?? 30_000)
  if (options.signal) {
    options.signal.addEventListener('abort', () => controller.abort(), { once: true })
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: upper,
      headers,
      body: options.body === undefined ? undefined : isFormData ? (options.body as FormData) : JSON.stringify(options.body),
      signal: controller.signal,
      credentials: 'same-origin',
    })
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError(0, 'timeout', `请求超时（${options.timeoutMs ?? 30_000}ms）`)
    }
    throw new ApiError(0, 'network_error', `网络不可达：${(error as Error).message}`)
  } finally {
    window.clearTimeout(timeout)
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T

  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    return (await response.text()) as unknown as T
  }
  return (await response.json()) as T
}

export const get = <T = unknown>(path: string, options?: RequestOptions) =>
  request<T>('GET', path, options)

export const post = <T = unknown>(path: string, options?: RequestOptions) =>
  request<T>('POST', path, options)

export const patch = <T = unknown>(path: string, options?: RequestOptions) =>
  request<T>('PATCH', path, options)

export const put = <T = unknown>(path: string, options?: RequestOptions) =>
  request<T>('PUT', path, options)

export const del = <T = unknown>(path: string, options?: RequestOptions) =>
  request<T>('DELETE', path, options)

/** SSE 端点 URL（禁止 WebSocket；由 WP09 提供事件流） */
export function streamUrl(projectId: number | string): string {
  return buildUrl(`/stream/${projectId}`)
}

export default { request, get, post, patch, put, del, streamUrl, apiUrl, API_BASE }
