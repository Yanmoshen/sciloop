/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 工作台数据层（WP15-T8）：封装工作台全部接口。
 *
 * 分层约定（contracts.code_style.frontend）：
 * - 组件内**禁止**直接 fetch；一切请求经 `src/api/*.ts`
 * - 流水线状态 / SSE 订阅**复用 WP09 的 `api/pipeline.ts`**（含断线重连与事件分发），本文件不再重写
 * - 未挂载的接口（WP11/WP13/WP14）用 `probe()` 包装：返回 `{available:false, waitingFor}`，
 *   由 UI 显示「等待 WPxx 交付」的空状态，**绝不伪造数据**
 *
 * 契约与状态裁决：
 * - `contracts.sse_events`：事件名与载荷
 * - 事件流只做实时展示；**状态一律以 status/stages 等快照接口为准**（SSE onOpen 时重新拉取）
 */

import {
  ApiError,
  apiUrl,
  get,
  post,
  streamUrl,
  type Paginated,
  type RequestOptions,
} from './client'
import {
  getDecisionLogs,
  getFailureReport,
  getInterventions,
  getPipelineStages,
  getPipelineStatus,
  getRecentEvents,
  intervene,
  listPipelineRuns,
  pausePipeline,
  resumePipeline,
  runPipeline,
  stopPipeline,
  subscribePipelineEvents,
  switchPipelineMode,
  PIPELINE_EVENT_NAMES,
  type DecisionLogItem,
  type FailureReport,
  type InterventionAction,
  type InterventionItem,
  type InterventionNode,
  type PipelineEvent,
  type PipelineEventName,
  type PipelineMode,
  type PipelineRunInfo,
  type PipelineStage,
  type PipelineStagesResponse,
  type PipelineStatus,
  type PipelineSubscription,
  type PolicyAction,
  type SSEPayloadMap,
  type StageDetail,
  type StageStatus,
  type StopReason,
} from './pipeline'

/* ------------------------------------------------------------------ *
 * 契约枚举再导出（组件只依赖本文件即可）
 * ------------------------------------------------------------------ */

export type {
  DecisionLogItem,
  FailureReport,
  InterventionAction,
  InterventionItem,
  InterventionNode,
  PipelineEvent,
  PipelineEventName,
  PipelineMode,
  PipelineRunInfo,
  PipelineStage,
  PipelineStagesResponse,
  PipelineStatus,
  PipelineSubscription,
  PolicyAction,
  SSEPayloadMap,
  StageDetail,
  StageStatus,
  StopReason,
}

export {
  getDecisionLogs,
  getFailureReport,
  getInterventions,
  getPipelineStages,
  getPipelineStatus,
  getRecentEvents,
  intervene,
  listPipelineRuns,
  pausePipeline,
  resumePipeline,
  runPipeline,
  stopPipeline,
  subscribePipelineEvents,
  switchPipelineMode,
  streamUrl,
  PIPELINE_EVENT_NAMES,
}

/* ------------------------------------------------------------------ *
 * 常量：环节 / 决策点 / 介入节点（来自 contracts）
 * ------------------------------------------------------------------ */

/** 六环节顺序固定（contracts.enums.pipeline_stage） */
export const STAGE_ORDER: PipelineStage[] = [
  'survey',
  'plan',
  'plan_review',
  'experiment',
  'writing',
  'review',
]

export const STAGE_LABELS: Record<PipelineStage, string> = {
  survey: '文献检索',
  plan: '方案生成',
  plan_review: '方案盲评',
  experiment: '实验执行',
  writing: '草稿写作',
  review: '评审迭代',
}

/** 决策点名称（contracts.decision_points） */
export const DECISION_POINTS: Array<{ id: string; name: string; stage: PipelineStage }> = [
  { id: 'D1', name: '检索策略', stage: 'survey' },
  { id: 'D2', name: '方案选型', stage: 'plan_review' },
  { id: 'D3', name: '实验配置', stage: 'experiment' },
  { id: 'D4', name: '失败处置', stage: 'any' as PipelineStage },
  { id: 'D5', name: '迭代判据', stage: 'review' },
  { id: 'D6', name: '写作结构', stage: 'writing' },
]

/** 介入节点（contracts.intervention_mapping） */
export const INTERVENTION_NODES: Array<{
  id: InterventionNode
  trigger: string
  decision_point: string | null
  actions: InterventionAction[]
}> = [
  {
    id: 'N1',
    trigger: '任务书提交后（流水线前的前置门禁）',
    decision_point: null,
    actions: ['approve', 'modify', 'reject'],
  },
  {
    id: 'N2',
    trigger: 'plan_review 产出后',
    decision_point: 'D2',
    actions: ['approve', 'modify', 'reject', 'rerun'],
  },
  {
    id: 'N3',
    trigger: '实验指标异常或 L2 失败',
    decision_point: 'D4',
    actions: ['approve', 'modify', 'reject', 'rerun', 'downgrade', 'abort'],
  },
  {
    id: 'N4',
    trigger: 'writing 大纲生成后',
    decision_point: 'D6',
    actions: ['approve', 'modify', 'reject'],
  },
]

/** 停止原因中文说明（contracts.enums.stop_reason） */
export const STOP_REASON_LABELS: Record<StopReason, string> = {
  score_threshold: '达到评分阈值',
  marginal_stagnation: '边际增益停滞',
  max_iterations: '达到最大迭代轮次',
  manual: '人工中止',
}

/** 策略动作（contracts.enums.policy_action） */
export const POLICY_ACTION_LABELS: Record<PolicyAction, string> = {
  auto_execute: '自动执行',
  need_human: '人工确认',
  circuit_break: '熔断',
}

/** 成本双线默认值（contracts.guardrails.cost；接口不可用时的兜底展示） */
export const COST_DEFAULT_LIMIT_USD = 8.0
export const COST_DEFAULT_QUOTA_USD = 3.0

/* ------------------------------------------------------------------ *
 * 未挂载接口的空状态语义
 * ------------------------------------------------------------------ */

export interface PanelAvailable<T> {
  available: true
  data: T
}

export interface PanelUnavailable {
  available: false
  /** 等待哪个工作包交付（显示在空状态里） */
  waitingFor: string
  reason: string
}

export type PanelResult<T> = PanelAvailable<T> | PanelUnavailable

/** FastAPI 未挂载模块返回 404/405；501 表示端点已声明但未实现 */
const ROUTE_ABSENT_STATUS = new Set([404, 405, 501])

/**
 * 探测式调用：接口未挂载时返回可展示的空状态，其余错误照常抛出。
 * `waitingFor` 会原样显示给用户（例如「等待 WP11 交付 /experiments/runs/{id}/passport」）。
 */
export async function probe<T>(waitingFor: string, fn: () => Promise<T>): Promise<PanelResult<T>> {
  try {
    return { available: true, data: await fn() }
  } catch (error) {
    if (error instanceof ApiError && ROUTE_ABSENT_STATUS.has(error.status)) {
      return {
        available: false,
        waitingFor,
        reason: `接口尚未挂载（HTTP ${error.status} ${error.code}）`,
      }
    }
    if (error instanceof ApiError) throw error
    return { available: false, waitingFor, reason: (error as Error).message }
  }
}

/* ------------------------------------------------------------------ *
 * Project 列表
 * ------------------------------------------------------------------ */

export interface WorkbenchProject {
  id: number
  name: string
  status?: string
  mode?: string
  is_demo?: boolean
  current_iteration?: number
  created_at?: string
}

export interface ProjectListResult {
  items: WorkbenchProject[]
  /** 实际生效的接口路径（用于在 UI 上如实披露数据来源） */
  source: string
  /** 非空表示线上接口尚未就绪、已回退到备用接口 */
  fallbackNote: string | null
}

/**
 * Project 列表。
 *
 * 契约路径为 `GET /projects`；当前后端只挂载了 `GET /demo/projects`（WP16），
 * 因此优先请求契约路径，404 时回退到 demo 列表并如实标注来源（不伪造项目）。
 */
export async function listWorkbenchProjects(): Promise<ProjectListResult> {
  try {
    const data = await get<Paginated<WorkbenchProject> | WorkbenchProject[]>('/projects', {
      query: { page: 1, page_size: 50 },
    })
    return {
      items: Array.isArray(data) ? data : (data.items ?? []),
      source: 'GET /api/v1/projects',
      fallbackNote: null,
    }
  } catch (error) {
    if (!(error instanceof ApiError) || !ROUTE_ABSENT_STATUS.has(error.status)) throw error
    const data = await get<{ items: WorkbenchProject[]; total: number }>('/demo/projects')
    return {
      items: data.items ?? [],
      source: 'GET /api/v1/demo/projects（回退）',
      fallbackNote: `GET /projects 未挂载（HTTP ${error.status}），已回退到 /demo/projects；项目容器接口就绪后自动切回`,
    }
  }
}

export function getProjectDetail(projectId: number) {
  return get<WorkbenchProject & Record<string, unknown>>(`/projects/${projectId}`)
}

/* ------------------------------------------------------------------ *
 * 决策列表（WP10 的 /decisions/{project_id} 比 /pipelines/{pid}/decision-logs 更全）
 * ------------------------------------------------------------------ */

export interface RiskThresholdsPayload {
  risk_auto_max: number
  confidence_auto_min: number
  reversibility_auto_min: number
  confidence_break_min_reserved?: number
  confidence_break_min_note?: string
  overridable_by_llm: boolean
  source: string
  policy_version: string
}

export interface GuardrailLimitsPayload {
  version: string
  run_timeout_seconds: number
  stage_timeout_seconds: number
  cost_hard_limit_usd: number
  cost_demo_quota_usd: number
  sample_size_max: number
  template_whitelist: string[]
  judgement_order: string[]
  thresholds_overridable: boolean
  demo_quota_is_warning_only: boolean
}

/** 决策记录 + WP10 补充的节点映射与记录类型 */
export interface DecisionRecord extends DecisionLogItem {
  node?: InterventionNode | null
  record_kind?: string
}

export interface DecisionOverview extends Paginated<DecisionRecord> {
  policy_version?: string
  thresholds?: RiskThresholdsPayload
  guardrail_limits?: GuardrailLimitsPayload
  options_by_decision_point?: Record<string, string[]>
  fallback_events?: Array<Record<string, unknown>>
  note?: string
  /** 数据来源接口（如实披露） */
  source?: string
}

/**
 * 决策总览：`GET /decisions/{project_id}`，失败时回退 WP09 的
 * `GET /pipelines/{project_id}/decision-logs`（少 node/thresholds，界面会标注）。
 */
export async function getDecisionOverview(projectId: number, pageSize = 100): Promise<DecisionOverview> {
  try {
    const data = await get<DecisionOverview>(`/decisions/${projectId}`, {
      query: { page: 1, page_size: pageSize },
    })
    return { ...data, source: `GET /decisions/${projectId}` }
  } catch (error) {
    if (!(error instanceof ApiError) || !ROUTE_ABSENT_STATUS.has(error.status)) throw error
    const fallback = await getDecisionLogs(projectId, { page: 1, page_size: pageSize })
    return {
      ...fallback,
      source: `GET /pipelines/${projectId}/decision-logs（回退：无节点映射与阈值来源）`,
    }
  }
}

export interface RiskFeatureDetail {
  name: string
  raw: number | null
  normalized: number | null
  weight: number
  source: string
  kind?: string
  applicable?: boolean
  missing?: boolean
  note?: string
}

export interface EvaluateRiskResponse {
  decision_id: number
  project_id: number
  decision_point: string
  stage: string | null
  persisted: boolean
  new_decision_log_id: number | null
  original: {
    risk_score: number | null
    confidence_score: number | null
    reversibility_score: number | null
    policy_action: PolicyAction
    chosen: string
    policy_version: string
  }
  recomputed: {
    risk_score: number | null
    confidence_score: number | null
    reversibility_score: number | null
    policy_action: PolicyAction
    chosen: string
    policy_version: string
  }
  changed: boolean
  result?: {
    policy_action: PolicyAction
    risk_score: number | null
    confidence_score: number | null
    reversibility_score: number | null
    features?: {
      risk?: { contributions?: Record<string, unknown>; features?: Record<string, RiskFeatureDetail> }
      confidence?: { contributions?: Record<string, unknown>; features?: Record<string, RiskFeatureDetail> }
      reversibility?: { contributions?: Record<string, unknown> }
      [key: string]: unknown
    }
    [key: string]: unknown
  }
  [key: string]: unknown
}

/** 用当前上下文重算风险三分与策略动作（persist=true 需 Owner，会追加审计记录） */
export function evaluateDecisionRisk(
  decisionId: number,
  options: { context?: Record<string, unknown>; persist?: boolean } = {},
) {
  return post<EvaluateRiskResponse>(`/decisions/${decisionId}/evaluate-risk`, {
    body: { context: options.context ?? {}, persist: options.persist === true },
  })
}

export interface ApproveDecisionResponse {
  decision_id: number
  intervention_id: number | null
  project_status: string
  action: string
  node: InterventionNode | null
  ready_to_resume: boolean
  resumed?: Record<string, unknown> | null
  note?: string
  [key: string]: unknown
}

/**
 * 人工处置高风险决策（Owner）。
 * `action` ∈ approve|modify|reject|rerun|downgrade|abort|switch_mode；
 * `resume=true` 时后端在解除 WAIT_HUMAN 后立即触发断点续跑。
 */
export function approveDecision(
  decisionId: number,
  options: {
    action: InterventionAction
    node?: InterventionNode | null
    note?: string
    payload?: Record<string, unknown>
    resume?: boolean
  },
) {
  return post<ApproveDecisionResponse>(`/decisions/${decisionId}/approve`, {
    body: {
      action: options.action,
      node: options.node ?? undefined,
      note: options.note,
      payload: options.payload,
      resume: options.resume === true,
    },
  })
}

/* ------------------------------------------------------------------ *
 * 成本双线（WP02 的 /costs）
 * ------------------------------------------------------------------ */

export interface CostSummary {
  used_usd: number
  limit_usd: number
  quota_usd: number
  quota_exceeded: boolean
  limit_exceeded: boolean
  breakdown_by_stage: Record<string, number>
  replay_saved_usd: number
  cost_complete: boolean
  warning: string | null
  calls?: number
  replay_calls?: number
  failed_calls?: number
  unknown_price_calls?: number
  currency?: string
  project_id?: number | null
  source?: string
}

export function getCostSummary(projectId?: number | null) {
  return get<CostSummary>('/costs/summary', {
    query: { project_id: projectId ?? undefined },
  })
}

export function getCostLimits() {
  return get<{ limit_usd: number; quota_usd: number; rule: string }>('/costs/limits')
}

/* ------------------------------------------------------------------ *
 * 盲评校准（WP12）
 * ------------------------------------------------------------------ */

export interface CandidateOrderItem {
  alias: string
  /** 匿名化后的展示位次（服务端 shuffle 结果） */
  position: number
}

export interface ModelScoreEntry {
  alias: string
  novelty: number | null
  feasibility: number | null
  rigor: number | null
  cost_reasonableness: number | null
  risk_control: number | null
  total: number | null
  selected: boolean
  comments: string[]
  [key: string]: unknown
}

export interface HumanLabelEntry {
  candidate_alias: string | null
  method_index?: number | null
  decision: 'approve' | 'revise' | 'reject'
  total?: number | null
  scores?: Partial<Record<'novelty' | 'feasibility' | 'rigor' | 'cost_reasonableness' | 'risk_control', number>>
  note?: string | null
  labeler?: string | null
  labeled_at?: string | null
}

export interface CalibrationPair {
  alias: string
  model_total: number | null
  human_total: number | null
  model_selected: boolean
  human_decision: string
  agree: boolean
  [key: string]: unknown
}

export interface CalibrationMetricBlock {
  metric: string
  value: number | null
  computable: boolean
  reason: string | null
  sample_size: number
  definition?: string
  [key: string]: unknown
}

export interface CalibrationReport {
  project_id: number
  status: 'pending' | 'calibrated'
  sample_size: number
  sample_size_requirement?: number
  small_sample_threshold: number
  agreement_metric: 'cohen_kappa' | 'mae' | null
  agreement_value: number | null
  candidate_order: CandidateOrderItem[]
  model_scores: ModelScoreEntry[]
  human_labels: HumanLabelEntry[]
  human_label_min?: number
  pairs?: CalibrationPair[]
  classification?: CalibrationMetricBlock & { confusion?: Record<string, number> }
  continuous?: CalibrationMetricBlock
  confidence_interval?: Record<string, unknown> | null
  disclosure?: {
    sample_size: number
    small_sample_threshold: number
    is_small_sample: boolean
    significance: string
    statement: string
  }
  summary?: string
  significance?: string
  note?: string
  warnings?: string[]
  /** 隔离事实：两处 ref 不同才允许展示盲评（不向界面暴露 ref 原文） */
  generator_model_ref?: string | null
  reviewer_model_ref?: string | null
  anonymization_version?: string | null
  shuffle_seed?: number | null
  calibration_id?: number | null
  pipeline_run_id?: number | null
  [key: string]: unknown
}

export function getReviewCalibration(projectId: number) {
  return get<CalibrationReport>(`/pipelines/${projectId}/review-calibration`)
}

/** 录入人工标签（Owner；≥3 条起算一致率） */
export function submitHumanLabels(
  projectId: number,
  labels: HumanLabelEntry[],
  options: { replace?: boolean } = {},
) {
  return post<CalibrationReport>(`/pipelines/${projectId}/human-labels`, {
    body: { labels, replace: options.replace === true },
  })
}

/* ------------------------------------------------------------------ *
 * 实验 / Passport（WP11，未挂载）
 * ------------------------------------------------------------------ */

export interface ExperimentTemplate {
  template_id: string
  name?: string
  description?: string
  params_schema?: Record<string, unknown>
  [key: string]: unknown
}

export interface ExperimentRun {
  id: number
  experiment_id?: number
  template_id?: string
  status?: string
  started_at?: string | null
  finished_at?: string | null
  cost_usd?: number
  metrics?: Record<string, unknown>
  [key: string]: unknown
}

export interface PassportRecord {
  id: number
  status: 'complete' | 'incomplete' | string
  dataset_name?: string | null
  dataset_version?: string | null
  dataset_sha256?: string | null
  sample_manifest?: unknown
  provider?: string | null
  model_id?: string | null
  prompt_version?: string | null
  prompt_sha256?: string | null
  generation_params?: Record<string, unknown> | null
  template_id?: string | null
  template_config?: Record<string, unknown> | null
  code_commit_sha?: string | null
  dependency_lock_sha256?: string | null
  metrics?: Record<string, unknown> | null
  cost_usd?: number | null
  is_replay?: boolean
  artifact_manifest?: unknown
  parent_passport_id?: number | null
  started_at?: string | null
  finished_at?: string | null
  /** 缺失的关键字段（status=incomplete 时由服务端给出，禁止前端猜测） */
  missing_fields?: string[]
  [key: string]: unknown
}

export interface PassportDiffReport {
  parent_passport_id?: number | null
  child_passport_id?: number | null
  is_replay?: boolean
  metric_diff?: Record<string, { parent?: number | null; child?: number | null; delta?: number | null }>
  cost_diff?: { parent?: number | null; child?: number | null; delta?: number | null }
  duration_diff?: { parent_ms?: number | null; child_ms?: number | null; delta_ms?: number | null }
  verdict?: string | null
  note?: string
  [key: string]: unknown
}

export const WP11_WAITING = '等待 WP11 交付 /experiments、/passports'

export function listExperimentTemplates() {
  return probe<{ items: ExperimentTemplate[] } | ExperimentTemplate[]>(
    WP11_WAITING,
    () => get<{ items: ExperimentTemplate[] } | ExperimentTemplate[]>('/experiments/templates'),
  )
}

export function listExperimentRuns(experimentId: number) {
  return probe<{ items: ExperimentRun[] } | ExperimentRun[]>(WP11_WAITING, () =>
    get<{ items: ExperimentRun[] } | ExperimentRun[]>(`/experiments/${experimentId}/runs`),
  )
}

export function getRunMetrics(runId: number) {
  return probe<{ items: Array<Record<string, unknown>> } | Record<string, unknown>>(
    WP11_WAITING,
    () => get(`/experiments/runs/${runId}/metrics`),
  )
}

export function getRunPassport(runId: number) {
  return probe<PassportRecord>(WP11_WAITING, () => get<PassportRecord>(`/experiments/runs/${runId}/passport`))
}

/** 回放（public_demo 面唯一允许的写操作，有速率限制） */
export function replayPassport(passportId: number) {
  return post<{ passport?: PassportRecord; diff?: PassportDiffReport; is_replay?: boolean; note?: string }>(
    `/passports/${passportId}/replay`,
  )
}

/** 用当前实时模型重跑（仅 Owner） */
export function rerunPassport(passportId: number) {
  return post<{ passport?: PassportRecord; diff?: PassportDiffReport; is_replay?: boolean; note?: string }>(
    `/passports/${passportId}/rerun`,
  )
}

/* ------------------------------------------------------------------ *
 * 证据链（WP13，未挂载）
 * ------------------------------------------------------------------ */

export const WP13_WAITING = '等待 WP13 交付 /evidence'

export type EvidenceType =
  | 'paper_span'
  | 'card_field'
  | 'experiment_run'
  | 'experiment_passport'
  | 'decision'

export interface EvidenceRecord {
  id?: number
  evidence_type?: EvidenceType | string
  verdict?: 'valid' | 'valid_by_hash' | 'invalid' | string
  coverage?: number | null
  quote_text?: string | null
  quote_sha256?: string | null
  section_name?: string | null
  page_number?: number | null
  char_start?: number | null
  char_end?: number | null
  document_version?: number | null
  paper_id?: number | null
  note?: string | null
  [key: string]: unknown
}

export function getEvidence(evidenceType: EvidenceType | string, evidenceId: number | string) {
  return probe<EvidenceRecord>(WP13_WAITING, () =>
    get<EvidenceRecord>(`/evidence/${evidenceType}/${evidenceId}`),
  )
}

/* ------------------------------------------------------------------ *
 * 草稿与 Claim（WP14，未挂载）
 * ------------------------------------------------------------------ */

export const WP14_WAITING = '等待 WP14 交付 /drafts'

export type ClaimStatus = 'supported' | 'contradicted' | 'insufficient'

export interface DraftClaim {
  id: number
  draft_id?: number
  claim_text?: string
  text?: string
  status: ClaimStatus
  evidence_ids?: number[]
  evidence?: Array<Record<string, unknown>>
  section_name?: string | null
  note?: string | null
  [key: string]: unknown
}

export interface DraftRecord {
  id: number
  project_id?: number
  title?: string | null
  content_md?: string | null
  content_markdown?: string | null
  version?: number | null
  claim_coverage?: number | null
  status?: string | null
  created_at?: string | null
  artifacts?: Array<{ name?: string; kind?: string; url?: string; [key: string]: unknown }>
  [key: string]: unknown
}

export function getDraft(draftId: number | string) {
  return probe<DraftRecord>(WP14_WAITING, () => get<DraftRecord>(`/drafts/${draftId}`))
}

export function getDraftClaims(draftId: number | string) {
  return probe<{ items: DraftClaim[] } | DraftClaim[]>(WP14_WAITING, () =>
    get<{ items: DraftClaim[] } | DraftClaim[]>(`/drafts/${draftId}/claims`),
  )
}

/** 逐条校验草稿 Claim 的证据（先比 quote_sha256 再比字符偏移） */
export function verifyDraftEvidence(draftId: number | string) {
  return probe<Record<string, unknown>>(WP13_WAITING, () =>
    post<Record<string, unknown>>(`/drafts/${draftId}/verify-evidence`),
  )
}

/**
 * 导出草稿（md | pdf）。pdf 未实现时后端返回 501，由 probe 转成空状态。
 * URL 由公共 client 的 `apiUrl()` 构建，本文件不再自行解析 `VITE_API_BASE`。
 */
export function draftExportUrl(draftId: number | string, format: 'md' | 'pdf' = 'md') {
  return apiUrl(`/drafts/${draftId}/export`, { format })
}

/* ------------------------------------------------------------------ *
 * 演示 / 访问面（WP16 的接口，已挂载）
 * ------------------------------------------------------------------ */

export interface DemoStatus {
  access_mode: 'public_demo' | 'owner_mode'
  snapshot: boolean
  replay: boolean
  demo_mode: string
  is_demo: boolean
  [key: string]: unknown
}

export function getDemoStatus() {
  return get<DemoStatus>('/demo/status')
}

export interface OwnerSession {
  access_mode: 'public_demo' | 'owner_mode'
  is_owner: boolean
  demo_mode: string
  snapshot: boolean
  replay: boolean
  permissions: { writes_allowed: boolean; reason?: string }
  [key: string]: unknown
}

export function getOwnerSession() {
  return get<OwnerSession>('/owner/session')
}

export function verifyOwnerToken(token: string) {
  return post<{ ok: boolean; access_mode: string; note?: string }>('/owner/verify', {
    headers: { 'X-Owner-Token': token },
  })
}

/* ------------------------------------------------------------------ *
 * 类型再导出
 *
 * 这里**不再**导出 `rawRequest` / `apiGet` 之类的 transport 别名：
 * 领域文件只做「打哪个端点、载荷长什么样」，传输层一律从 `./client` 直接取，
 * 避免在领域模块里长出一套与公共 client 平行的第二入口（主计划 P1-1）。
 * ------------------------------------------------------------------ */

export type { ApiError, Paginated, RequestOptions }

export default {
  COST_DEFAULT_LIMIT_USD,
  COST_DEFAULT_QUOTA_USD,
  DECISION_POINTS,
  INTERVENTION_NODES,
  STAGE_LABELS,
  STAGE_ORDER,
  STOP_REASON_LABELS,
  approveDecision,
  draftExportUrl,
  evaluateDecisionRisk,
  getCostLimits,
  getCostSummary,
  getDecisionOverview,
  getDemoStatus,
  getDraft,
  getDraftClaims,
  getEvidence,
  getOwnerSession,
  getProjectDetail,
  getReviewCalibration,
  getRunMetrics,
  getRunPassport,
  listExperimentRuns,
  listExperimentTemplates,
  listWorkbenchProjects,
  probe,
  replayPassport,
  rerunPassport,
  submitHumanLabels,
  verifyDraftEvidence,
}
