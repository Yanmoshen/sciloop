/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 会话级状态（WP01）：访问面 / Owner 令牌 / 主题 / 当前 Project / 风险策略阈值 /
 * 后端与数据库健康状态。业务数据一律不放这里（见各域 store）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, clearOwnerToken, get, getOwnerToken, setOwnerToken } from '@/api/client'

export type AccessMode = 'public_demo' | 'owner_mode'
export type ThemeMode = 'light' | 'dark'

const THEME_KEY = 'sciloop.theme'

export interface ProjectBrief {
  id: number
  name: string
  status?: string
  mode?: string
  is_demo?: boolean
}

export interface HealthPayload {
  status: string
  app: { name: string; version: string; env: string; access_mode: AccessMode }
  db: { ok: boolean; detail: string; database?: string; server_version?: string }
  checked_at: string
}

export interface RiskThresholds {
  /** risk_score ≤ autoMax 且 confidence ≥ confidenceAutoMin 且 reversibility ≥ reversibilityAutoMin → auto_execute */
  autoMax: number
  humanMax: number
  confidenceAutoMin: number
  confidenceBreakMin: number
  reversibilityAutoMin: number
}

export const useSessionStore = defineStore('session', () => {
  // ---- 主题（P0 浅色 / P1 深色）----
  const theme = ref<ThemeMode>(readTheme())

  function readTheme(): ThemeMode {
    try {
      const saved = localStorage.getItem(THEME_KEY)
      if (saved === 'dark' || saved === 'light') return saved
    } catch {
      /* 忽略存储不可用 */
    }
    return (import.meta.env?.VITE_DEFAULT_THEME as ThemeMode | undefined) ?? 'light'
  }

  function applyTheme(next: ThemeMode): void {
    theme.value = next
    document.documentElement.dataset.theme = next
    // Element Plus 的暗色变量挂在 html.dark 下，必须同步切换（只改 data-theme 不够）
    document.documentElement.classList.toggle('dark', next === 'dark')
    try {
      localStorage.setItem(THEME_KEY, next)
    } catch {
      /* 忽略存储不可用 */
    }
  }

  function toggleTheme(): void {
    applyTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  // ---- 访问面与 Owner 令牌 ----
  const ownerToken = ref<string>(getOwnerToken())
  const accessMode = ref<AccessMode>('public_demo')

  /** owner 令牌已就位即视为可写面（后端仍会独立校验） */
  const isOwner = computed(() => ownerToken.value.length > 0)
  const accessLabel = computed(() =>
    isOwner.value ? 'owner_mode（可写）' : 'public_demo（只读）',
  )

  function updateOwnerToken(token: string): void {
    setOwnerToken(token)
    ownerToken.value = getOwnerToken()
  }

  function forgetOwnerToken(): void {
    clearOwnerToken()
    ownerToken.value = ''
  }

  // ---- 当前 Project ----
  const projects = ref<ProjectBrief[]>([])
  const currentProjectId = ref<number | null>(null)
  const projectsError = ref('')
  const currentProject = computed(
    () => projects.value.find((p) => p.id === currentProjectId.value) ?? null,
  )

  function selectProject(id: number | null): void {
    currentProjectId.value = id
  }

  async function loadProjects(): Promise<void> {
    try {
      const data = await get<
        { items: ProjectBrief[]; total: number } | ProjectBrief[]
      >('/projects', { query: { page: 1, page_size: 50 } })
      projects.value = Array.isArray(data) ? data : (data.items ?? [])
      // 后端按 is_demo 优先排序，默认选中第一个
      if (currentProjectId.value === null && projects.value.length > 0) {
        currentProjectId.value = projects.value[0]?.id ?? null
      }
    } catch (error) {
      // /projects 属 WP16 交付范围，未就绪时保持空列表且不阻塞页面
      projects.value = []
      if (!(error instanceof ApiError && error.status === 404)) projectsError.value = message(error)
    }
  }

  // ---- 健康状态 ----
  const health = ref<HealthPayload | null>(null)
  const healthError = ref('')

  async function loadHealth(): Promise<void> {
    try {
      const data = await get<HealthPayload>('/health', { timeoutMs: 5_000 })
      health.value = data
      healthError.value = data.db.ok ? '' : data.db.detail
      accessMode.value = data.app.access_mode
    } catch (error) {
      health.value = null
      healthError.value = message(error)
    }
  }

  // ---- 风险策略阈值（默认值来自契约，运行时可由 WP10 覆盖）----
  const riskThresholds = ref<RiskThresholds>({
    autoMax: 30,
    humanMax: 70,
    confidenceAutoMin: 0.75,
    confidenceBreakMin: 0.5,
    reversibilityAutoMin: 0.6,
  })

  function updateRiskThresholds(next: Partial<RiskThresholds>): void {
    riskThresholds.value = { ...riskThresholds.value, ...next }
  }

  function message(error: unknown): string {
    return error instanceof Error ? error.message : String(error)
  }

  return {
    accessLabel,
    accessMode,
    applyTheme,
    currentProject,
    currentProjectId,
    forgetOwnerToken,
    health,
    healthError,
    isOwner,
    loadHealth,
    loadProjects,
    ownerToken,
    projects,
    projectsError,
    riskThresholds,
    selectProject,
    theme,
    toggleTheme,
    updateOwnerToken,
    updateRiskThresholds,
  }
})
