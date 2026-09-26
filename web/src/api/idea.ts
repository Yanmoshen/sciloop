/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 构思域数据层（WP08-T7）：聚合 / 空白 / idea / 可行性 / 任务书。
 *
 * 分层约定（contracts.code_style.frontend）：组件内**禁止**直接 fetch，
 * 一切请求经 `src/api/*.ts`；写操作由 `client.ts` 自动带 `X-Owner-Token`。
 *
 * 覆盖端点（附录 B.2 / B.3）：
 *   POST   /aggregations                     创建聚合（矩阵 + 演进 + 空白）
 *   GET    /aggregations, /aggregations/{id}, /aggregations/{id}/gaps
 *   POST   /ideas/generate                   生成 idea（服务端丢弃无证据条目）
 *   POST   /ideas, GET /ideas, GET /ideas/{id}
 *   GET    /ideas/{id}/evidences, POST /ideas/{id}/evidences
 *   POST   /ideas/{id}/select
 *   POST   /feasibility, GET /feasibility/{id}, GET /feasibility?idea_id=
 *   POST   /taskbooks, GET /taskbooks/{id}, PATCH /taskbooks/{id}, POST /taskbooks/{id}/lock
 *
 * 诚实展示纪律：证据一律经 `src/api/claims.ts` 的 `getEvidenceDetail` 展开
 * （复用 WP13 冻结契约），本文件只做参数拼装，不复制证据解析逻辑。
 */
import { del, get, patch, post, type Paginated } from './client'

import type { EvidenceDetail, EvidenceType } from './claims'

export type { EvidenceDetail, EvidenceType }

/** ``evidences`` 候选字段白名单（与 WP13 CANDIDATE_FIELDS 对齐，前端不发明字段） */
export interface EvidenceCandidate {
  evidence_type: EvidenceType | string
  paper_id?: number | null
  paper_span_id?: number | null
  card_field?: string | null
  experiment_run_id?: number | null
  experiment_passport_id?: number | null
  decision_log_id?: number | null
  metric_name?: string | null
  metric_value?: number | null
  quote_text?: string | null
  weight?: number | null
}

/** 对比矩阵的单元格证据块（WP08 自产结构，可展开成人话与跳转） */
export interface CellEvidence {
  kind: 'card_field' | 'paper_span' | string
  label: string
  paper_id: number
  card_field?: string | null
  scope?: string | null
  candidate: EvidenceCandidate
  paper_span_id?: number | null
  quote_text?: string | null
  section_name?: string | null
  locator_kind?: string | null
  match_coverage?: number | null
  document_version?: string | null
  char_start?: number | null
  char_end?: number | null
  jump_url?: string | null
}

export interface MatrixCell {
  text: string | null
  items: string[]
  missing: boolean
  card_field: string
  item_count?: number
  evidence: CellEvidence[]
  note?: string | null
}

export interface MatrixDimension {
  key: string
  label: string
  card_field: string
  sub_key?: string | null
  kind: 'text' | 'list' | string
}

export interface MatrixRow {
  paper_id: number
  title: string | null
  venue: string | null
  published_at: string | null
  citation_count: number | null
  card_version: number | null
  scope: string | null
  coverage: number | null
  coverage_tag: string | null
  values: Record<string, MatrixCell>
}

export interface ComparisonMatrix {
  dimensions: MatrixDimension[]
  rows: MatrixRow[]
  row_count: number
  column_count: number
  generated_by?: string
  missing?: Array<{ paper_id: number; reason: string }>
  notes?: string[]
  compliance_note?: string
  /** 跨篇综述（矩阵之外的另一半产物）：随聚合一起生成，失败时 status='failed' */
  synthesis?: AggregationSynthesis | null
}

/** 跨篇综述：一段 200–400 字（中文按字、英文术语按词），**只用各篇卡片与速览** */
export interface AggregationSynthesis {
  status: 'ok' | 'failed'
  text?: string
  chars?: number
  paper_count?: number
  model_ref?: string | null
  generated_at?: string | null
  /** 失败原因码（内部口径，**前端不显示**） */
  reason?: string
}

export interface EvolutionRelation {
  from_paper_id: number
  to_paper_id: number
  change: string
  evidence: CellEvidence[]
  mechanism?: string | null
  mechanism_basis?: string | null
  matched_terms?: string[]
  match_hits?: number
  match_coverage?: number
  match_scope?: 'span' | 'document' | 'card' | string
  confidence?: 'high' | 'medium' | 'low' | string
  span_located?: boolean
  span?: Record<string, unknown> | null
  from_title?: string | null
  to_title?: string | null
}

export interface EvolutionPayload {
  relations: EvolutionRelation[]
  relation_count?: number
  timeline?: Array<{
    paper_id: number
    title: string | null
    venue: string | null
    published_at: string | null
    core_method: string | null
    scope: string | null
  }>
  generated_by?: string
  thresholds?: Record<string, number>
  notes?: string[]
}

export interface UnsolvedEvidence {
  paper_id: number
  paper_span_id: number
  document_version?: string | null
  section_name?: string | null
  page_number?: number | null
  char_start?: number | null
  char_end?: number | null
  quote_text?: string | null
  quote_sha256?: string | null
  locator_kind?: string | null
  match_coverage?: number | null
  source?: string | null
  kind?: string
}

export interface GapRaisedBy {
  paper_id: number
  title?: string | null
  venue?: string | null
  source?: string | null
  card_field?: string | null
  span_id?: number | null
  section_name?: string | null
  page_number?: number | null
  quote_text?: string | null
  locator_kind?: string | null
  note?: string | null
  jump_url?: string | null
  span_ids?: number[]
  coverage_tag?: string | null
}

export interface Gap {
  id: number
  aggregation_id: number
  gap_text: string
  raised_by_paper_ids: number[]
  unsolved_evidence: UnsolvedEvidence[]
  novelty_hint: string | null
  raised_by?: GapRaisedBy[]
  span_count?: number
  evidence_kinds?: string[]
  novelty_hint_source?: string | null
  unsolved_scope_note?: string | null
  sources?: string[]
  merged_count?: number
  created_at?: string | null
}

export interface Aggregation {
  id: number
  aggregation_id: number
  project_id: number | null
  paper_ids: number[]
  paper_count: number
  comparison_matrix?: ComparisonMatrix
  method_evolution?: EvolutionPayload
  gaps?: Gap[]
  gap_count?: number
  gap_payload_meta?: Record<string, unknown>
  created_at?: string | null
  evidence_hint?: string
  compliance_note?: string
}

/** 四个创新方向（与后端 `services/ideation/idea_generator.py` 的 MECHANISMS 同口径） */
export type IdeaMechanism = 'refinement' | 'transfer' | 'combination' | 'paradigm'

/** 界面上四个方向的**固定展示顺序**（方法迭代 → 场景迁移 → 技术融合 → 范式拓展） */
export const IDEA_DIRECTIONS = ['refinement', 'transfer', 'combination', 'paradigm'] as const
export type IdeaMode = 'auto' | 'llm' | 'template'

export interface Idea {
  id: number
  idea_id: number
  project_id: number | null
  aggregation_id: number | null
  origin: 'ai_generated' | 'user_input' | string
  title: string
  content: string
  mechanism: IdeaMechanism | string | null
  novelty_note: string | null
  is_selected: boolean
  created_at: string | null
  evidences: EvidenceDetail[]
  evidence_count: number
  evidence_ids: number[]
  has_evidence: boolean
  evidence_errors?: string[]
  gate?: { rule: string; ok: boolean; evidence_scope: string | null }
}

export interface IdeaGenerationResult {
  aggregation_id: number
  project_id: number | null
  requested_count: number
  generated_count: number
  created_count: number
  discarded_count: number
  generation_mode: IdeaMode | string
  llm: Record<string, unknown> | null
  llm_error: Record<string, unknown> | null
  evidence_policy: string
  discarded: Array<{ idea_id: number; reason: string; message: string }>
  evidence_audit: Array<Record<string, unknown>>
  items: Idea[]
  used_gaps: Array<{ id: number; gap_text: string; raised_by_paper_ids: number[]; span_count: number }>
  compliance_note?: string
}

export interface FeasibilityDimension {
  key: string
  label: string
  /** 界面展示的分：模型评审分或规则分；**未评为 null**（null ≠ 0 分） */
  score: number | null
  /** 模型生成的约 50 字分析（界面只读，研究者不能改） */
  analysis?: string
  /** 这个分是谁给的：model_review / rule / not_evaluated */
  score_source?: string
  /** 规则层的分与依据（审计基线，可复算） */
  rule_score?: number | null
  rule_rationale?: string | null
  rationale: string
  evidence: EvidenceDetail[]
  evidence_count: number
  evidence_ids?: number[]
  evidence_note?: string | null
  signals: Record<string, unknown>
  signals_missing?: string[]
  formula: string
  llm_suggestion?: {
    suggested_score: number | null
    comment: string | null
    model_ref?: string
    provider?: string
    used_in_total: boolean
    note?: string
  }
}

export interface RiskItem {
  key: string
  risk: string
  level: 'low' | 'medium' | 'high' | string
  mitigation: string
  level_basis: string
  trigger: Record<string, unknown>
}

export interface MveStep {
  step: number
  action: string
  detail: string
  endpoint: string | null
  expected_output: string
  duration_minutes: number
  duration_source: string
}

export interface MvePlan {
  objective: string
  template_id: string
  template_rationale: string
  template_whitelist: string[]
  dataset: {
    name: string | null
    confirmed: boolean
    needs_confirmation: boolean
    source: string
    paper_id?: number | null
    paper_span_id?: number | null
    quote_text?: string | null
    jump_url?: string | null
    candidates: string[]
    note?: string
  }
  metrics: { values: string[]; source: string; missing: boolean; note?: string }
  baselines: { values: string[]; source: string; note?: string | null }
  sample_size: number
  sample_size_limit: number
  steps: MveStep[]
  step_count: number
  expected_duration_minutes: number
  duration_note: string
  expected_cost_usd: number | null
  cost_note: string
  rounds: Record<string, number>
  stop_rules: Record<string, number>
  success_criteria: string[]
  guardrail_notes: string[]
  human_review_checklist: Array<{ item: string; why: string; blocking: boolean }>
  unsupported_assumptions: Array<{ assumption: string; status: string; reason: string }>
  readiness: string
}

export interface Feasibility {
  id: number
  feasibility_id: number
  idea_id: number
  project_id?: number | null
  paper_ids?: number[]
  dimensions: FeasibilityDimension[]
  dimension_scores?: Record<string, number | null>
  total_score: number
  /** 总分怎么来的：seven_dimension_average（七维平均）/ rule_weighted（规则加权） */
  total_score_source?: string
  scoring?: {
    weights: Record<string, number>
    contributions: Array<{
      key: string
      label: string
      score: number
      weight: number
      contribution: number
    }>
    formula: string
    owner: string
    note: string
    dimension_direction: string
  }
  risk_list: RiskItem[]
  risk_count?: number
  risk_summary?: {
    risk_count: number
    level_counts: Record<string, number>
    highest_level: string | null
    evaluated_rules: Array<Record<string, unknown>>
    thresholds: Record<string, number>
    policy_note: string
  }
  mve_plan: MvePlan
  dimensions_without_evidence?: string[]
  all_dimensions_have_evidence?: boolean
  llm_suggestions?: Record<string, unknown>
  llm_used_in_total?: boolean
  created_at?: string | null
  compliance_note?: string
}

export type TaskbookStatus = 'draft' | 'locked'

export interface Taskbook {
  id: number
  taskbook_id: number
  project_id: number
  idea_id: number
  research_question: string
  target_datasets: string[]
  baselines: string[]
  metrics: string[]
  compute_budget: {
    max_llm_cost_usd?: number
    demo_cost_quota_usd?: number
    max_stage_minutes?: number
    guardrail?: {
      hard_limit_usd: number
      demo_quota_usd: number
      rule: string
      relaxable: boolean
    }
  }
  deliverables: string[]
  max_iterations: number | null
  score_threshold: number | null
  marginal_gain_threshold: number | null
  max_retry: number | null
  status: TaskbookStatus | string
  locked: boolean
  read_only: boolean
  locked_at: string | null
  created_at: string | null
  editable_fields: string[]
}

/** 交付形态白名单（与后端 DELIVERABLE_OPTIONS 对齐） */
export const DELIVERABLE_OPTIONS = ['paper_draft', 'code', 'experiment_log', 'slides'] as const

/* --------------------------------------------------------------------- */
/* 聚合                                                                   */
/* --------------------------------------------------------------------- */
export function createAggregation(
  body: { paper_ids: number[]; project_id?: number | null },
  signal?: AbortSignal,
) {
  return post<Aggregation>('/aggregations', { body, signal, timeoutMs: 180_000 })
}

export function listAggregations(
  options: { projectId?: number | null; limit?: number; signal?: AbortSignal } = {},
) {
  const query: Record<string, string | number> = {}
  if (options.projectId) query.project_id = options.projectId
  if (options.limit) query.limit = options.limit
  return get<{ items: Aggregation[]; total: number }>('/aggregations', {
    query,
    signal: options.signal,
  })
}

export function getAggregation(id: number | string, signal?: AbortSignal) {
  return get<Aggregation>(`/aggregations/${id}`, { signal })
}

export function getAggregationGaps(id: number | string, signal?: AbortSignal) {
  return get<{ aggregation_id: number; items: Gap[]; total: number; scope_note: string }>(
    `/aggregations/${id}/gaps`,
    { signal },
  )
}

export function deleteAggregation(id: number | string, signal?: AbortSignal) {
  return del<{ deleted: boolean }>(`/aggregations/${id}`, { signal })
}

/* --------------------------------------------------------------------- */
/* idea                                                                   */
/* --------------------------------------------------------------------- */
export function generateIdeas(
  body: {
    aggregation_id: number
    /** 总条数（不传 directions 时）/ 每个方向各几条（传 directions 时） */
    count?: number
    /** 四个方向固定口径：要产出的方向；只给一个＝对该方向再来一批备选 */
    directions?: IdeaMechanism[]
    project_id?: number | null
    mode?: IdeaMode
    model_ref?: string | null
    temperature?: number
  },
  signal?: AbortSignal,
) {
  return post<IdeaGenerationResult>('/ideas/generate', { body, signal, timeoutMs: 180_000 })
}

export function createManualIdea(
  body: {
    title: string
    content: string
    project_id?: number | null
    aggregation_id?: number | null
    mechanism?: IdeaMechanism | null
    novelty_note?: string | null
    evidences?: EvidenceCandidate[]
  },
  signal?: AbortSignal,
) {
  return post<{
    item: Idea
    binding: Record<string, unknown> | null
    evidence_status: { has_evidence: boolean; needs_evidence: boolean; message: string | null }
    next_step: string
  }>('/ideas', { body, signal })
}

export function listIdeas(
  options: {
    projectId?: number | null
    aggregationId?: number | null
    origin?: 'ai_generated' | 'user_input'
    onlyWithEvidence?: boolean
    page?: number
    pageSize?: number
    signal?: AbortSignal
  } = {},
) {
  const query: Record<string, string | number | boolean> = {}
  if (options.projectId) query.project_id = options.projectId
  if (options.aggregationId) query.aggregation_id = options.aggregationId
  if (options.origin) query.origin = options.origin
  if (options.onlyWithEvidence) query.only_with_evidence = true
  if (options.page) query.page = options.page
  if (options.pageSize) query.page_size = options.pageSize
  return get<Paginated<Idea> & { evidence_policy: string }>('/ideas', {
    query,
    signal: options.signal,
  })
}

export function getIdea(id: number | string, signal?: AbortSignal) {
  return get<{ item: Idea; next_step: string }>(`/ideas/${id}`, { signal })
}

export function getIdeaEvidences(id: number | string, signal?: AbortSignal) {
  return get<{
    idea_id: number
    idea_title: string
    items: EvidenceDetail[]
    total: number
    has_evidence: boolean
    gate: { rule: string; ok: boolean; evidence_scope: string | null }
  }>(`/ideas/${id}/evidences`, { signal })
}

export function bindIdeaEvidences(
  id: number | string,
  body: { evidences: EvidenceCandidate[]; replace?: boolean },
  signal?: AbortSignal,
) {
  return post<{
    binding: {
      bound: EvidenceDetail[]
      bound_ids: number[]
      rejected: Array<{ code: string; reason: string }>
      rejected_total: number
      warnings: string[]
    }
    item: Idea
    bound_count: number
    rejected_count: number
    gate: { rule: string; ok: boolean }
  }>(`/ideas/${id}/evidences`, { body, signal })
}

export function selectIdea(id: number | string, signal?: AbortSignal) {
  return post<{ item: Idea; cleared_siblings: number }>(`/ideas/${id}/select`, { signal })
}

/* --------------------------------------------------------------------- */
/* 可行性                                                                 */
/* --------------------------------------------------------------------- */
export function createFeasibility(
  body: {
    idea_id: number
    project_id?: number | null
    aggregation_id?: number | null
    sample_size?: number
    use_llm?: boolean
    model_ref?: string | null
    rounds?: Record<string, unknown> | null
  },
  signal?: AbortSignal,
) {
  return post<Feasibility>('/feasibility', { body, signal, timeoutMs: 180_000 })
}

export function getFeasibility(id: number | string, signal?: AbortSignal) {
  return get<Feasibility>(`/feasibility/${id}`, { signal })
}

export function getFeasibilityByIdea(ideaId: number | string, signal?: AbortSignal) {
  return get<Feasibility>('/feasibility', { query: { idea_id: ideaId }, signal })
}

/* --------------------------------------------------------------------- */
/* 任务书                                                                 */
/* --------------------------------------------------------------------- */
export function createTaskbook(
  body: {
    project_id: number
    idea_id: number
    research_question: string
    target_datasets?: string[]
    baselines?: string[]
    metrics?: string[]
    compute_budget?: Record<string, unknown> | null
    deliverables?: string[] | null
    rounds?: Record<string, unknown> | null
  },
  signal?: AbortSignal,
) {
  return post<{
    item: Taskbook
    guardrail: Taskbook['compute_budget']['guardrail'] | null
    defaults: { rounds: Record<string, number>; compute_budget: Record<string, number> }
    next_step: string
  }>('/taskbooks', { body, signal })
}

export function getTaskbook(id: number | string, signal?: AbortSignal) {
  return get<Taskbook>(`/taskbooks/${id}`, { signal })
}

export function listTaskbooks(
  options: { projectId?: number | null; limit?: number; signal?: AbortSignal } = {},
) {
  const query: Record<string, string | number> = {}
  if (options.projectId) query.project_id = options.projectId
  if (options.limit) query.limit = options.limit
  return get<{ items: Taskbook[]; total: number }>('/taskbooks', { query, signal: options.signal })
}

export function patchTaskbook(id: number | string, body: Record<string, unknown>, signal?: AbortSignal) {
  return patch<{ item: Taskbook; changed_fields: string[] }>(`/taskbooks/${id}`, { body, signal })
}

export function lockTaskbook(id: number | string, signal?: AbortSignal) {
  return post<{
    item: Taskbook
    already_locked: boolean
    locked_now: boolean
    idea_evidence_ids?: number[]
    next_step?: string
  }>(`/taskbooks/${id}/lock`, { signal })
}

/* --------------------------------------------------------------------- */
/* 展示辅助（仅格式化，不做任何数据编造）                                  */
/* --------------------------------------------------------------------- */
/** 四个创新方向的中文名（研究构想界面按此展示，与后端 description 同口径） */
export const MECHANISM_LABELS: Record<string, string> = {
  refinement: '方法迭代型',
  transfer: '场景迁移型',
  combination: '技术融合型',
  paradigm: '范式拓展型',
}

/** 方向的创新层级（卡片上的一行说明） */
export const MECHANISM_LAYERS: Record<string, string> = {
  refinement: '增量创新',
  transfer: '拓展创新',
  combination: '交叉创新',
  paradigm: '范式创新',
}

/** 方向的难度评分（0–100，越高越难）—— 界面用数值，不用星级 */
export const MECHANISM_DIFFICULTY: Record<string, number> = {
  refinement: 40,
  transfer: 60,
  combination: 80,
  paradigm: 100,
}

/** 七个可行性维度的固定展示顺序（与后端 scorer.REVIEW_DIMENSIONS 一致） */
export const REVIEW_DIMENSIONS = [
  'method_maturity',
  'data_availability',
  'compute_cost',
  'novelty_gap',
  'landing_risk',
  'application_value',
  'ethics_compliance',
] as const

export const RISK_LEVEL_LABELS: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
}

export const ORIGIN_LABELS: Record<string, string> = {
  ai_generated: 'AI 生成',
  user_input: '手动录入',
}

export const MODE_LABELS: Record<string, string> = {
  llm: 'LLM 生成',
  template: '模板合成（未调用 LLM）',
  auto: '自动（优先 LLM）',
}

/** 覆盖率展示：null → 「无定义」，绝不显示 0（与 WP13 同口径） */
export function formatCoverage(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '无定义'
  return `${Math.round(value * 1000) / 10}%`
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return String(Math.round(Number(value) * 100) / 100)
}

/** 证据类型的人话标签 */
export function evidenceTypeLabel(type: string | null | undefined): string {
  const map: Record<string, string> = {
    paper_span: '原文段落',
    card_field: '卡片字段',
    experiment_run: '实验运行',
    experiment_passport: '实验凭证',
    decision: '决策日志',
  }
  return map[String(type)] ?? String(type ?? '未知证据')
}
