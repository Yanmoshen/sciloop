/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文库 API 封装（WP07-T6）：统一走 `api/client.ts` 的 request()，视图层不直接 fetch。
 *
 * 口径纪律（contracts.ranking_and_influence / 计划书 §2.7）：
 * - 分项缺失一律 null，前端显示「未获取」，**禁止显示 0 分、禁止编造来源**；
 * - influence_score 是辅助展示分，**不用于推荐视图排序**；
 * - llm_novelty 仅作辅助标签，stable=false 时只展示区间。
 */

import { get, type Paginated } from './client'

/** 三视图（口径分离，contracts.ranking_and_influence.views） */
export type FeedViewName = 'recommended' | 'influence' | 'latest'

/** parse_status 受控值（contracts.enums.parse_status） */
export type ParseStatus = 'ok' | 'partial' | 'unavailable' | 'failed'

/** 数据来源标识（徽标用） */
export type SourceName = 'arxiv' | 'semantic_scholar' | 'openalex' | 'github' | 'llm'

export const SOURCE_LABELS: Record<string, string> = {
  arxiv: 'arXiv',
  semantic_scholar: 'Semantic Scholar',
  openalex: 'OpenAlex',
  github: 'GitHub',
  llm: 'LLM',
}

/** 单条分项：value / source / confidence 三项齐全（缺失时 value=null，source 如实标注原因） */
export interface ScoreDimension {
  value: number | null
  source: string | null
  confidence: number | null
}

export type Breakdown = Record<string, ScoreDimension>

/** 排序四维（顺序即展示顺序） */
export const RANK_DIMENSIONS: string[] = [
  'relevance',
  'recency',
  'citation_trend',
  'evidence_completeness',
]

/** 影响力三项（辅助分，不参与推荐排序） */
export const INFLUENCE_DIMENSIONS: string[] = ['venue', 'citation_velocity', 'code_heat']

export const DIMENSION_LABELS: Record<string, string> = {
  relevance: '检索相关性',
  recency: '时效性',
  citation_trend: '引用趋势',
  evidence_completeness: '证据完整度',
  venue: '会议/期刊等级',
  citation_velocity: '引用速度',
  code_heat: '代码热度',
}

/** llm_novelty 辅助标签（带不确定性区间；**不进入任何分数**） */
export interface NoveltyTag {
  value: number | null
  low: number | null
  high: number | null
  stable: boolean | null
  note: string | null
  display: 'point' | 'range' | 'unavailable'
  model: string | null
  prompt_version: string | null
}

/** 全文覆盖信息（WP05 的 paper_documents 或 papers.is_parsed 兜底，source 如实标注） */
export interface FulltextBlock {
  parse_status: ParseStatus | null
  coverage: number | null
  document_version: string | null
  source: string | null
}

export interface FeedItemAuthor {
  name?: string | null
  affiliation?: string | null
  institution?: string | null
  institution_id?: number | null
}

export interface FeedItem {
  id: number
  source: string | null
  external_id: string | null
  doi: string | null
  title: string
  abstract: string | null
  authors: FeedItemAuthor[] | null
  published_at: string | null
  venue: string | null
  venue_source: string | null
  venue_level: number | null
  citation_count: number | null
  citation_velocity: number | null
  code_url: string | null
  rank_score: number | null
  influence_score: number | null
  score_coverage: number | null
  rank_breakdown: Breakdown
  score_breakdown: Breakdown
  influence_coverage: number | null
  llm_novelty_tag: NoveltyTag
  fulltext: FulltextBlock
  is_parsed: boolean
  display_metadata?: {
    institution_score?: number | null
    affiliations?: string[]
    note?: string
  }
}

export interface FeedRankingMeta {
  rank_weights: Record<string, number>
  influence_weights: Record<string, number>
  query_provided: boolean
  notes: string[]
}

export interface FeedFilters {
  field: string | null
  from: string | null
  to: string | null
  venue_only: boolean
}

export interface FeedResponse extends Paginated<FeedItem> {
  data_source: 'live' | 'snapshot' | 'replay'
  data_source_note: string | null
  view: FeedViewName
  sort_by: string
  reranked_by_query: boolean
  filters: FeedFilters
  ranking: FeedRankingMeta
}

export interface FeedQuery {
  view: FeedViewName
  /** 领域，如 cs.AI */
  field?: string | null
  /** 起始日期 YYYY-MM-DD */
  from?: string | null
  /** 结束日期 YYYY-MM-DD */
  to?: string | null
  /** 仅看已识别到顶会/期刊等级的论文 */
  venue_only?: boolean
  /** 可选检索词：不传则 relevance 不参与排序 */
  q?: string | null
  /** demo=读演示快照，replay=回放；不传=live */
  snapshot?: string | null
  page?: number
  page_size?: number
}

/** 视图元信息（供 Tab / 排序依据说明条使用） */
export const FEED_VIEW_META: Record<FeedViewName, { label: string; sortLabel: string; hint: string }> =
  {
    recommended: {
      label: '推荐视图',
      sortLabel: 'rank_score DESC（排序四维加权）',
      hint: '默认口径：相关性 0.40 + 时效性 0.25 + 引用趋势 0.20 + 证据完整度 0.15，缺失项按剩余权重归一',
    },
    influence: {
      label: '影响力视图',
      sortLabel: 'influence_score DESC（辅助分）',
      hint: '辅助展示分：venue 0.40 + citation_velocity 0.40 + code_heat 0.20；不用于默认（推荐）排序',
    },
    latest: {
      label: '最新视图',
      sortLabel: 'published_at DESC（不按任何分数排序）',
      hint: '按发布时间倒序；新论文天然低引用，分数仅作标签，不参与排序',
    },
  }

/** 领域选项（任务书指定四个） */
export const FIELD_OPTIONS = ['cs.AI', 'cs.CL', 'cs.CV', 'cs.LG']

/** 三视图论文库：GET /papers/feed */
export function fetchFeed(query: FeedQuery, signal?: AbortSignal): Promise<FeedResponse> {
  return get<FeedResponse>('/papers/feed', {
    query: {
      view: query.view,
      field: query.field ?? undefined,
      from: query.from ?? undefined,
      to: query.to ?? undefined,
      venue_only: query.venue_only ? true : undefined,
      q: query.q ?? undefined,
      snapshot: query.snapshot ?? undefined,
      page: query.page ?? 1,
      page_size: query.page_size ?? 10,
    },
    signal,
  })
}

/** 单篇论文取数留痕：GET /papers/{id}/sources（用于来源徽标与来源明细，禁止臆测来源） */
export interface PaperSourceRecord {
  id: number
  paper_id: number
  source: string
  source_label: string | null
  field_name: string
  raw_value: string | null
  confidence: number | null
  request_url: string
  http_status: number | null
  fetched_at: string | null
}

export interface PaperSourcesResponse extends Paginated<PaperSourceRecord> {
  paper_id: number
  summary?: Record<string, unknown>
  confidence_by_source?: Record<string, number | null>
}

export function fetchPaperSources(
  paperId: number | string,
  params: { page?: number; page_size?: number } = {},
  signal?: AbortSignal,
): Promise<PaperSourcesResponse> {
  return get<PaperSourcesResponse>(`/papers/${paperId}/sources`, {
    query: { page: params.page ?? 1, page_size: params.page_size ?? 100 },
    signal,
  })
}

/** 数据源健康：GET /sources/health */
export interface SourceHealthEntry {
  source: string
  label: string | null
  status: 'ok' | 'degraded' | string
  ok: boolean
  confidence: number | null
  configured: boolean
  credentials_required: boolean
  total_records: number | null
  success_records: number | null
  empty_records: number | null
  distinct_papers: number | null
  last_fetched_at: string | null
  last_success_at: string | null
  last_http_status: number | null
  degraded_reason: string | null
  config?: Record<string, unknown> | null
}

export interface SourceHealthResponse {
  status: string
  degraded_sources: string[]
  sources: Record<string, SourceHealthEntry>
  checked_at: string | null
  probe_enabled?: boolean
  notes?: string[]
}

export function fetchSourceHealth(
  params: { probe?: boolean } = {},
  signal?: AbortSignal,
): Promise<SourceHealthResponse> {
  return get<SourceHealthResponse>('/sources/health', {
    query: params.probe ? { probe: true } : undefined,
    signal,
  })
}

/**
 * llm_novelty 辅助标签的展示口径（纯函数，便于复算与验证）：
 * - `stable === false`（或 `display === 'range'`）→ **只能展示区间**并标注 unstable；
 * - 未生成（`display === 'unavailable'` / 空值）→ 显示「未获取」，**不得显示 0 分**；
 * - 其余情况展示单点分 value。
 * 该标签**不进入任何排序或评分**。
 */
export interface NoveltyDisplay {
  text: string
  rangeOnly: boolean
  unavailable: boolean
  low: number | null
  high: number | null
  note: string | null
  model: string | null
}

export function describeNovelty(tag: NoveltyTag | null | undefined): NoveltyDisplay {
  const empty: NoveltyDisplay = {
    text: '未获取（LLM 辅助标签未生成；该标签不进入任何分数）',
    rangeOnly: false,
    unavailable: true,
    low: null,
    high: null,
    note: null,
    model: null,
  }
  if (!tag || tag.display === 'unavailable') return empty
  const value =
    tag.value === null || tag.value === undefined || Number.isNaN(tag.value)
      ? null
      : Math.round(tag.value * 100) / 100
  const low = tag.low === null || tag.low === undefined ? null : Math.round(tag.low * 100) / 100
  const high = tag.high === null || tag.high === undefined ? null : Math.round(tag.high * 100) / 100
  if (tag.stable === false || tag.display === 'range') {
    return {
      text: `区间 ${low ?? '未获取'} ~ ${high ?? '未获取'}（unstable：两次调用差值超过容差 15，仅展示区间不展示单点分）`,
      rangeOnly: true,
      unavailable: false,
      low,
      high,
      note: tag.note,
      model: tag.model,
    }
  }
  if (value === null && low === null && high === null) return empty
  return {
    text: value === null ? `未获取单点分（区间 ${low ?? '未获取'} ~ ${high ?? '未获取'}）` : String(value),
    rangeOnly: false,
    unavailable: false,
    low,
    high,
    note: tag.note,
    model: tag.model,
  }
}
