/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 构思域状态（WP08-T7）。
 *
 * 职责：把「聚合 → 空白 → idea → 可行性 → 任务书」这条链的状态与请求收拢到一处，
 * 组件只读 store、不直接 fetch（contracts.code_style.frontend）。
 *
 * 诚实展示纪律：
 * - 无 Evidence 的 idea **不允许进入可行性**：`canRunFeasibility` 依据
 *   `idea.has_evidence` 计算，UI 只能引导去绑证据，不能绕过；
 * - ``generation_mode`` / ``llm_error`` / ``discarded`` 原样透传，UI 必须如实标注
 *   「模板合成（未调用 LLM）」与「被丢弃的无证据条目」，不得让模板结果看起来像模型结果。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError } from '@/api/client'
import {
  bindIdeaEvidences,
  createAggregation,
  createFeasibility,
  createManualIdea,
  createTaskbook,
  generateIdeas,
  getAggregation,
  getFeasibility,
  getFeasibilityByIdea,
  getTaskbook,
  listAggregations,
  listIdeas,
  lockTaskbook,
  patchTaskbook,
  selectIdea,
  type Aggregation,
  type EvidenceCandidate,
  type Feasibility,
  type Gap,
  type Idea,
  type IdeaGenerationResult,
  type IdeaMechanism,
  type IdeaMode,
  type Taskbook,
} from '@/api/idea'

/** 长任务统一用「进行中 + 错误明文」两态，禁止静默失败 */
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** 构思域里会各自登记失败的四个环节 */
type ErrorScope = 'aggregation' | 'ideas' | 'feasibility' | 'taskbook'

/** 某个环节最近一次失败的真实元信息（code / HTTP status） */
interface ErrorMeta {
  message: string
  code: string
  status: number | null
}

export const useIdeaStore = defineStore('idea', () => {
  // ---- 聚合 / 空白 ----
  const aggregations = ref<Aggregation[]>([])
  const aggregation = ref<Aggregation | null>(null)
  const gaps = ref<Gap[]>([])
  const aggregationError = ref<string | null>(null)

  // ---- idea ----
  const ideas = ref<Idea[]>([])
  const ideasError = ref<string | null>(null)
  const generation = ref<IdeaGenerationResult | null>(null)
  const selectedIdeaId = ref<number | null>(null)

  // ---- 可行性 / 任务书 ----
  const feasibility = ref<Feasibility | null>(null)
  const feasibilityError = ref<string | null>(null)
  const taskbook = ref<Taskbook | null>(null)
  const taskbookError = ref<string | null>(null)

  // ---- 长任务态 ----
  const busy = ref<string | null>(null)

  /**
   * 按环节登记失败的真实元信息。
   *
   * 为什么要单独存：`aggregationError` 等四个 ref 保持 `string` 形态（向后兼容既有调用点），
   * 但视图需要**真实的** `ApiError.code` / `status` 才能如实区分「权限拒绝 403」与一般失败，
   * 而不是靠对文案做 `includes('403')` 猜测、或在前端硬编码一个后端并不存在的错误码。
   */
  const errorMeta = ref<Partial<Record<ErrorScope, ErrorMeta>>>({})

  /** 登记失败并返回展示用文案（保持 `xxxError.value = reportError(...)` 的既有写法） */
  function reportError(scope: ErrorScope, error: unknown): string {
    const text = messageOf(error)
    errorMeta.value = {
      ...errorMeta.value,
      [scope]: {
        message: text,
        code: error instanceof ApiError ? error.code : 'unknown_error',
        status: error instanceof ApiError ? error.status : null,
      },
    }
    return text
  }

  /**
   * 当前实际生效的错误（与视图的取值优先级一致：ideas → feasibility → taskbook → aggregation）。
   * 以文案做一次精确匹配，从 `errorMeta` 里取回该条错误真实的 code / status。
   */
  const activeErrorInfo = computed<ErrorMeta | null>(() => {
    const message =
      ideasError.value ?? feasibilityError.value ?? taskbookError.value ?? aggregationError.value
    if (!message) return null
    const hit = Object.values(errorMeta.value).find((item) => item?.message === message)
    return hit ?? { message, code: 'unknown_error', status: null }
  })

  /** 供 `ViewStatePanel` 的 `error-code` 使用；无错误时为 null（组件显示 unknown_error） */
  const errorCode = computed(() => activeErrorInfo.value?.code ?? null)
  /** 供视图如实判断「权限拒绝（401/403）」 */
  const errorStatus = computed(() => activeErrorInfo.value?.status ?? null)

  const selectedIdea = computed<Idea | null>(
    () => ideas.value.find((item) => item.id === selectedIdeaId.value) ?? null,
  )
  const ideasWithoutEvidence = computed(() => ideas.value.filter((item) => !item.has_evidence))
  const canRunFeasibility = computed(
    () => selectedIdea.value !== null && selectedIdea.value.has_evidence,
  )
  const totalScore = computed(() => feasibility.value?.total_score ?? null)

  async function loadAggregations(projectId?: number | null): Promise<void> {
    busy.value = 'loadAggregations'
    aggregationError.value = null
    try {
      const payload = await listAggregations({ projectId })
      aggregations.value = payload.items
    } catch (error) {
      aggregationError.value = reportError('aggregation', error)
    } finally {
      busy.value = null
    }
  }

  async function createAggregationForPapers(
    paperIds: number[],
    projectId?: number | null,
  ): Promise<Aggregation | null> {
    busy.value = 'createAggregation'
    aggregationError.value = null
    try {
      const payload = await createAggregation({ paper_ids: paperIds, project_id: projectId })
      aggregation.value = payload
      gaps.value = payload.gaps ?? []
      aggregations.value = [payload, ...aggregations.value]
      return payload
    } catch (error) {
      aggregationError.value = reportError('aggregation', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function loadAggregation(id: number | string): Promise<void> {
    busy.value = 'loadAggregation'
    aggregationError.value = null
    try {
      const payload = await getAggregation(id)
      aggregation.value = payload
      gaps.value = payload.gaps ?? []
    } catch (error) {
      aggregationError.value = reportError('aggregation', error)
    } finally {
      busy.value = null
    }
  }

  async function loadIdeas(params: {
    projectId?: number | null
    aggregationId?: number | null
  } = {}): Promise<void> {
    busy.value = 'loadIdeas'
    ideasError.value = null
    try {
      const payload = await listIdeas({
        projectId: params.projectId,
        aggregationId: params.aggregationId,
        pageSize: 100,
      })
      ideas.value = payload.items
    } catch (error) {
      ideasError.value = reportError('ideas', error)
    } finally {
      busy.value = null
    }
  }

  async function generate(params: {
    aggregationId: number
    count: number
    projectId?: number | null
    mode: IdeaMode
    modelRef?: string | null
  }): Promise<IdeaGenerationResult | null> {
    busy.value = 'generateIdeas'
    ideasError.value = null
    try {
      const payload = await generateIdeas({
        aggregation_id: params.aggregationId,
        count: params.count,
        project_id: params.projectId ?? null,
        mode: params.mode,
        model_ref: params.modelRef ?? null,
      })
      generation.value = payload
      const fresh = payload.items
      const rest = ideas.value.filter((item) => !fresh.some((f) => f.id === item.id))
      ideas.value = [...fresh, ...rest]
      if (!selectedIdeaId.value && fresh.length > 0) selectedIdeaId.value = fresh[0]?.id ?? null
      return payload
    } catch (error) {
      ideasError.value = reportError('ideas', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function addManualIdea(body: {
    title: string
    content: string
    projectId?: number | null
    aggregationId?: number | null
    mechanism?: IdeaMechanism | null
  }): Promise<Idea | null> {
    busy.value = 'createManualIdea'
    ideasError.value = null
    try {
      const payload = await createManualIdea({
        title: body.title,
        content: body.content,
        project_id: body.projectId ?? null,
        aggregation_id: body.aggregationId ?? null,
        mechanism: body.mechanism ?? null,
      })
      ideas.value = [payload.item, ...ideas.value]
      selectedIdeaId.value = payload.item.id
      return payload.item
    } catch (error) {
      ideasError.value = reportError('ideas', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function bindEvidence(
    ideaId: number,
    candidates: EvidenceCandidate[],
  ): Promise<{ boundCount: number; rejected: Array<{ code: string; reason: string }> } | null> {
    busy.value = 'bindEvidence'
    ideasError.value = null
    try {
      const payload = await bindIdeaEvidences(ideaId, { evidences: candidates, replace: false })
      const index = ideas.value.findIndex((item) => item.id === ideaId)
      if (index >= 0) ideas.value[index] = payload.item
      return { boundCount: payload.bound_count, rejected: payload.binding.rejected ?? [] }
    } catch (error) {
      ideasError.value = reportError('ideas', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function select(id: number): Promise<void> {
    selectedIdeaId.value = id
    busy.value = 'selectIdea'
    try {
      const payload = await selectIdea(id)
      const index = ideas.value.findIndex((item) => item.id === id)
      if (index >= 0) ideas.value[index] = payload.item
    } catch (error) {
      ideasError.value = reportError('ideas', error)
    } finally {
      busy.value = null
    }
  }

  async function runFeasibility(params: {
    ideaId: number
    projectId?: number | null
    sampleSize?: number
    useLlm?: boolean
    modelRef?: string | null
  }): Promise<Feasibility | null> {
    busy.value = 'createFeasibility'
    feasibilityError.value = null
    try {
      const payload = await createFeasibility({
        idea_id: params.ideaId,
        project_id: params.projectId ?? null,
        sample_size: params.sampleSize ?? 20,
        use_llm: params.useLlm ?? false,
        model_ref: params.modelRef ?? null,
      })
      feasibility.value = payload
      taskbook.value = null
      return payload
    } catch (error) {
      feasibilityError.value = reportError('feasibility', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function loadFeasibilityForIdea(ideaId: number | string): Promise<void> {
    busy.value = 'loadFeasibility'
    feasibilityError.value = null
    try {
      feasibility.value = await getFeasibilityByIdea(ideaId)
      taskbook.value = null
    } catch (error) {
      feasibility.value = null
      feasibilityError.value = reportError('feasibility', error)
    } finally {
      busy.value = null
    }
  }

  async function loadFeasibility(id: number | string): Promise<void> {
    busy.value = 'loadFeasibility'
    feasibilityError.value = null
    try {
      feasibility.value = await getFeasibility(id)
    } catch (error) {
      feasibilityError.value = reportError('feasibility', error)
    } finally {
      busy.value = null
    }
  }

  async function submitTaskbook(body: {
    projectId: number
    ideaId: number
    researchQuestion: string
    targetDatasets: string[]
    baselines: string[]
    metrics: string[]
    deliverables: string[]
    rounds: Record<string, number>
    computeBudget: Record<string, number>
  }): Promise<Taskbook | null> {
    busy.value = 'createTaskbook'
    taskbookError.value = null
    try {
      const payload = await createTaskbook({
        project_id: body.projectId,
        idea_id: body.ideaId,
        research_question: body.researchQuestion,
        target_datasets: body.targetDatasets,
        baselines: body.baselines,
        metrics: body.metrics,
        deliverables: body.deliverables,
        rounds: body.rounds,
        compute_budget: body.computeBudget,
      })
      taskbook.value = payload.item
      return payload.item
    } catch (error) {
      taskbookError.value = reportError('taskbook', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function saveTaskbook(id: number, patchBody: Record<string, unknown>): Promise<Taskbook | null> {
    busy.value = 'patchTaskbook'
    taskbookError.value = null
    try {
      const payload = await patchTaskbook(id, patchBody)
      taskbook.value = payload.item
      return payload.item
    } catch (error) {
      taskbookError.value = reportError('taskbook', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function lockTaskbookById(id: number): Promise<Taskbook | null> {
    busy.value = 'lockTaskbook'
    taskbookError.value = null
    try {
      const payload = await lockTaskbook(id)
      taskbook.value = payload.item
      return payload.item
    } catch (error) {
      taskbookError.value = reportError('taskbook', error)
      return null
    } finally {
      busy.value = null
    }
  }

  async function loadTaskbook(id: number | string): Promise<void> {
    busy.value = 'loadTaskbook'
    taskbookError.value = null
    try {
      taskbook.value = await getTaskbook(id)
    } catch (error) {
      taskbookError.value = reportError('taskbook', error)
    } finally {
      busy.value = null
    }
  }

  function reset(): void {
    aggregation.value = null
    gaps.value = []
    ideas.value = []
    generation.value = null
    selectedIdeaId.value = null
    feasibility.value = null
    taskbook.value = null
    aggregationError.value = null
    ideasError.value = null
    feasibilityError.value = null
    taskbookError.value = null
  }

  return {
    // state
    aggregations,
    aggregation,
    gaps,
    aggregationError,
    ideas,
    ideasError,
    generation,
    selectedIdeaId,
    feasibility,
    feasibilityError,
    taskbook,
    taskbookError,
    busy,
    // getters
    errorCode,
    errorStatus,
    activeErrorInfo,
    selectedIdea,
    ideasWithoutEvidence,
    canRunFeasibility,
    totalScore,
    // actions
    addManualIdea,
    bindEvidence,
    createAggregationForPapers,
    generate,
    loadAggregation,
    loadAggregations,
    loadFeasibility,
    loadFeasibilityForIdea,
    loadIdeas,
    loadTaskbook,
    lockTaskbookById,
    reset,
    runFeasibility,
    saveTaskbook,
    select,
    submitTaskbook,
  }
})
