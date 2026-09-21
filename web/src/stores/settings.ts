/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 设置页状态：供应商 / 环节路由 / 成本双线 / 数据源健康（WP02）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  ApiError,
  type ConnectivityResult,
  type CostSummary,
  type IsolationStatus,
  type ModelConfig,
  type ModelEntry,
  type RoutingEntry,
  type RoutingStage,
  type SourceHealthItem,
  createModelConfig,
  deleteModelConfig,
  getCostSummary,
  getIsolationStatus,
  getOwnerToken,
  getRouting,
  getSourcesHealth,
  listModelConfigs,
  putRouting,
  setOwnerToken as persistOwnerToken,
  testModelConfig,
  updateModelConfig,
} from '@/api/models'

export const useSettingsStore = defineStore('settings', () => {
  const configs = ref<ModelConfig[]>([])
  const routingStages = ref<RoutingStage[]>([])
  const routingEntries = ref<RoutingEntry[]>([])
  const costSummary = ref<CostSummary | null>(null)
  const isolation = ref<IsolationStatus | null>(null)
  const sourceHealth = ref<SourceHealthItem[]>([])
  const ownerToken = ref<string>(getOwnerToken())

  const loading = ref(false)
  const saving = ref(false)
  const testing = ref(false)
  const error = ref<string | null>(null)
  /** 真实错误码（`ApiError.code`）与 HTTP 状态码；未取到时为 null，界面显示 unknown_error */
  const errorCode = ref<string | null>(null)
  const errorStatus = ref<number | null>(null)
  const notice = ref<string | null>(null)
  const lastTestResult = ref<ConnectivityResult | null>(null)

  const hasOwnerToken = computed(() => ownerToken.value.length > 0)
  const pricingIncomplete = computed(() => configs.value.filter((item) => !item.pricing_complete))
  const quotaRatio = computed(() => {
    const summary = costSummary.value
    if (!summary || !summary.limit_usd) return 0
    return Math.min(1, summary.used_usd / summary.limit_usd)
  })

  function updateOwnerToken(token: string): void {
    ownerToken.value = token
    persistOwnerToken(token)
  }

  function clearMessages(): void {
    error.value = null
    errorCode.value = null
    errorStatus.value = null
    notice.value = null
  }

  function reportError(err: unknown): void {
    if (err instanceof ApiError) {
      // 403 给更可操作的文案，但错误码/状态码仍如实保留，供视图准确区分权限拒绝态
      error.value = err.status === 403 ? '该操作需要 Owner 令牌（X-Owner-Token）' : err.message
      errorCode.value = err.code
      errorStatus.value = err.status
    } else {
      error.value = err instanceof Error ? err.message : String(err)
      errorCode.value = 'unknown_error'
      errorStatus.value = null
    }
  }

  async function loadConfigs(): Promise<void> {
    try {
      const payload = await listModelConfigs()
      configs.value = payload.items
    } catch (err) {
      reportError(err)
    }
  }

  async function loadRouting(projectId: number | null = null): Promise<void> {
    try {
      const payload = await getRouting(projectId)
      routingStages.value = payload.stages
      routingEntries.value = payload.entries
    } catch (err) {
      reportError(err)
    }
  }

  async function loadCost(projectId: number | null = null): Promise<void> {
    try {
      costSummary.value = await getCostSummary(projectId)
    } catch (err) {
      reportError(err)
    }
  }

  async function loadIsolation(projectId: number | null = null): Promise<void> {
    try {
      isolation.value = await getIsolationStatus(projectId)
    } catch (err) {
      reportError(err)
    }
  }

  async function loadSourceHealth(): Promise<void> {
    try {
      const payload = await getSourcesHealth()
      const raw = (payload.items ?? payload.sources ?? []) as unknown
      const mapped: SourceHealthItem[] = Array.isArray(raw)
        ? (raw as Array<Partial<SourceHealthItem>>).map((entry) => ({
            // `/sources/health` 的条目必须带 ok 判定；缺失时如实标注为 null（不猜测健康）
            ok: entry.ok ?? null,
            name: entry.name ?? String((entry as Record<string, unknown>).source ?? '未命名来源'),
            ...entry,
          }))
        : Object.entries(raw as Record<string, unknown>).map(([name, value]) => ({
            name,
            ok: null,
            ...(typeof value === 'object' && value !== null
              ? (value as object)
              : { detail: String(value) }),
          }))
      sourceHealth.value = mapped
    } catch (err) {
      reportError(err)
    }
  }

  async function loadAll(projectId: number | null = null): Promise<void> {
    loading.value = true
    clearMessages()
    await Promise.all([
      loadConfigs(),
      loadRouting(projectId),
      loadCost(projectId),
      loadIsolation(projectId),
    ])
    loading.value = false
  }

  async function createConfig(payload: {
    name: string
    base_url: string
    api_key: string
    models: ModelEntry[]
    is_default?: boolean
  }): Promise<boolean> {
    saving.value = true
    clearMessages()
    try {
      const created = await createModelConfig(payload)
      configs.value = [...configs.value, created]
      notice.value = `供应商「${created.name}」已保存`
      return true
    } catch (err) {
      reportError(err)
      return false
    } finally {
      saving.value = false
    }
  }

  async function updateConfig(id: number, payload: Record<string, unknown>): Promise<boolean> {
    saving.value = true
    clearMessages()
    try {
      const updated = await updateModelConfig(id, payload)
      configs.value = configs.value.map((item) => (item.id === id ? updated : item))
      notice.value = `供应商「${updated.name}」已更新`
      return true
    } catch (err) {
      reportError(err)
      return false
    } finally {
      saving.value = false
    }
  }

  async function removeConfig(id: number): Promise<boolean> {
    saving.value = true
    clearMessages()
    try {
      await deleteModelConfig(id)
      configs.value = configs.value.filter((item) => item.id !== id)
      notice.value = '供应商已删除'
      return true
    } catch (err) {
      reportError(err)
      return false
    } finally {
      saving.value = false
    }
  }

  async function testConnection(configId: number, modelId?: string): Promise<ConnectivityResult | null> {
    testing.value = true
    clearMessages()
    try {
      const result = await testModelConfig(configId, modelId)
      lastTestResult.value = result
      notice.value = result.ok
        ? `连通性测试通过（${result.latency_ms ?? '-'} ms，模型 ${result.model_ref}）`
        : `连通性测试失败：${result.message ?? result.error_kind}`
      await loadConfigs()
      return result
    } catch (err) {
      reportError(err)
      return null
    } finally {
      testing.value = false
    }
  }

  async function saveRouting(
    entries: Array<Omit<RoutingEntry, 'id' | 'project_id'>>,
    projectId: number | null = null,
  ): Promise<boolean> {
    saving.value = true
    clearMessages()
    try {
      const payload = await putRouting({ project_id: projectId, entries })
      routingStages.value = payload.stages
      routingEntries.value = payload.entries
      notice.value = '路由已保存，下一个环节立即生效'
      await loadIsolation(projectId)
      return true
    } catch (err) {
      reportError(err)
      return false
    } finally {
      saving.value = false
    }
  }

  function buildEntry(
    stage: string,
    configId: number,
    model: ModelEntry,
    previous?: RoutingEntry,
  ): Omit<RoutingEntry, 'id' | 'project_id'> {
    return {
      stage,
      model_config_id: configId,
      model_id: model.model_id,
      temperature: previous?.temperature ?? model.temperature ?? null,
      max_tokens: previous?.max_tokens ?? model.max_tokens ?? null,
      purpose: previous?.purpose ?? null,
    }
  }

  return {
    configs,
    routingStages,
    routingEntries,
    costSummary,
    isolation,
    sourceHealth,
    ownerToken,
    loading,
    saving,
    testing,
    error,
    errorCode,
    errorStatus,
    notice,
    lastTestResult,
    hasOwnerToken,
    pricingIncomplete,
    quotaRatio,
    updateOwnerToken,
    loadAll,
    loadConfigs,
    loadRouting,
    loadCost,
    loadIsolation,
    loadSourceHealth,
    createConfig,
    updateConfig,
    removeConfig,
    testConnection,
    saveRouting,
    buildEntry,
    clearMessages,
    reportError,
  }
})
