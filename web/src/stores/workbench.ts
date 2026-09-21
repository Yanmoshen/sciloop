/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 工作台状态（WP15-T8）。
 *
 * 设计要点：
 * - **SSE 只做实时展示，状态以接口为准**：`onOpen`（每次（重）连成功）重新拉取
 *   `status` + `stages`（+ decisions/interventions），补齐断线期间的状态；
 *   刷新页面后仅凭 projectId 即可从后端恢复全部状态，不依赖内存。
 * - 事件分发集中在此处，组件只读 store、只调 action，**组件内不出现 fetch**。
 * - 未挂载接口的面板（Passport / 草稿 / 证据）保留 `PanelResult`，UI 据此显示
 *   「等待 WPxx 交付」空状态，禁止伪造数据。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import {
  approveDecision,
  evaluateDecisionRisk,
  getCostSummary,
  getDecisionOverview,
  getDemoStatus,
  getDraft,
  getDraftClaims,
  getEvidence,
  getInterventions,
  getOwnerSession,
  getPipelineStages,
  getPipelineStatus,
  getReviewCalibration,
  getRunPassport,
  intervene,
  listPipelineRuns,
  listWorkbenchProjects,
  pausePipeline,
  replayPassport,
  rerunPassport,
  resumePipeline,
  runPipeline,
  stopPipeline,
  submitHumanLabels,
  switchPipelineMode,
  subscribePipelineEvents,
  type CalibrationReport,
  type CostSummary,
  type DecisionOverview,
  type DecisionRecord,
  type DemoStatus,
  type DraftClaim,
  type DraftRecord,
  type EvaluateRiskResponse,
  type EvidenceRecord,
  type EvidenceType,
  type HumanLabelEntry,
  type InterventionAction,
  type InterventionItem,
  type InterventionNode,
  type PanelResult,
  type PassportDiffReport,
  type PassportRecord,
  type PipelineEventName,
  type PipelineMode,
  type PipelineRunInfo,
  type PipelineStagesResponse,
  type PipelineStatus,
  type PipelineSubscription,
  type PolicyAction,
  type SSEPayloadMap,
  type StopReason,
  type WorkbenchProject,
} from '@/api/workbench'

/** 事件日志条目（右侧实时日志） */
export interface WorkbenchEventEntry {
  seq: number
  name: PipelineEventName
  payload: unknown
  at: string
}

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'error'

const MAX_EVENT_LOG = 200

function message(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.isForbidden) return `public_demo 面禁止该写操作（HTTP ${error.status} ${error.code}）`
    if (error.isDbUnavailable) return `数据库不可用：${error.message}`
    return error.message
  }
  return error instanceof Error ? error.message : String(error)
}

export const useWorkbenchStore = defineStore('workbench', () => {
  /* ---------------- Project ---------------- */
  const projectId = ref<number | null>(null)
  const projects = ref<WorkbenchProject[]>([])
  const projectsSource = ref('')
  const projectsNote = ref<string | null>(null)

  /* ---------------- 快照状态 ---------------- */
  const status = ref<PipelineStatus | null>(null)
  const stagesResponse = ref<PipelineStagesResponse | null>(null)
  const decisions = ref<DecisionOverview | null>(null)
  const interventions = ref<{ items: InterventionItem[]; total: number; mapping: Record<string, string> }>({
    items: [],
    total: 0,
    mapping: {},
  })
  const runs = ref<PipelineRunInfo[]>([])
  const costSummary = ref<CostSummary | null>(null)
  const calibration = ref<CalibrationReport | null>(null)
  const demoStatus = ref<DemoStatus | null>(null)
  const ownerWritesAllowed = ref(false)

  /* ---------------- 未挂载接口的面板 ---------------- */
  const passport = ref<PanelResult<PassportRecord> | null>(null)
  const passportRunId = ref<number | null>(null)
  const passportActionNote = ref('')
  const passportActionResult = ref<{
    kind: 'replay' | 'rerun'
    passport: PassportRecord | null
    diff: PassportDiffReport | null
    at: string
  } | null>(null)
  const draft = ref<PanelResult<DraftRecord> | null>(null)
  const draftClaims = ref<DraftClaim[]>([])
  const draftLoading = ref(false)
  const evidence = ref<PanelResult<EvidenceRecord> | null>(null)
  const evidenceLoading = ref(false)

  /* ---------------- 交互状态 ---------------- */
  const loading = ref(false)
  const busy = ref('')
  const errorMessage = ref('')
  /** 真实错误码（来自 `ApiError.code`）；未取到时为 null，界面显示 unknown_error 而不是编造代号 */
  const errorCode = ref<string | null>(null)
  /** 真实 HTTP 状态码：用于如实区分「权限拒绝 401/403」与一般失败 */
  const errorStatus = ref<number | null>(null)
  const noticeMessage = ref('')
  /** 非阻塞的降级说明（如成本接口不可用）：不冒充成功，也不把整页判为失败 */
  const costNote = ref<string | null>(null)
  const connection = ref<ConnectionState>('idle')
  const lastSyncedAt = ref<string | null>(null)
  const eventLog = ref<WorkbenchEventEntry[]>([])
  const riskEvents = ref<Array<SSEPayloadMap['risk_policy'] & { at: string }>>([])
  const circuitBreak = ref<{ reason: string; report_url: string; at: string } | null>(null)
  const lastEvaluate = ref<EvaluateRiskResponse | null>(null)

  let subscription: PipelineSubscription | null = null
  let seq = 0

  /**
   * 统一登记失败：message + **真实** `code` / `status` 一并落 ref。
   * 视图据此展示「权限拒绝（403）」而不是靠对文案做 `includes('403')` 之类的猜测，
   * 也不再需要在前端硬编码一个并不存在的错误码。
   */
  function reportError(error: unknown): void {
    errorMessage.value = message(error)
    if (error instanceof ApiError) {
      errorCode.value = error.code
      errorStatus.value = error.status
    } else {
      errorCode.value = 'unknown_error'
      errorStatus.value = null
    }
  }

  function clearError(): void {
    errorMessage.value = ''
    errorCode.value = null
    errorStatus.value = null
  }

  /* ---------------- 派生 ---------------- */
  const project = computed(
    () => projects.value.find((p) => p.id === projectId.value) ?? null,
  )
  const projectName = computed(() => status.value?.project_name ?? project.value?.name ?? `#${projectId.value ?? '-'}`)
  const stages = computed(() => stagesResponse.value?.stages ?? [])
  const stopReason = computed<StopReason | null>(
    () => stagesResponse.value?.stop_reason ?? status.value?.stop_reason ?? status.value?.run?.stop_reason ?? null,
  )
  const iteration = computed(() => status.value?.current_iteration ?? status.value?.run?.iteration ?? 0)
  const pendingDecision = computed(() => status.value?.pending_decision ?? null)
  const isWaitingHuman = computed(
    () => status.value?.project_status === 'WAIT_HUMAN' || Boolean(pendingDecision.value),
  )
  const isRunning = computed(() => status.value?.project_status === 'RUNNING' || status.value?.in_process_task === true)
  const isDemoMode = computed(
    () => demoStatus.value?.snapshot === true || demoStatus.value?.replay === true,
  )

  /** 成本视图：优先项目级 /costs/summary，缺失时回退 status.cost（两者都含双线阈值） */
  const cost = computed<CostSummary | null>(() => {
    const snapshot = status.value?.cost as CostSummary | undefined
    const summary = costSummary.value
    if (summary && typeof summary.limit_usd === 'number') {
      return {
        ...(snapshot ?? {}),
        ...summary,
        // 项目级 summary 若未返回分项，沿用快照里的分项
        breakdown_by_stage: summary.breakdown_by_stage ?? snapshot?.breakdown_by_stage ?? {},
      } as CostSummary
    }
    if (snapshot && typeof snapshot.limit_usd === 'number') return snapshot
    return null
  })

  const decisionGroups = computed(() => {
    const grouped: Record<string, DecisionRecord[]> = { D1: [], D2: [], D3: [], D4: [], D5: [], D6: [] }
    for (const item of decisions.value?.items ?? []) {
      const key = String(item.decision_point)
      if (!grouped[key]) grouped[key] = []
      grouped[key].push(item)
    }
    return grouped
  })

  /** 风险策略三态在真库中的实际命中分布（用于面板可视化） */
  const policyActionCounts = computed<Record<PolicyAction, number>>(() => {
    const counts: Record<PolicyAction, number> = { auto_execute: 0, need_human: 0, circuit_break: 0 }
    for (const item of decisions.value?.items ?? []) {
      if (item.policy_action in counts) counts[item.policy_action] += 1
    }
    return counts
  })

  const totalCostUsd = computed(() => cost.value?.used_usd ?? status.value?.run_cost_usd ?? 0)

  /* ---------------- 工具 ---------------- */
  function setProject(next: number | null): void {
    if (next === projectId.value) return
    projectId.value = next
    resetProjectState()
  }

  function resetProjectState(): void {
    status.value = null
    stagesResponse.value = null
    decisions.value = null
    interventions.value = { items: [], total: 0, mapping: {} }
    runs.value = []
    costSummary.value = null
    calibration.value = null
    passport.value = null
    passportActionNote.value = ''
    passportActionResult.value = null
    draft.value = null
    draftClaims.value = []
    evidence.value = null
    eventLog.value = []
    riskEvents.value = []
    circuitBreak.value = null
    lastEvaluate.value = null
    clearError()
    noticeMessage.value = ''
    lastSyncedAt.value = null
  }

  function pushEvent(name: PipelineEventName, payload: unknown): void {
    eventLog.value = [
      { seq: ++seq, name, payload, at: new Date().toISOString() },
      ...eventLog.value,
    ].slice(0, MAX_EVENT_LOG)
  }

  /* ---------------- 快照拉取 ---------------- */
  async function refreshProjects(): Promise<void> {
    try {
      const result = await listWorkbenchProjects()
      projects.value = result.items
      projectsSource.value = result.source
      projectsNote.value = result.fallbackNote
      if (projectId.value === null && result.items.length > 0) {
        setProject(result.items[0]?.id ?? null)
      }
    } catch (error) {
      projects.value = []
      reportError(error)
    }
  }

  async function refreshStatus(): Promise<void> {
    if (projectId.value === null) return
    try {
      status.value = await getPipelineStatus(projectId.value)
    } catch (error) {
      reportError(error)
    }
  }

  async function refreshStages(): Promise<void> {
    if (projectId.value === null) return
    try {
      stagesResponse.value = await getPipelineStages(projectId.value)
    } catch (error) {
      reportError(error)
    }
  }

  async function refreshDecisions(): Promise<void> {
    if (projectId.value === null) return
    try {
      decisions.value = await getDecisionOverview(projectId.value)
    } catch (error) {
      reportError(error)
    }
  }

  async function refreshInterventions(): Promise<void> {
    if (projectId.value === null) return
    try {
      const data = await getInterventions(projectId.value, { page: 1, page_size: 100 })
      interventions.value = { items: data.items ?? [], total: data.total ?? 0, mapping: data.mapping ?? {} }
    } catch (error) {
      reportError(error)
    }
  }

  async function refreshRuns(): Promise<void> {
    if (projectId.value === null) return
    try {
      const data = await listPipelineRuns({ project_id: projectId.value, page: 1, page_size: 50 })
      runs.value = data.items ?? []
      if (passportRunId.value === null && runs.value.length > 0) {
        passportRunId.value = runs.value[0]?.id ?? null
      }
    } catch (error) {
      reportError(error)
    }
  }

  async function refreshCost(): Promise<void> {
    if (projectId.value === null) return
    try {
      costSummary.value = await getCostSummary(projectId.value)
      costNote.value = null
    } catch (error) {
      // 成本接口不可用不阻塞看板：status.cost 仍含双线阈值。
      // 这是**降级**而非整页失败，因此只记 degraded 说明，不写 errorMessage
      // （否则成本端点一次 4xx 会让看板误报「流水线同步失败」）。
      costSummary.value = null
      const detail = error instanceof ApiError ? `${error.code}：${message(error)}` : message(error)
      costNote.value = `成本接口（GET /costs/summary）不可用：${detail}。已回退 status.cost 的双线阈值，分项与已用金额可能缺失。`
    }
  }

  async function refreshCalibration(): Promise<void> {
    if (projectId.value === null) return
    try {
      calibration.value = await getReviewCalibration(projectId.value)
    } catch (error) {
      reportError(error)
    }
  }

  async function refreshDemo(): Promise<void> {
    try {
      demoStatus.value = await getDemoStatus()
      const session = await getOwnerSession()
      ownerWritesAllowed.value = session.permissions?.writes_allowed === true
    } catch {
      // 演示/访问面接口不可用时保持静默：不阻塞工作台
      demoStatus.value = null
    }
  }

  /** 一次拉齐所有快照（刷新页面 / 切换 Project / SSE 重连后的兜底） */
  async function refreshAll(reason = 'manual'): Promise<void> {
    if (projectId.value === null) return
    loading.value = true
    clearError()
    try {
      await Promise.all([
        refreshStatus(),
        refreshStages(),
        refreshDecisions(),
        refreshInterventions(),
        refreshRuns(),
        refreshCost(),
        refreshCalibration(),
        refreshDemo(),
      ])
      lastSyncedAt.value = new Date().toISOString()
      if (reason !== 'silent') noticeMessage.value = ''
    } finally {
      loading.value = false
    }
  }

  /**
   * SSE `onOpen` 兜底（WP09 明确要求）：重连成功后重新拉取 status + stages，
   * 补齐断线期间错过的状态；事件流本身只用于实时展示。
   */
  async function syncSnapshotFromOpen(): Promise<void> {
    await Promise.all([refreshStatus(), refreshStages()])
    lastSyncedAt.value = new Date().toISOString()
  }

  /* ---------------- SSE ---------------- */
  async function handleEvent<K extends PipelineEventName>(name: K, payload: SSEPayloadMap[K]): Promise<void> {
    pushEvent(name, payload)
    switch (name) {
      case 'stage_progress':
      case 'stage_done':
        await refreshStages()
        break
      case 'decision':
        await Promise.all([refreshDecisions(), refreshStatus()])
        break
      case 'risk_policy': {
        const risk = payload as SSEPayloadMap['risk_policy']
        riskEvents.value = [{ ...risk, at: new Date().toISOString() }, ...riskEvents.value].slice(0, 50)
        await refreshDecisions()
        break
      }
      case 'guardrail':
        await refreshCost()
        break
      case 'need_human':
        await Promise.all([refreshStatus(), refreshInterventions()])
        break
      case 'circuit_break': {
        const cb = payload as SSEPayloadMap['circuit_break']
        circuitBreak.value = { reason: cb.reason, report_url: cb.report_url, at: new Date().toISOString() }
        await Promise.all([refreshStatus(), refreshStages()])
        break
      }
      case 'passport_ready':
        if (passportRunId.value !== null) await loadPassport(passportRunId.value)
        break
      case 'review_calibrated':
        await refreshCalibration()
        break
      case 'demo_mode':
        await refreshDemo()
        break
      default:
        break
    }
  }

  function stopStream(): void {
    subscription?.close()
    subscription = null
    connection.value = 'idle'
  }

  function startStream(): void {
    stopStream()
    if (projectId.value === null) return
    const pid = projectId.value
    connection.value = 'connecting'
    subscription = subscribePipelineEvents(pid, {
      onOpen: () => {
        connection.value = 'open'
        void syncSnapshotFromOpen()
      },
      onError: () => {
        connection.value = subscription ? 'reconnecting' : 'error'
      },
      onEvent: (name, payload) => {
        void handleEvent(name, payload)
      },
      fallbackPollMs: 15_000,
    })
  }

  /* ---------------- 运行控制（写操作，Owner） ---------------- */
  async function withBusy<T>(label: string, fn: () => Promise<T>, okNotice?: string): Promise<T | null> {
    busy.value = label
    clearError()
    noticeMessage.value = ''
    try {
      const result = await fn()
      if (okNotice) noticeMessage.value = okNotice
      await refreshAll('silent')
      return result
    } catch (error) {
      reportError(error)
      return null
    } finally {
      busy.value = ''
    }
  }

  const startRun = (mode?: PipelineMode, resume = false) =>
    projectId.value === null
      ? Promise.resolve(null)
      : withBusy('start', () => runPipeline(projectId.value as number, mode, resume), '已触发启动')

  const pauseRun = () =>
    projectId.value === null
      ? Promise.resolve(null)
      : withBusy('pause', () => pausePipeline(projectId.value as number), '已暂停')

  const resumeRun = () =>
    projectId.value === null
      ? Promise.resolve(null)
      : withBusy('resume', () => resumePipeline(projectId.value as number), '已继续')

  const stopRun = (reason = 'manual') =>
    projectId.value === null
      ? Promise.resolve(null)
      : withBusy('stop', () => stopPipeline(projectId.value as number, reason), '已中止')

  const switchMode = (mode: PipelineMode) =>
    projectId.value === null
      ? Promise.resolve(null)
      : withBusy('switch-mode', () => switchPipelineMode(projectId.value as number, mode), `已切换到 ${mode}`)

  /** 人工介入（N1–N4） */
  const interveneNode = (
    node: InterventionNode,
    action: InterventionAction,
    payload: Record<string, unknown> = {},
    options: { note?: string; autoResume?: boolean } = {},
  ) =>
    projectId.value === null
      ? Promise.resolve(null)
      : withBusy(
          `intervene-${node}`,
          () => intervene(projectId.value as number, node, action, payload, options),
          `${node} ${action} 已提交`,
        )

  /** 批准/处置待决策（A3：need_human → 批准 → 继续流水线） */
  const resolvePendingDecision = (
    decisionId: number,
    action: InterventionAction,
    options: { node?: InterventionNode | null; note?: string; resume?: boolean } = {},
  ) =>
    withBusy(
      'approve',
      () =>
        approveDecision(decisionId, {
          action,
          node: options.node ?? null,
          note: options.note,
          resume: options.resume !== false,
        }),
      `决策 #${decisionId} 已 ${action}${options.resume !== false ? '，已请求续跑' : ''}`,
    )

  const recomputeRisk = (decisionId: number, persist = false) =>
    withBusy('evaluate-risk', async () => {
      const result = await evaluateDecisionRisk(decisionId, { persist })
      lastEvaluate.value = result
      return result
    })

  /* ---------------- 人工标签（WP12） ---------------- */
  async function submitLabels(labels: HumanLabelEntry[], replace = false): Promise<boolean> {
    if (projectId.value === null) return false
    const result = await withBusy(
      'human-labels',
      () => submitHumanLabels(projectId.value as number, labels, { replace }),
      `已录入 ${labels.length} 条人工标签`,
    )
    if (result) calibration.value = result
    return Boolean(result)
  }

  /* ---------------- Passport（WP11） ---------------- */
  async function loadPassport(runId: number): Promise<void> {
    passportRunId.value = runId
    try {
      passport.value = await getRunPassport(runId)
    } catch (error) {
      reportError(error)
    }
  }

  async function doReplay(passportId: number): Promise<void> {
    passportActionNote.value = ''
    busy.value = 'replay'
    clearError()
    try {
      const result = await replayPassport(passportId)
      passportActionNote.value = result?.note ?? '回放已完成（is_replay=true）'
      passportActionResult.value = {
        kind: 'replay',
        passport: result?.passport ?? null,
        diff: result?.diff ?? null,
        at: new Date().toISOString(),
      }
      if (passportRunId.value !== null) await loadPassport(passportRunId.value)
    } catch (error) {
      reportError(error)
    } finally {
      busy.value = ''
    }
  }

  async function doRerun(passportId: number): Promise<void> {
    passportActionNote.value = ''
    busy.value = 'rerun'
    clearError()
    try {
      const result = await rerunPassport(passportId)
      passportActionNote.value = result?.note ?? '重跑已完成'
      passportActionResult.value = {
        kind: 'rerun',
        passport: result?.passport ?? null,
        diff: result?.diff ?? null,
        at: new Date().toISOString(),
      }
      if (passportRunId.value !== null) await loadPassport(passportRunId.value)
    } catch (error) {
      reportError(error)
    } finally {
      busy.value = ''
    }
  }

  /* ---------------- 草稿与证据（WP14 / WP13） ---------------- */
  async function loadDraft(draftId: number): Promise<void> {
    draftLoading.value = true
    try {
      draft.value = await getDraft(draftId)
      const claims = await getDraftClaims(draftId)
      draftClaims.value = claims.available
        ? Array.isArray(claims.data)
          ? claims.data
          : (claims.data.items ?? [])
        : []
    } catch (error) {
      reportError(error)
    } finally {
      draftLoading.value = false
    }
  }

  async function loadEvidence(evidenceType: EvidenceType | string, evidenceId: number | string): Promise<void> {
    evidenceLoading.value = true
    try {
      evidence.value = await getEvidence(evidenceType, evidenceId)
    } catch (error) {
      reportError(error)
    } finally {
      evidenceLoading.value = false
    }
  }

  function clearEvidence(): void {
    evidence.value = null
  }

  function clearNotice(): void {
    noticeMessage.value = ''
    clearError()
  }

  return {
    // project
    projectId,
    project,
    projectName,
    projects,
    projectsSource,
    projectsNote,
    setProject,
    refreshProjects,
    // snapshot
    status,
    stages,
    stagesResponse,
    decisions,
    decisionGroups,
    interventions,
    runs,
    cost,
    costSummary,
    calibration,
    demoStatus,
    ownerWritesAllowed,
    stopReason,
    iteration,
    pendingDecision,
    isWaitingHuman,
    isRunning,
    isDemoMode,
    policyActionCounts,
    totalCostUsd,
    // unmounted panels
    passport,
    passportRunId,
    passportActionNote,
    passportActionResult,
    draft,
    draftClaims,
    draftLoading,
    evidence,
    evidenceLoading,
    // ui
    loading,
    busy,
    errorMessage,
    errorCode,
    errorStatus,
    costNote,
    noticeMessage,
    connection,
    lastSyncedAt,
    eventLog,
    riskEvents,
    circuitBreak,
    lastEvaluate,
    // actions
    refreshAll,
    refreshStatus,
    refreshStages,
    refreshDecisions,
    refreshInterventions,
    refreshRuns,
    refreshCost,
    refreshCalibration,
    refreshDemo,
    startStream,
    stopStream,
    startRun,
    pauseRun,
    resumeRun,
    stopRun,
    switchMode,
    interveneNode,
    resolvePendingDecision,
    recomputeRisk,
    submitLabels,
    loadPassport,
    doReplay,
    doRerun,
    loadDraft,
    loadEvidence,
    clearEvidence,
    clearNotice,
  }
})
