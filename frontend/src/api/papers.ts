/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 文献调研 · 文献总览接口封装（统一走 api/client.ts，视图层不直接 fetch）。
 *
 * 口径纪律：统计口径全部来自后端真实计数；取不到的项为 null，
 * 前端显示「未获取」，**不显示 0、不做估算**。
 */

import { get, post, type Paginated } from './client'

/** GET /papers/overview（文献总览统计条） */
export interface PapersOverview {
  papers_total: number
  papers_new_7d: number
  papers_new_24h: number
  documents_total: number
  documents_ok: number
  cards_total: number
  cards_papers: number
  /** 统一口径：既有解析成功的全文、又有解析卡片（2026-09-21 与产品确认） */
  papers_parsed: number
  papers_unparsed: number
  aggregations_total: number
  last_sync_at: string | null
  checked_at: string
  note: string
  parsed_definition?: string
}

export function fetchPapersOverview(signal?: AbortSignal): Promise<PapersOverview> {
  return get<PapersOverview>('/papers/overview', { signal })
}

/** GET /papers/trends（文献总览折线图数据源：逐日/逐周的 5 条序列） */
export interface PapersTrends {
  axis: string[]
  granularity: 'day' | 'week'
  window_days: number
  series: {
    total: number[]
    parsed: number[]
    unparsed: number[]
    new_papers: number[]
    new_parsed: number[]
  }
  definitions: Record<string, string>
  checked_at: string
  note: string
}

export function fetchPaperTrends(
  params: { days?: number; bucket?: 'day' | 'week' } = {},
  signal?: AbortSignal,
): Promise<PapersTrends> {
  return get<PapersTrends>('/papers/trends', {
    query: { days: params.days ?? 7, bucket: params.bucket ?? 'day' },
    signal,
  })
}

/** 本地检索条目（与后端 paper_to_item 对齐，缺失字段为 null） */
export interface PaperSearchItem {
  id: number
  source: string | null
  external_id: string | null
  doi: string | null
  title: string
  abstract: string | null
  authors: Array<{ name?: string }> | null
  published_at: string | null
  venue: string | null
  venue_source: string | null
  citation_count: number | null
  code_url: string | null
  pdf_url: string | null
  is_parsed: boolean | null
  rank_score: number | null
  influence_score: number | null
}

export interface SearchQuery {
  q?: string
  field?: string
  /** 全库筛选：来源（arxiv / semantic_scholar / openalex / github），空串＝不过滤 */
  source?: string
  /** 全库筛选：解析状态（parsed=已解析 / unparsed=未解析），空串＝不过滤；非法值服务端 422 */
  parseStatus?: string
  /** 全库排序：published=发表时间倒序（默认）/ citation=引用数倒序（null 排最后） */
  sort?: 'published' | 'citation'
  page?: number
  pageSize?: number
}

export type SearchResponse = Paginated<PaperSearchItem> & {
  query?: string | null
  field?: string | null
  source?: string
  source_filter?: string | null
  parse_status?: string | null
  sort?: string | null
  note?: string
}

/**
 * GET /papers/search（本地库检索）。
 *
 * ``q`` 命中标题/摘要，``field`` 命中 arXiv 分类；**``source`` / ``parse_status`` / ``sort`` 全库生效**
 * （2026-09-20 之前只在当前页切片，翻页即失效）。
 */
export function searchPapers(query: SearchQuery, signal?: AbortSignal): Promise<SearchResponse> {
  return get<SearchResponse>('/papers/search', {
    query: {
      q: query.q || undefined,
      field: query.field || undefined,
      source: query.source || undefined,
      parse_status: query.parseStatus || undefined,
      sort: query.sort ?? 'published',
      page: query.page ?? 1,
      page_size: query.pageSize ?? 20,
    },
    signal,
  })
}

/** POST /papers/fetch（Owner 写操作，长任务：返回 task_id） */
export interface FetchTaskAccepted {
  task_id: string
  status?: string
  job?: string
  poll_url?: string
  [key: string]: unknown
}

export function triggerFetch(
  body: { fields?: string[]; limit?: number } = {},
  signal?: AbortSignal,
): Promise<FetchTaskAccepted> {
  return post<FetchTaskAccepted>('/papers/fetch', { body, signal, timeoutMs: 30_000 })
}

/** GET /papers/fetch-jobs/{task_id}（公开只读：抓取任务进度与产出计数） */
export interface FetchJobCounts {
  discovered?: number
  created?: number
  reused?: number
  enriched?: number
  skipped_duplicate?: number
  source_records_written?: number
  citation_count_filled?: number
  citation_count_null?: number
  github_stars_filled?: number
}

export interface FetchJobStatus {
  task_id: string
  status: string
  stage?: string | null
  started_at?: string | null
  finished_at?: string | null
  progress?: FetchTaskProgress | null
  events?: FetchTaskEvent[] | null
  control?: { pause?: boolean; cancel?: boolean } | null
  report?: FetchJobReport | null
  error?: string | null
  [key: string]: unknown
}

/** 单个来源的取数统计（`report.by_source[source]`） */
export interface FetchSourceState {
  available?: boolean
  reason?: string | null
  http_status?: number | null
  requests?: number
  ok?: number
  failed?: number
  skipped?: number
  empty?: number
  breaker_openings?: number
  last_error?: string | null
  last_request_url?: string | null
  [key: string]: unknown
}

/** 任务事件流条目（后端逐条写入，非前端拼接） */
export interface FetchTaskEvent {
  at: string
  level: 'ok' | 'warn' | 'fail' | 'run' | string
  text: string
  stage?: string | null
}

/** 阶段进度（estimate=true 表示后端按阶段估算，不是实测值） */
export interface FetchTaskProgress {
  stage?: string
  label?: string
  pct?: number
  elapsed_seconds?: number
  eta_seconds?: number | null
  estimate?: boolean
}

export interface FetchJobReport {
  counts?: FetchJobCounts | null
  by_source?: Record<string, FetchSourceState> | null
  degraded?: Array<Record<string, unknown>> | null
  errors?: Array<{ stage?: string; error?: string }> | null
  source_records_written?: number
  [key: string]: unknown
}

export function fetchFetchJob(taskId: string, signal?: AbortSignal): Promise<FetchJobStatus> {
  return get<FetchJobStatus>(`/papers/fetch-jobs/${encodeURIComponent(taskId)}`, { signal })
}

/** 历史同步任务（`GET /papers/fetch-jobs`，新的在前；含事件流，无需二次请求） */
export interface FetchTaskSummary {
  task_id: string
  job?: string | null
  status: string
  stage?: string | null
  started_at?: string | null
  finished_at?: string | null
  duration_seconds?: number | null
  params?: Record<string, unknown> | null
  counts?: FetchJobCounts | null
  failed_total?: number | null
  sources?: Record<string, FetchSourceState> | null
  events?: FetchTaskEvent[] | null
}

export function listFetchJobs(
  limit = 30,
  signal?: AbortSignal,
): Promise<{ tasks: FetchTaskSummary[]; total: number }> {
  return get<{ tasks: FetchTaskSummary[]; total: number }>('/papers/fetch-jobs', {
    query: { limit },
    signal,
  })
}

/** Owner 面：暂停 / 恢复 / 终止抓取任务 */
export function pauseFetchJob(taskId: string): Promise<FetchJobStatus> {
  return post<FetchJobStatus>(`/papers/fetch-jobs/${encodeURIComponent(taskId)}/pause`, {})
}

export function resumeFetchJob(taskId: string): Promise<FetchJobStatus> {
  return post<FetchJobStatus>(`/papers/fetch-jobs/${encodeURIComponent(taskId)}/resume`, {})
}

export function cancelFetchJob(taskId: string): Promise<FetchJobStatus> {
  return post<FetchJobStatus>(`/papers/fetch-jobs/${encodeURIComponent(taskId)}/cancel`, {})
}
