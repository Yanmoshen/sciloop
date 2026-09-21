/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 流水线数据层（WP09-T8）：接口封装 + SSE 订阅（供 WP15 看板使用）。
 *
 * 契约要点：
 * - 事件名与载荷严格对齐 `contracts.sse_events`（`PIPELINE_EVENT_NAMES`）
 * - SSE 只负责实时展示；**断线后必须用 `getPipelineStatus` + `getPipelineStages` 补齐状态**
 *   （`onOpen` 回调会在每次（重）连成功时触发，直接在那里拉状态即可）
 * - 写操作自动带 `X-Owner-Token`（由 `api/client.ts` 注入）
 * - 禁止 WebSocket：只用 EventSource
 */

import { get, post, request, streamUrl, type Paginated } from './client'

/** 项目状态（contracts.enums.project_status） */
export type ProjectStatus =
  | 'DRAFT'
  | 'TASKBOOK_LOCKED'
  | 'RUNNING'
  | 'RISK_EVALUATING'
  | 'WAIT_HUMAN'
  | 'CIRCUIT_BREAK'
  | 'REVIEWING'
  | 'DONE'
  | 'ABORTED'

/** 六环节（contracts.enums.pipeline_stage，顺序固定） */
export type PipelineStage = 'survey' | 'plan' | 'plan_review' | 'experiment' | 'writing' | 'review'

/** 环节状态（contracts.enums.stage_status） */
export type StageStatus = 'pending' | 'running' | 'waiting_human' | 'done' | 'failed'

/** 停止原因（contracts.enums.stop_reason） */
export type StopReason = 'score_threshold' | 'marginal_stagnation' | 'max_iterations' | 'manual'

export type PipelineMode = 'auto' | 'manual'

export type PolicyAction = 'auto_execute' | 'need_human' | 'circuit_break'

export type InterventionNode = 'N1' | 'N2' | 'N3' | 'N4'

export type InterventionAction =
  | 'approve'
  | 'modify'
  | 'reject'
  | 'rerun'
  | 'downgrade'
  | 'abort'
  | 'switch_mode'

export interface PipelineRunInfo {
  id: number
  project_id: number
  iteration: number
  mode: PipelineMode
  status: 'running' | 'paused' | 'failed' | 'circuit_break' | 'done'
  started_at: string | null
  finished_at: string | null
  total_cost_usd: number
  stop_reason: StopReason | null
  created_at: string | null
}

export interface StageProgress {
  stage: PipelineStage
  status: StageStatus
  attempt: number
}

export interface StageDetail extends StageProgress {
  stage_output_id?: number
  cost_usd: number
  duration_ms: number | null
  error: string | null
  has_output: boolean
  output_json: Record<string, unknown> | null
  started_at?: string | null
  finished_at?: string | null
  updated_at?: string | null
}

export interface StopConditions {
  max_iterations: number
  score_threshold: number
  marginal_gain_threshold: number
  score_threshold_source: string
  max_iterations_source: string
  marginal_gain_threshold_source: string
  is_demo: boolean
}

export interface CostSnapshot {
  used_usd?: number
  limit_usd?: number
  quota_usd?: number
  quota_exceeded?: boolean
  limit_exceeded?: boolean
  breakdown_by_stage?: Record<string, number>
  replay_saved_usd?: number
  cost_complete?: boolean
  [key: string]: unknown
}

export interface RiskPolicyState {
  mounted: boolean
  source: string
  policy_version?: string | null
  degrade_reason: string | null
  note: string | null
}

export interface PendingDecision {
  stage: PipelineStage
  attempt: number
  decision_point: string | null
  failure: Record<string, unknown> | null
  error: string | null
  options: string[]
}

export interface ResumePlanItem {
  stage: PipelineStage
  attempt: number
  status: StageStatus
  reusable: boolean
  revert_reason: string | null
  attempts_seen: number[]
  has_output: boolean
}

export interface StageRegistryInfo {
  stage_order: PipelineStage[]
  registered: PipelineStage[]
  missing: PipelineStage[]
  decision_points: Record<string, string | null>
  intervention_nodes: Record<string, string | null>
}

export interface PipelineStatus {
  project_id: number
  project_name: string
  project_status: ProjectStatus
  mode: PipelineMode
  current_iteration: number
  is_demo: boolean
  taskbook_id: number | null
  run: PipelineRunInfo | null
  stop_reason: StopReason | null
  run_cost_usd: number
  current_stage: { stage: PipelineStage; status: StageStatus; attempt: number } | null
  stages_progress: StageProgress[]
  resume_plan: ResumePlanItem[]
  next_stage: PipelineStage | null
  stop_conditions: StopConditions
  cost: CostSnapshot
  risk_policy: RiskPolicyState
  stage_registry: StageRegistryInfo
  in_process_task: boolean
  pending_decision: PendingDecision | null
  updated_at: string
}

export interface PipelineStagesResponse {
  project_id: number
  project_status: ProjectStatus
  pipeline_run_id: number | null
  stop_reason: StopReason | null
  stages: StageDetail[]
  resume_plan: ResumePlanItem[]
  next_stage: PipelineStage | null
  stage_registry: StageRegistryInfo
  note: string
}

export interface DecisionLogItem {
  id: number
  project_id: number
  pipeline_run_id: number | null
  decision_point: string
  stage: string | null
  context_digest: string
  options_considered: string[]
  chosen: string
  rationale: string
  risk_score: number | null
  confidence_score: number | null
  reversibility_score: number | null
  policy_action: PolicyAction
  policy_version: string
  guardrail_checks: Record<string, unknown>
  cost_usd: number
  created_at: string | null
}

export interface InterventionItem {
  id: number
  project_id: number
  pipeline_run_id: number | null
  node: InterventionNode
  action: InterventionAction
  payload: Record<string, unknown> | null
  note: string | null
  created_at: string | null
}

export interface FailureReport {
  report_version: string
  generated_at: string
  project_id: number
  project_name?: string
  project_status: ProjectStatus
  pipeline_run_id: number | null
  iteration: number | null
  run_status: string | null
  stop_reason: StopReason | null
  level: 'L1' | 'L2' | 'L3'
  failed_stage: PipelineStage | null
  failure_chain: Array<Record<string, unknown>>
  handled: Array<Record<string, unknown>>
  cost: Record<string, unknown>
  decision_log_ids: number[]
  suggested_human_actions: string[]
  report_url: string
  source?: 'snapshot' | 'live'
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * SSE 事件（contracts.sse_events）
 * ------------------------------------------------------------------ */

export interface SSEPayloadMap {
  stage_progress: { stage: PipelineStage; percent: number; message: string }
  stage_done: {
    stage: PipelineStage
    attempt?: number
    verdict?: string
    selected_method_index?: number
    metrics?: Record<string, unknown>
  }
  decision: { decision_point: string | null; chosen: string | null; rationale: string }
  risk_policy: {
    decision_id: number | null
    risk: number | null
    confidence: number | null
    reversibility: number | null
    action: PolicyAction
    policy_version?: string
    degraded?: boolean
    decision_point?: string
    reason?: string | null
  }
  passport_ready: { passport_id: number; status: string; is_replay: boolean }
  review_calibrated: { sample_size: number; metric: string; value: number | null }
  guardrail: {
    type: string
    used_usd: number
    limit_usd: number
    quota_usd: number
    ok: boolean
  }
  need_human: { node: InterventionNode | null; payload: Record<string, unknown>; node_defined?: boolean }
  circuit_break: { reason: string; report_url: string }
  demo_mode: { snapshot: boolean; replay: boolean }
  /** WP02 事件总线转发的附加事件（超出 contracts，仅作审计展示） */
  llm_fallback: Record<string, unknown>
  llm_error: Record<string, unknown>
  replay_miss: Record<string, unknown>
}

export type PipelineEventName = keyof SSEPayloadMap

export const PIPELINE_EVENT_NAMES: PipelineEventName[] = [
  'stage_progress',
  'stage_done',
  'decision',
  'risk_policy',
  'passport_ready',
  'review_calibrated',
  'guardrail',
  'need_human',
  'circuit_break',
  'demo_mode',
  'llm_fallback',
  'llm_error',
  'replay_miss',
]

export interface PipelineEvent<K extends PipelineEventName = PipelineEventName> {
  event: K
  payload: SSEPayloadMap[K]
  id: number
  created_at: string
}

export interface SubscribeOptions {
  /** 每次（重）连成功时触发：请在此拉取 status/stages 补齐状态 */
  onOpen?: () => void
  onError?: (error: Event) => void
  /** 兜底轮询间隔毫秒（EventSource 彻底失败时启用，默认 0 = 不启用） */
  fallbackPollMs?: number
  /** 单事件订阅 */
  onEvent?: <K extends PipelineEventName>(name: K, payload: SSEPayloadMap[K]) => void
}

/* ------------------------------------------------------------------ *
 * 接口封装
 * ------------------------------------------------------------------ */

/** 创建流水线（Owner） */
export function createPipeline(projectId: number, mode?: PipelineMode) {
  return post<{
    pipeline_run_id: number
    project_id: number
    iteration: number
    mode: PipelineMode
    status: string
    started: boolean
    note: string
  }>('/pipelines', { body: { project_id: projectId, mode } })
}

/** 启动 / 断点续跑（Owner） */
export function runPipeline(projectId: number, mode?: PipelineMode, resume = false) {
  return post<{
    project: Record<string, unknown>
    run: PipelineRunInfo
    mode: PipelineMode
    status: string
    task_id: string
    note: string
    registry?: StageRegistryInfo
  }>(`/pipelines/${projectId}/run`, { query: { mode, resume: resume ? true : undefined } })
}

export function pausePipeline(projectId: number) {
  return post<{ status: string; note: string; run: PipelineRunInfo | null }>(
    `/pipelines/${projectId}/pause`,
  )
}

export function resumePipeline(projectId: number) {
  return post<{ next_stage: PipelineStage | null; resume_plan: ResumePlanItem[]; note?: string }>(
    `/pipelines/${projectId}/resume`,
  )
}

export function stopPipeline(projectId: number, reason?: string) {
  return post<{ project_status: ProjectStatus; stop_reason: StopReason; note: string }>(
    `/pipelines/${projectId}/stop`,
    { query: { reason } },
  )
}

export function switchPipelineMode(projectId: number, mode: PipelineMode) {
  return post<{ previous_mode: PipelineMode; mode: PipelineMode; note: string }>(
    `/pipelines/${projectId}/switch-mode`,
    { body: { mode } },
  )
}

/** 人工介入（N1–N4）；auto_resume 默认 true，approve/rerun 等动作后自动续跑 */
export function intervene(
  projectId: number,
  node: InterventionNode,
  action: InterventionAction,
  payload: Record<string, unknown> = {},
  options: { note?: string; autoResume?: boolean } = {},
) {
  return post<{
    intervention_id: number
    project_status: ProjectStatus
    effect: Record<string, unknown>
    ready_to_resume: boolean
    resumed?: unknown
    note?: string
  }>(`/pipelines/${projectId}/intervene`, {
    body: { node, action, payload, note: options.note },
    query: { auto_resume: options.autoResume === false ? false : true },
  })
}

export function getPipelineStatus(projectId: number) {
  return get<PipelineStatus>(`/pipelines/${projectId}/status`)
}

export function getPipelineStages(projectId: number) {
  return get<PipelineStagesResponse>(`/pipelines/${projectId}/stages`)
}

export function getDecisionLogs(
  projectId: number,
  query: { page?: number; page_size?: number; decision_point?: string } = {},
) {
  return get<Paginated<DecisionLogItem> & { note?: string }>(
    `/pipelines/${projectId}/decision-logs`,
    { query: { page: query.page, page_size: query.page_size, decision_point: query.decision_point } },
  )
}

export function getInterventions(projectId: number, query: { page?: number; page_size?: number } = {}) {
  return get<Paginated<InterventionItem> & { mapping: Record<string, string> }>(
    `/pipelines/${projectId}/interventions`,
    { query: { page: query.page, page_size: query.page_size } },
  )
}

export function listPipelineRuns(
  query: { project_id?: number; page?: number; page_size?: number; status?: string } = {},
) {
  return get<Paginated<PipelineRunInfo> & { orphan_run?: boolean; note?: string }>('/runs', {
    query: { project_id: query.project_id, page: query.page, page_size: query.page_size, status: query.status },
  })
}

export function getRunDetail(runId: number) {
  return get<{ run: PipelineRunInfo; stage_attempts: StageDetail[]; note: string }>(`/runs/${runId}`)
}

/** 《失败分析报告》（source: auto 优先快照，live 强制实况） */
export function getFailureReport(projectId: number, source: 'auto' | 'snapshot' | 'live' = 'auto') {
  return get<FailureReport>(`/reports/${projectId}/failure`, { query: { source } })
}

export function getRecentEvents(projectId: number, limit = 50) {
  return get<{ items: Array<PipelineEvent>; subscribers: number; note: string }>(
    `/stream/${projectId}/recent`,
    { query: { limit } },
  )
}

/* ------------------------------------------------------------------ *
 * SSE 订阅
 * ------------------------------------------------------------------ */

export interface PipelineSubscription {
  close: () => void
  /** 当前连接状态（供 UI 展示） */
  readyState: () => number
}

/**
 * 订阅流水线事件流。
 *
 * - 内建指数退避重连（EventSource 自动重连之外，针对 4xx/服务重启的兜底）
 * - 浏览器会自动带 `Last-Event-ID`，服务端据此补发缺失事件
 * - `onOpen` 每次（重）连成功都会触发 → 在这里拉 `getPipelineStatus` 补齐状态
 * - 返回 `close()` 释放连接（组件卸载必须调用）
 */
export function subscribePipelineEvents(
  projectId: number,
  handlers: Partial<{ [K in PipelineEventName]: (payload: SSEPayloadMap[K]) => void }> &
    SubscribeOptions = {},
): PipelineSubscription {
  let source: EventSource | null = null
  let closed = false
  let attempt = 0
  let retryTimer: number | undefined
  let pollTimer: number | undefined

  const open = () => {
    if (closed) return
    source = new EventSource(streamUrl(projectId), { withCredentials: true })

    source.onopen = () => {
      attempt = 0
      handlers.onOpen?.()
    }

    for (const name of PIPELINE_EVENT_NAMES) {
      const handler = handlers[name] as ((payload: unknown) => void) | undefined
      source.addEventListener(name, (raw: Event) => {
        const message = raw as MessageEvent<string>
        let payload: unknown = {}
        try {
          const parsed = JSON.parse(message.data) as { payload?: unknown }
          payload = parsed?.payload ?? parsed
        } catch {
          payload = { raw: message.data }
        }
        handler?.(payload)
        ;(handlers.onEvent as ((name: PipelineEventName, payload: unknown) => void) | undefined)?.(
          name,
          payload,
        )
      })
    }

    source.onerror = (error: Event) => {
      handlers.onError?.(error)
      // readyState=CLOSED 表示浏览器不会再自动重连（如 4xx / 服务重启），需要手动重建
      if (source?.readyState === EventSource.CLOSED) {
        source.close()
        source = null
        scheduleReconnect()
      }
    }
  }

  const scheduleReconnect = () => {
    if (closed) return
    attempt += 1
    const delay = Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5))
    retryTimer = window.setTimeout(open, delay)
    if (handlers.fallbackPollMs && handlers.fallbackPollMs > 0) {
      pollTimer = window.setTimeout(() => handlers.onOpen?.(), delay)
    }
  }

  open()

  return {
    close: () => {
      closed = true
      if (retryTimer) window.clearTimeout(retryTimer)
      if (pollTimer) window.clearTimeout(pollTimer)
      source?.close()
      source = null
    },
    readyState: () => (source ? source.readyState : EventSource.CLOSED),
  }
}

/** 便捷：把事件写入 console（A7 的「控制台可观察事件流」验收） */
export function logPipelineEvents(projectId: number): PipelineSubscription {
  return subscribePipelineEvents(projectId, {
    onOpen: () => console.info(`[pipeline] SSE connected project_id=${projectId}`),
    onError: (error) => console.warn('[pipeline] SSE error', error),
    onEvent: (name, payload) => console.info(`[pipeline] ${name}`, payload),
  })
}

export default {
  PIPELINE_EVENT_NAMES,
  createPipeline,
  runPipeline,
  pausePipeline,
  resumePipeline,
  stopPipeline,
  switchPipelineMode,
  intervene,
  getPipelineStatus,
  getPipelineStages,
  getDecisionLogs,
  getInterventions,
  listPipelineRuns,
  getRunDetail,
  getFailureReport,
  getRecentEvents,
  subscribePipelineEvents,
  logPipelineEvents,
  request,
}
