/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 模型与成本域：**请求/响应类型定义 + 端点常量 + 薄封装函数**（WP02）。
 *
 * 职责边界（P1-1 合并后）：
 * - 本文件**不再实现** transport：baseURL 解析、`fetch`、超时、`X-Owner-Token` 注入、
 *   错误规范化、JSON 解析一律由 `./client` 的公共 client 独占（见 client.ts 顶部说明）。
 * - 本文件只保留「这个域打哪些端点、每个端点的载荷长什么样」。
 *
 * 向后兼容：`stores/settings.ts`、`views/SettingsView.vue` 等既有导入方依赖本文件导出的
 * `ApiError` / `getOwnerToken` / `setOwnerToken`，这些名字继续可用，但改为从 `./client`
 * 再导出，从而与其它域共用**同一个** `ApiError` 类（`instanceof` 才不会再分叉）。
 *
 * 约定（contracts.forbidden_actions 第 4 条）：
 * - `api_key` 只上送、不回显；列表接口返回的都是脱敏值
 * - 令牌只存在 sessionStorage，**绝不进入前端构建产物**
 */

import { del, get, patch, post, put } from './client'

/* ------------------------------------------------------------------ *
 * transport 再导出（保持既有导入路径可用；实现唯一在 ./client）
 * ------------------------------------------------------------------ */

export { ApiError, getOwnerToken, setOwnerToken } from './client'

/* ------------------------------------------------------------------ *
 * 供应商
 * ------------------------------------------------------------------ */

export interface ModelEntry {
  model_id: string
  label?: string | null
  context_window?: number | null
  input_price?: number | null
  output_price?: number | null
  price_unit?: number | null
  temperature?: number | null
  max_tokens?: number | null
}

export interface ModelConfig {
  id: number
  name: string
  base_url: string
  models: ModelEntry[]
  is_default: boolean
  api_key_masked: string
  api_key_source: 'encrypted' | 'env_ref' | 'empty' | string
  api_key_fingerprint: string | null
  pricing_complete: boolean
  warning: string | null
  last_tested_at: string | null
  test_ok: boolean | null
  created_at: string | null
}

export interface ModelConfigList {
  items: ModelConfig[]
  total: number
  page: number
  page_size: number
}

export interface ConnectivityResult {
  ok: boolean
  model_ref: string | null
  model_id: string | null
  latency_ms: number | null
  reply_preview: string | null
  error_kind: string | null
  message: string | null
  logged_to: string
}

/* ------------------------------------------------------------------ *
 * 环节路由
 * ------------------------------------------------------------------ */

export interface RoutingStage {
  stage: string
  configured: boolean
  model_ref: string | null
  provider?: string
  model_id?: string
  source: string
  temperature?: number | null
  max_tokens?: number | null
  api_key_configured?: boolean
  error?: string
}

export interface RoutingEntry {
  id?: number
  project_id: number | null
  stage: string
  purpose?: string | null
  model_config_id: number
  model_id: string
  temperature?: number | null
  max_tokens?: number | null
}

export interface RoutingTable {
  project_id: number | null
  stages: RoutingStage[]
  entries: RoutingEntry[]
}

/** 路由解析结果摘要（含脱敏信息，见后端 ResolvedModel.describe()） */
export interface ResolvedModelSummary {
  model_ref?: string | null
  provider?: string
  model_id?: string
  source?: string
  api_key_configured?: boolean
  [key: string]: unknown
}

export interface IsolationStatus {
  isolated: boolean
  generator_stage: string
  reviewer_stage: string
  code: string | null
  message: string | null
  generator: ResolvedModelSummary | null
  reviewer: ResolvedModelSummary | null
}

/* ------------------------------------------------------------------ *
 * 成本摘要与数据源健康
 * ------------------------------------------------------------------ */

/**
 * `GET /costs/summary` 响应。
 *
 * 注意：后端该端点未声明严格 schema（`additionalProperties: true`），本类型以**实测响应**
 * 为准（2026-09-18 采集：`source=llm_call_logs`、`total_calls`、`stub_saved_usd`、`stub_calls`…）。
 * 带 `?` 的字段是实测存在但不保证所有调用路径都返回的补充字段。
 */
export interface CostSummary {
  used_usd: number
  limit_usd: number
  quota_usd: number
  quota_exceeded: boolean
  limit_exceeded: boolean
  breakdown_by_stage: Record<string, number>
  replay_saved_usd: number
  project_id: number | null
  calls: number
  replay_calls: number
  failed_calls: number
  unknown_price_calls: number
  cost_complete: boolean
  warning: string | null
  currency: string
  /** 成本口径来源，实测值为 `llm_call_logs` */
  source?: string | null
  /** 含 stub/replay 的总调用数 */
  total_calls?: number
  /** 非真实调用（stub）折算的节省金额；不累计进 `used_usd` */
  stub_saved_usd?: number
  stub_calls?: number
  /** 口径说明，用于 UI 展示「真实 / stub / replay 分桶」依据 */
  notes?: string | null
}

/**
 * 数据源健康条目。
 * `GET /sources/health` 实测返回 `{status, degraded_sources, sources: {<name>: SourceHealthItem}, …}`，
 * 其中每个条目都带 `ok`；`ok` 保持必填以免下游组件漏处理降级态。
 */
export interface SourceHealthItem {
  name: string
  ok: boolean | null
  detail?: string | null
  [key: string]: unknown
}

/* ------------------------------------------------------------------ *
 * 端点
 * ------------------------------------------------------------------ */

/** 供应商列表（分页） */
export function listModelConfigs(page = 1, pageSize = 50): Promise<ModelConfigList> {
  return get<ModelConfigList>('/models/configs', { query: { page, page_size: pageSize } })
}

/** 新建供应商（Owner 写操作） */
export function createModelConfig(payload: {
  name: string
  base_url: string
  api_key: string
  models: ModelEntry[]
  is_default?: boolean
}): Promise<ModelConfig> {
  return post<ModelConfig>('/models/configs', { body: payload })
}

/** 更新供应商（Owner 写操作） */
export function updateModelConfig(id: number, payload: Record<string, unknown>): Promise<ModelConfig> {
  return patch<ModelConfig>(`/models/configs/${id}`, { body: payload })
}

/** 删除供应商（Owner 写操作） */
export function deleteModelConfig(id: number): Promise<{ deleted: boolean; id: number }> {
  return del<{ deleted: boolean; id: number }>(`/models/configs/${id}`)
}

/**
 * 连通性测试（Owner 写操作）。
 * 该端点会真实打一次上游 LLM，耗时可能远超默认 30s 超时，故显式放大到 120s；
 * 原实现（合并前）不设超时，此处是有界的等价行为。
 */
export function testModelConfig(id: number, modelId?: string): Promise<ConnectivityResult> {
  return post<ConnectivityResult>(`/models/configs/${id}/test`, {
    body: { model_id: modelId ?? null },
    timeoutMs: 120_000,
  })
}

/**
 * 环节路由表。
 * `projectId` 为空（`null` / `undefined` / `0`）时返回全局路由，不发送查询参数
 * —— 与合并前实现的真值判断保持一致。
 */
export function getRouting(projectId?: number | null): Promise<RoutingTable> {
  return get<RoutingTable>('/models/routing', {
    query: projectId ? { project_id: projectId } : undefined,
  })
}

/** 保存环节路由（Owner 写操作） */
export function putRouting(payload: {
  project_id: number | null
  entries: Omit<RoutingEntry, 'id' | 'project_id'>[]
}): Promise<RoutingTable> {
  return put<RoutingTable>('/models/routing', { body: payload })
}

/** 盲评隔离校验（生成模型 ≠ 评审模型） */
export function getIsolationStatus(projectId?: number | null): Promise<IsolationStatus> {
  return get<IsolationStatus>('/models/routing/isolation', {
    query: projectId ? { project_id: projectId } : undefined,
  })
}

/** 成本双线摘要（护栏 8.0 / 演示配额 3.0） */
export function getCostSummary(projectId?: number | null): Promise<CostSummary> {
  return get<CostSummary>('/costs/summary', {
    query: projectId ? { project_id: projectId } : undefined,
  })
}

/** 数据源健康（默认只读库内留痕，`probe=true` 才真实探活） */
export function getSourcesHealth(): Promise<{ items?: SourceHealthItem[] } & Record<string, unknown>> {
  return get<{ items?: SourceHealthItem[] } & Record<string, unknown>>('/sources/health')
}

/* ------------------------------------------------------------------ *
 * 契约常量（LLM 环节）
 * ------------------------------------------------------------------ */

export const LLM_STAGES = [
  'parse',
  'aggregate',
  'ideate',
  'feasibility',
  'survey',
  'plan',
  'plan_review',
  'experiment',
  'writing',
  'review',
  'decide',
] as const

export type LlmStage = (typeof LLM_STAGES)[number]

export const STAGE_LABELS: Record<string, string> = {
  parse: '全文解析',
  aggregate: '多篇聚合解析',
  ideate: '构思生成',
  feasibility: '可行性分析',
  survey: '文献调研',
  plan: '算法生成',
  plan_review: '算法评审（盲评）',
  experiment: '自动实验',
  writing: '论文写作',
  review: '论文评审',
  decide: '决策建议',
}
