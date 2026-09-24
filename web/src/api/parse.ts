/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 单篇解析域 API 封装（WP07-T6）：论文详情 / 解析卡片 / 全文文档 / 原文片段 / 重新解析。
 *
 * 边界纪律（WP05 + WP06 integration_notes）：
 * - 偏移口径：paper_spans.char_start/char_end 为**文档级**偏移，卡片 evidence_span 与之同口径；
 *   偏移校验优先比对 quote_sha256，全文缓存缺失时 verdict 退化为 valid_by_hash →
 *   UI 必须提示「精确定位不可用，以引用文本为准」，不得把页码当物理页码。
 * - 缺失数据一律如实展示（如「未获取」「尚未解析」），禁止 0 分填充或来源编造。
 */

import { get, post, type Paginated } from './client'

import type { Breakdown, ParseStatus } from './feed'

/** 论文详情：GET /papers/{id} */
export interface PaperIdentity {
  id_type?: string
  id_value?: string
  source?: string | null
  [key: string]: unknown
}

export interface PaperDetail {
  id: number
  source: string | null
  external_id: string | null
  doi: string | null
  title: string
  abstract: string | null
  authors: Array<{ name?: string | null; affiliation?: string | null }> | null
  published_at: string | null
  venue: string | null
  venue_source: string | null
  venue_level: number | null
  citation_count: number | null
  citation_velocity: number | null
  code_url: string | null
  code_heat: number | null
  pdf_url: string | null
  rank_score: number | null
  rank_breakdown: Breakdown | null
  influence_score: number | null
  score_breakdown: Breakdown | null
  score_coverage: number | null
  is_parsed: boolean
  identities?: PaperIdentity[]
  source_summary?: Record<string, unknown> | null
  raw_included?: boolean
}

export function fetchPaperDetail(paperId: number | string, signal?: AbortSignal): Promise<PaperDetail> {
  return get<PaperDetail>(`/papers/${paperId}`, { signal })
}

/** 证据校验结论（WP05 verify_span：哈希优先于偏移） */
export interface SpanVerification {
  verdict: 'valid' | 'valid_by_hash' | 'invalid'
  hash_match?: boolean | null
  offset_match?: boolean | null
  reason?: string | null
  expected_quote_sha256?: string | null
  stored_quote_sha256?: string | null
}

/** 原文定位片段：GET /papers/{id}/spans */
export interface PaperSpan {
  id: number
  paper_id: number
  document_version: string
  section_name: string | null
  page_number: number | null
  bbox: unknown
  char_start: number
  char_end: number
  quote_text: string
  quote_sha256: string
  created_at?: string | null
  verification: SpanVerification | null
}

export interface SpansResponse extends Paginated<PaperSpan> {
  section: string | null
  document_version: string | null
  document_versions: string[]
  verification_summary: Record<string, number>
  fulltext_cache_available: boolean
  coverage_note: string | null
  spans_allowed: boolean
  evidence_scope: string | null
  note?: string
}

export interface SpanQuery {
  section?: string | null
  document_version?: string | null
  page?: number
  page_size?: number
}

export function fetchSpans(
  paperId: number | string,
  query: SpanQuery = {},
  signal?: AbortSignal,
): Promise<SpansResponse> {
  return get<SpansResponse>(`/papers/${paperId}/spans`, {
    query: {
      section: query.section ?? undefined,
      document_version: query.document_version ?? undefined,
      page: query.page ?? 1,
      page_size: query.page_size ?? 100,
    },
    signal,
  })
}

/** 拉取某解析版本的全部片段（分页循环，最多 maxPages 次，避免无界轮询） */
export async function fetchAllSpans(
  paperId: number | string,
  options: { document_version?: string | null; section?: string | null; maxPages?: number } = {},
  signal?: AbortSignal,
): Promise<{ items: PaperSpan[]; total: number; truncated: boolean; last: SpansResponse }> {
  const pageSize = 500
  const maxPages = options.maxPages ?? 6
  const items: PaperSpan[] = []
  let page = 1
  let last: SpansResponse | null = null
  while (page <= maxPages) {
    const response = await fetchSpans(
      paperId,
      {
        document_version: options.document_version ?? null,
        section: options.section ?? null,
        page,
        page_size: pageSize,
      },
      signal,
    )
    last = response
    items.push(...response.items)
    if (items.length >= response.total || response.items.length === 0) break
    page += 1
  }
  const total = last?.total ?? items.length
  return { items, total, truncated: items.length < total, last: last as SpansResponse }
}

/** 全文解析记录：GET /papers/{id}/documents */
export interface PaperDocument {
  id: number
  paper_id: number
  document_version: string
  source_type: string
  source_url: string | null
  parser: string
  parser_version: string | null
  page_count: number | null
  text_sha256: string | null
  char_count: number | null
  locatable_chars: number | null
  coverage: number | null
  parse_status: ParseStatus
  parse_error: string | null
  parsed_at: string | null
  spans_allowed: boolean
  evidence_scope: string | null
}

export interface DocumentsSummary {
  parse_status: ParseStatus | null
  coverage: number | null
  document_version: string | null
  parser: string | null
  page_count: number | null
  char_count: number | null
  locatable_chars: number | null
  spans_allowed: boolean
  evidence_scope: string | null
  coverage_note: string | null
}

export interface DocumentsResponse {
  paper_id: number
  items: PaperDocument[]
  total: number
  summary: DocumentsSummary
  latest_document_version: string | null
  coverage_note: string | null
  spans_allowed: boolean
  evidence_scope: string | null
  note?: string
}

export function fetchDocuments(
  paperId: number | string,
  signal?: AbortSignal,
): Promise<DocumentsResponse> {
  return get<DocumentsResponse>(`/papers/${paperId}/documents`, { signal })
}

/** 卡片条目里的证据片段（与 paper_spans 同偏移口径） */
export interface EvidenceSpan {
  span_id: number | null
  paper_id: number | null
  document_version: string
  section_name: string | null
  page_number: number | null
  bbox: unknown
  char_start: number
  char_end: number
  quote_text: string
  quote_sha256: string | null
  verification: SpanVerification | null
  evidence_type?: string | null
  locator?: Record<string, unknown> | null
  source_span_char_span?: [number, number] | null
  source_span_verdict?: string | null
}

/** 8 字段卡片的内容条目（不同字段用不同键名） */
export interface CardEntry {
  point?: string
  conclusion?: string
  limitation?: string
  step?: string
  description?: string
  target_problem?: string
  evidence_note?: string | null
  evidence_scope?: string | null
  evidence_span: EvidenceSpan | null
}

export interface ExperimentalSetup {
  metrics?: string[]
  datasets?: string[]
  baselines?: string[]
  [key: string]: unknown
}

export interface CardContent {
  research_problem: string
  core_method: string
  key_innovation: CardEntry[]
  technical_route: CardEntry[]
  experimental_setup: ExperimentalSetup
  main_conclusions: CardEntry[]
  limitations: CardEntry[]
  transferable: CardEntry[]
}

export interface CardVersionBrief {
  version: number
  llm_call_log_id: number | null
  created_at: string | null
}

export interface CardCallLog {
  id: number
  stage: string | null
  provider: string | null
  model: string | null
  purpose: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  cost_usd: number | null
  duration_ms: number | null
  success: boolean | null
  is_replay: boolean | null
  error: string | null
  created_at: string | null
}

export interface FieldStats {
  total: number
  located: number
  unlocated: number
}

/** 全文总结速览：一段 100–200 字（中文按字、英文术语按词），**不参与证据核验** */
export interface CardSummary {
  /** `ok` = 有 text 可显示；`failed` = 页面如实显示「生成失败」 */
  status: 'ok' | 'failed'
  text?: string
  chars?: number
  generated_at?: string | null
  model_ref?: string | null
  /** 失败原因码（内部口径，**前端不显示**，只用于排查） */
  reason?: string
}

/** 解析卡片：GET /papers/{id}/card?version= */
export interface CardResponse {
  paper_id: number
  version: number
  card: CardContent
  llm_call_log_id: number | null
  available_scope: string | null
  evidence_scope: string | null
  parse_status: ParseStatus | null
  coverage: number | null
  document_version: string | null
  located_count: number | null
  unlocated_count: number | null
  by_field: Record<string, FieldStats>
  unknown_fields: string[]
  generated_at: string | null
  card_builder_version: string | null
  fields: string[]
  evidence_meta?: Record<string, unknown> | null
  coverage_tag: string | null
  compliance_note: string | null
  llm_call_log: CardCallLog | null
  llm_call_log_traceable: boolean
  /** 全文总结速览（随卡片生成）。`null` = 本功能上线前建的老卡片，此时显示「生成失败」 */
  summary?: CardSummary | null
  versions: CardVersionBrief[]
  is_latest: boolean
  fulltext_gate_threshold: number | null
}

export function fetchCard(
  paperId: number | string,
  params: { version?: number; with_call_log?: boolean } = {},
  signal?: AbortSignal,
): Promise<CardResponse> {
  return get<CardResponse>(`/papers/${paperId}/card`, {
    query: {
      version: params.version ?? undefined,
      with_call_log: params.with_call_log === false ? false : undefined,
    },
    signal,
  })
}

export interface CardVersionsResponse extends Paginated<CardVersionBrief> {
  paper_id: number
  latest_version: number | null
  note?: string
}

export function fetchCardVersions(
  paperId: number | string,
  signal?: AbortSignal,
): Promise<CardVersionsResponse> {
  return get<CardVersionsResponse>(`/papers/${paperId}/cards`, { signal })
}

/** 重新解析（建卡/重解析长任务）：POST /papers/{id}/card（owner 面，需 X-Owner-Token） */
export interface CardJobSubmit {
  paper_id: number
  task_id: string
  status: string
  force: boolean
  poll_url: string
  card_url: string
  versions_url: string
  note?: string
}

export function rebuildCard(
  paperId: number | string,
  force = true,
  signal?: AbortSignal,
): Promise<CardJobSubmit> {
  return post<CardJobSubmit>(`/papers/${paperId}/card`, {
    body: { force },
    timeoutMs: 60_000,
    signal,
  })
}

export interface CardJobStatus {
  paper_id?: number
  task_id?: string
  status: string
  error?: string | null
  result?: Record<string, unknown> | null
  [key: string]: unknown
}

export function fetchCardJob(
  paperId: number | string,
  taskId: string,
  signal?: AbortSignal,
): Promise<CardJobStatus> {
  return get<CardJobStatus>(`/papers/${paperId}/card-jobs/${taskId}`, { signal })
}

/**
 * 8 字段定义（顺序即左侧卡片顺序，附录 C.2）
 * - `pick`：从卡片内容取展示用条目
 * - `textField`：单一文本字段（无独立引用条目）
 */
export type CardFieldKind = 'text' | 'entries' | 'setup'

export interface CardFieldDef {
  key: keyof CardContent
  label: string
  kind: CardFieldKind
  /** entries 模式下取正文文本 */
  textKey?: 'point' | 'conclusion' | 'limitation' | 'description' | 'target_problem'
  hint?: string
}

export const CARD_FIELDS: CardFieldDef[] = [
  { key: 'research_problem', label: '研究问题', kind: 'text' },
  { key: 'core_method', label: '核心方法', kind: 'text' },
  { key: 'key_innovation', label: '关键创新', kind: 'entries', textKey: 'point' },
  { key: 'technical_route', label: '技术路线', kind: 'entries', textKey: 'description' },
  { key: 'experimental_setup', label: '实验设置', kind: 'setup' },
  { key: 'main_conclusions', label: '主要结论', kind: 'entries', textKey: 'conclusion' },
  { key: 'limitations', label: '局限性', kind: 'entries', textKey: 'limitation' },
  { key: 'transferable', label: '可迁移点', kind: 'entries', textKey: 'point' },
]

/** 章节受控名（WP05 SECTION_NAMES），供右侧视图按章节过滤 */
export const SECTION_NAMES = [
  'abstract',
  'introduction',
  'related_work',
  'method',
  'experiment',
  'conclusion',
  'other',
] as const

// --------------------------------------------------------------------------- //
// 解析首屏（GET /papers/parse-home）：一次拿全「最近解析」+「聚合解析」两个区块
// --------------------------------------------------------------------------- //
/** `status`：`running` 解析中 / `failed` 失败 / `ok` 已完成 —— 前端只按它选状态图标 */
export interface ParseHomeRecentRow {
  paper_id: number
  title: string
  status: 'running' | 'failed' | 'ok'
  at: string | null
  version: number | null
}

export interface ParseHomeAggregationRow {
  aggregation_id: number
  paper_ids: number[]
  paper_count: number
  /** 服务端拼好的一行标题，如「3 篇聚合：A / B / C」 */
  title: string
  status: string
  at: string | null
}

export interface ParseHomeResponse {
  recent: ParseHomeRecentRow[]
  aggregations: ParseHomeAggregationRow[]
  limit: number
}

/** 解析首屏数据源（服务端已把时间口径与标题拼好，前端不再各自拼一遍） */
export function fetchParseHome(
  limit = 20,
  signal?: AbortSignal,
): Promise<ParseHomeResponse> {
  return get<ParseHomeResponse>('/papers/parse-home', { query: { limit }, signal })
}

