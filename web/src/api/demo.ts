/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 演示模式与访问面 API 封装（WP16-T5 / WP16-T6）。
 *
 * 契约（contracts.api_contract.key_endpoints.demo / sse_events.demo_mode）：
 * - `GET  /demo/status`         公开只读：snapshot / replay / access_mode + 三层保险库存
 * - `POST /demo/mode`           **owner_only**：切换 snapshot / replay（广播 demo_mode SSE）
 * - `POST /demo/projects/seed`  **owner_only**：导入/续跑预置示例 Project
 * - `GET  /owner/session`       公开只读：当前访问面（供 OwnerBadge 常驻展示）
 *
 * 红线：
 * - `OWNER_TOKEN` 只从 `sessionStorage`（由 `api/client.ts` 统一注入请求头）读取，
 *   本文件**不保存、不回显、不落任何持久化产物**；
 * - 响应体只含布尔、计数与时间戳；模型 base_url / api_key 一律不进前端；
 * - 演示开关**不下发到浏览器存储**：状态永远是服务端权威口径，避免前端自己"假装"切换。
 *
 * 视图与组件一律经由此文件访问演示端点，禁止组件内直接 fetch（contracts.code_style.frontend）。
 */

import { computed, onScopeDispose, ref } from 'vue'

import { ApiError, get, post } from './client'

/** 访问面（contracts.enums.access_mode） */
export type AccessMode = 'public_demo' | 'owner_mode'

/** 演示模式（由 snapshot / replay 两个布尔派生，用于 DemoBadge 三态展示） */
export type DemoMode = 'live' | 'snapshot' | 'replay' | 'snapshot+replay'

/** 数据来源标签：feed 等端点回传的 `data_source` 受控值 */
export type DataSource = 'live' | 'snapshot' | 'replay' | string | null | undefined

/** 三层演示保险的库存（GET /demo/status -> inventory） */
export interface SnapshotInventory {
  ok?: boolean
  error?: string
  table_rows?: number
  is_demo_rows?: number
  views_covered?: string[]
  missing_views?: string[]
  by_view?: Record<string, number>
  latest_is_demo_at?: string | null
  ready_for_snapshot_demo?: boolean
}

export interface FixtureInventory {
  ok?: boolean
  error?: string
  table_rows?: number
  llm_response_total?: number
  empty_content?: number
  by_stage?: Record<string, number>
  stages_covered?: string[]
  missing_stages?: string[]
  latest_recorded_at?: string | null
  prompt_hash_version_in_table?: string | null
  prompt_hash_version_expected?: string
  hash_version_consistent?: boolean
  ready_for_replay_demo?: boolean
}

export interface DemoProjectBrief {
  id: number
  name: string
  status?: string
  mode?: string
}

export interface DemoProjectInventory {
  ok?: boolean
  error?: string
  projects_total?: number
  is_demo_total?: number
  items?: DemoProjectBrief[]
}

export interface ReplayQuota {
  limit_per_hour?: number
  remaining?: number | null
  retry_after_seconds?: number
  consumed_by_caller?: number
}

export interface PublicDemoSummary {
  access_mode?: AccessMode
  read_methods?: string[]
  public_write_allowlist?: string[]
  owner_header?: string
  owner_token_source?: string
  replay_rate_limit_per_hour?: number
  owner_only_contract?: string[]
  quota?: ReplayQuota
}

/** GET /demo/status 响应 */
export interface DemoStatus {
  access_mode: AccessMode
  /** 论文库是否读 demo 快照（`paper_feed_snapshots.is_demo=true`） */
  snapshot: boolean
  /** LLM 是否从 `demo_fixtures` 回放（响应标 `is_replay=true`） */
  replay: boolean
  /** 派生口径：live / snapshot / replay / snapshot+replay */
  demo_mode: DemoMode | string
  is_demo?: boolean
  source_defaults?: Record<string, unknown>
  overrides?: Record<string, unknown>
  effect?: Record<string, unknown>
  updated_at?: string | null
  updated_by?: string | null
  notes?: string[]
  public_demo?: PublicDemoSummary
  inventory?: {
    snapshots?: SnapshotInventory
    fixtures?: FixtureInventory
    demo_projects?: DemoProjectInventory
  }
}

/** POST /demo/mode 响应 */
export interface DemoModeResult {
  ok: boolean
  changed?: { before?: Record<string, boolean>; after?: Record<string, boolean> }
  status?: DemoStatus
  sse_event?: { event?: string; payload?: { snapshot?: boolean; replay?: boolean } }
}

export interface DemoModeRequest {
  snapshot?: boolean | null
  replay?: boolean | null
  /** 先清除运行期覆盖，回到 `DEMO_*` 环境变量默认值 */
  reset?: boolean
}

/** 单条产出物体检结果（seed 报告） */
export interface SeedCheck {
  key: string
  label: string
  requirement: string
  satisfied: boolean
  detail?: Record<string, unknown>
  next_step?: string | null
}

/** POST /demo/projects/seed 响应 */
export interface SeedReport {
  ok: boolean
  dry_run?: boolean
  job?: string
  status?: string
  project_id?: number | null
  project_slug?: string
  counts?: Record<string, number>
  checks?: SeedCheck[]
  missing_artifacts?: string[]
  next_steps?: string[]
  blockers?: string[]
  inspection?: Record<string, unknown>
  error?: string
  [key: string]: unknown
}

/** GET /owner/session 响应（**不含令牌本身**） */
export interface OwnerSession {
  access_mode: AccessMode
  is_owner: boolean
  demo_mode?: DemoMode | string
  snapshot?: boolean
  replay?: boolean
  owner_header?: string
  owner_token_source?: string
  permissions?: { writes_allowed?: boolean; reason?: string }
  public_demo_summary?: PublicDemoSummary
  checked_at?: string | null
}

const OWNER_ONLY_HINT = '该操作属 owner_only（contracts.api_contract），需服务端环境变量提供的 X-Owner-Token'

/** `GET /demo/status` —— 公开只读，前端演示口径的**唯一权威来源** */
export async function fetchDemoStatus(timeoutMs = 8_000): Promise<DemoStatus> {
  return get<DemoStatus>('/demo/status', { timeoutMs })
}

/**
 * `POST /demo/mode` —— **owner_only**。
 * 切换后服务端广播 `demo_mode {snapshot, replay}`，前端立即反映。
 */
export async function setDemoMode(payload: DemoModeRequest, timeoutMs = 15_000): Promise<DemoModeResult> {
  return post<DemoModeResult>('/demo/mode', { body: payload, timeoutMs })
}

/** `POST /demo/projects/seed` —— **owner_only**；`dry_run=true` 只体检不写库 */
export async function seedDemoProject(
  payload: { dry_run?: boolean; force?: boolean; project_slug?: string } = {},
  timeoutMs = 120_000,
): Promise<SeedReport> {
  return post<SeedReport>('/demo/projects/seed', { body: payload, timeoutMs })
}

/** `GET /owner/session` —— 公开只读；用于 OwnerBadge 常驻显示访问面 */
export async function fetchOwnerSession(timeoutMs = 8_000): Promise<OwnerSession> {
  return get<OwnerSession>('/owner/session', { timeoutMs })
}

// --------------------------------------------------------------------------- //
// 展示口径（文案集中在 API 层，保证 DemoBadge / OwnerBadge / 各视图口径一致）
// --------------------------------------------------------------------------- //

/** 数据来源徽标文案：让评委一眼分清「实时」与「演示固化」 */
export const DATA_SOURCE_LABELS: Record<string, string> = {
  live: '实时结果',
  snapshot: '演示快照（非实时）',
  replay: '回放结果（非实时）',
}

export function dataSourceLabel(source: DataSource, fallback = '来源未标注'): string {
  if (!source) return fallback
  return DATA_SOURCE_LABELS[source] ?? `来源：${source}`
}

/** 是否属于「非实时」的演示固化来源（`snapshot` / `replay`） */
export function isDemoDataSource(source: DataSource): boolean {
  return source === 'snapshot' || source === 'replay'
}

/** 演示模式的中文长文案（DemoBadge 常驻提示用） */
export const DEMO_MODE_LABELS: Record<string, string> = {
  live: '实时运行',
  snapshot: '论文库快照',
  replay: 'LLM 回放',
  'snapshot+replay': '快照 + 回放',
}

export function demoModeLabel(mode: string | null | undefined): string {
  if (!mode) return '实时运行'
  return DEMO_MODE_LABELS[mode] ?? String(mode)
}

/** 由两个布尔派生演示模式（与服务端 `GET /demo/status` 口径一致） */
export function deriveDemoMode(snapshot: boolean, replay: boolean): DemoMode {
  if (snapshot && replay) return 'snapshot+replay'
  if (snapshot) return 'snapshot'
  if (replay) return 'replay'
  return 'live'
}

export const ACCESS_MODE_LABELS: Record<AccessMode, string> = {
  public_demo: '浏览模式（只读）',
  owner_mode: '可编辑（研究者）',
}

export function accessModeLabel(mode: AccessMode | null | undefined): string {
  if (!mode) return '访问面未知'
  return ACCESS_MODE_LABELS[mode] ?? String(mode)
}

export { OWNER_ONLY_HINT }

// --------------------------------------------------------------------------- //
// 共享会话状态（模块级单例）
// --------------------------------------------------------------------------- //

/**
 * DemoBadge 与 OwnerBadge 共用一个轮询源，避免两个组件各拉一遍 `/demo/status`。
 *
 * **刻意不放进 Pinia store**：`stores/` 归各域 WP 所有，且演示开关属于全局基础设施；
 * 放在 API 层可以同时被 `ShellLayout` 与各视图产出物直接引用，而无需改动布局文件。
 */
const status = ref<DemoStatus | null>(null)
const ownerSession = ref<OwnerSession | null>(null)
const loading = ref(false)
const loadError = ref('')
const lastLoadedAt = ref<string | null>(null)

let poller: number | null = null
let subscribers = 0

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}：${error.message}`
  return error instanceof Error ? error.message : String(error)
}

/** 拉取一次演示状态 + 访问面（两者互不阻塞；失败如实记录，不伪造默认值） */
export async function refreshDemoSession(): Promise<void> {
  loading.value = true
  loadError.value = ''
  const [demoResult, ownerResult] = await Promise.allSettled([
    fetchDemoStatus(),
    fetchOwnerSession(),
  ])
  if (demoResult.status === 'fulfilled') status.value = demoResult.value
  if (ownerResult.status === 'fulfilled') ownerSession.value = ownerResult.value
  const failures: string[] = []
  if (demoResult.status === 'rejected') failures.push(`/demo/status ${describe(demoResult.reason)}`)
  if (ownerResult.status === 'rejected') failures.push(`/owner/session ${describe(ownerResult.reason)}`)
  loadError.value = failures.join('；')
  lastLoadedAt.value = new Date().toISOString()
  loading.value = false
}

/** SSE `demo_mode` 事件落地（由 `GET /stream/{project_id}` 的订阅方调用） */
export function applyDemoModeEvent(payload: { snapshot?: boolean; replay?: boolean } | null | undefined): void {
  if (!payload) return
  const snapshot = Boolean(payload.snapshot)
  const replay = Boolean(payload.replay)
  status.value = status.value
    ? { ...status.value, snapshot, replay, demo_mode: deriveDemoMode(snapshot, replay) }
    : status.value
  if (ownerSession.value) {
    ownerSession.value = { ...ownerSession.value, snapshot, replay, demo_mode: deriveDemoMode(snapshot, replay) }
  }
}

export interface DemoSession {
  status: DemoStatus | null
  ownerSession: OwnerSession | null
  loading: boolean
  error: string
  lastLoadedAt: string | null
  snapshot: boolean
  replay: boolean
  demoMode: DemoMode
  accessMode: AccessMode
  isOwner: boolean
  /** 任一演示开关打开 ⇒ 界面必须常驻 DemoBadge */
  demoActive: boolean
  refresh: typeof refreshDemoSession
  startPolling: (intervalMs?: number) => void
  stopPolling: () => void
  toggleMode: (payload: DemoModeRequest) => Promise<DemoModeResult>
  seed: (payload?: { dry_run?: boolean; force?: boolean; project_slug?: string }) => Promise<SeedReport>
}

/**
 * 组件侧入口。第一个调用者自动发起一次刷新；离开作用域计数归零时停止轮询。
 *
 * @param intervalMs 轮询间隔；`0` 表示只拉一次不轮询（默认 30s，演示时足够实时）
 */
export function useDemoSession(intervalMs = 30_000): DemoSession {
  if (status.value === null && !loading.value) void refreshDemoSession()
  subscribers += 1
  if (intervalMs > 0) startPolling(intervalMs)
  onScopeDispose(() => {
    subscribers = Math.max(0, subscribers - 1)
    if (subscribers === 0) stopPolling()
  })

  const snapshot = computed(() => Boolean(status.value?.snapshot))
  const replay = computed(() => Boolean(status.value?.replay))
  const demoMode = computed(() => deriveDemoMode(snapshot.value, replay.value))
  const accessMode = computed<AccessMode>(() => status.value?.access_mode ?? 'public_demo')
  const isOwner = computed(() => ownerSession.value?.is_owner === true)

  return {
    get status() {
      return status.value
    },
    get ownerSession() {
      return ownerSession.value
    },
    get loading() {
      return loading.value
    },
    get error() {
      return loadError.value
    },
    get lastLoadedAt() {
      return lastLoadedAt.value
    },
    get snapshot() {
      return snapshot.value
    },
    get replay() {
      return replay.value
    },
    get demoMode() {
      return demoMode.value
    },
    get accessMode() {
      return accessMode.value
    },
    get isOwner() {
      return isOwner.value
    },
    get demoActive() {
      return snapshot.value || replay.value
    },
    refresh: refreshDemoSession,
    startPolling,
    stopPolling,
    toggleMode,
    seed,
  } as DemoSession
}

function startPolling(intervalMs: number): void {
  if (poller !== null) return
  poller = window.setInterval(() => {
    void refreshDemoSession()
  }, Math.max(5_000, intervalMs))
}

function stopPolling(): void {
  if (poller === null) return
  window.clearInterval(poller)
  poller = null
}

/** 切换演示开关（仅 Owner 面；浏览模式会被拒绝） */
async function toggleMode(payload: DemoModeRequest): Promise<DemoModeResult> {
  const result = await setDemoMode(payload)
  if (result.status) status.value = result.status
  else await refreshDemoSession()
  return result
}

/** 导入/续跑预置示例 Project（**owner_only**），成功后刷新库存 */
async function seed(
  payload: { dry_run?: boolean; force?: boolean; project_slug?: string } = {},
): Promise<SeedReport> {
  const report = await seedDemoProject(payload)
  if (!payload.dry_run) await refreshDemoSession()
  return report
}
