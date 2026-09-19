<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 知识资产库（P1）：把「论文卡片 / 证据 / 决策 / Experiment Passport」四类可复验沉淀
 * 收敛到一个可检索入口，全部数据取自既有只读接口，**不新增后端能力、不编造任何字段**。
 *
 * 数据来源（均为 `src/api/**` 现有导出，本页不直接 fetch）：
 * - 论文卡片：`GET /papers/search`（经公共 client 的 `get`）→ `fetchPaperDetail` / `fetchCard` / `fetchDocuments`
 * - 证据：    `GET /evidence/types`（受控值域 + fulltext_gate 阈值）+ `getEvidenceDetail`
 * - 决策：    `GET /decisions/{project_id}`（getDecisionOverview，含阈值与护栏明细）
 * - Passport：`GET /runs?project_id=` → `GET /experiments/runs/{run_id}/passport`（getRunPassport）
 *
 * 六类状态：loading / empty / error / permission denied /
 * retry 全部有可见表达（见 `ViewStatePanel` 与各 Tab 内联空态）。
 */
import { computed, onMounted, ref } from 'vue'

import { get } from '@/api/client'
import { getEvidenceDetail, getEvidenceTypes, type EvidenceDetail } from '@/api/claims'
import {
  CARD_FIELDS,
  fetchCard,
  fetchDocuments,
  fetchPaperDetail,
  type CardEntry,
  type CardResponse,
  type DocumentsResponse,
  type PaperDetail,
} from '@/api/parse'
import {
  getCostLimits,
  getDecisionOverview,
  getRunPassport,
  listPipelineRuns,
  STAGE_LABELS,
  STOP_REASON_LABELS,
  type DecisionOverview,
  type DecisionRecord,
  type PassportRecord,
  type PipelineRunInfo,
} from '@/api/workbench'
import CoverageTag from '@/components/CoverageTag.vue'
import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { useSessionStore } from '@/stores/session'

/** `GET /papers/search` 的条目（字段与 feed 条目同源；api 层暂无该端点的类型导出） */
interface SearchPaperItem {
  id: number
  title: string
  abstract?: string | null
  published_at?: string | null
  venue?: string | null
  citation_count?: number | null
  rank_score?: number | null
  source?: string | null
  external_id?: string | null
}

interface SearchResponse {
  items?: SearchPaperItem[]
  total?: number
  note?: string
  [key: string]: unknown
}

const session = useSessionStore()

const tab = ref<'cards' | 'evidence' | 'decisions' | 'passports'>('cards')

/** 本页只归档事实：stop_reason 的显著展示在 Workbench 看板顶部 */
const stopReasonHint =
  '停止原因（stop_reason）在 Workbench 看板顶部显著展示；本页只归档 Passport / 决策事实，不做二次判定。'

/** 展示辅助（纯格式化，不做推断） */
function stageLabel(stage: string | null | undefined): string {
  if (!stage) return '环节未标注'
  return STAGE_LABELS[stage as keyof typeof STAGE_LABELS] ?? stage
}

function runLabel(run: PipelineRunInfo): string {
  const stop = run.stop_reason ? ` · ${STOP_REASON_LABELS[run.stop_reason]}` : ''
  return `run #${run.id} · iter ${run.iteration} · ${run.status}${stop}`
}

function json(value: unknown): string {
  if (value === null || value === undefined) return '—'
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

/** 令牌失效/缺失属前端可判定信息，写操作一律禁用并说明（本页仅读，故不出现写按钮） */
const ownerDenied = computed(() => !session.isOwner)

const FIELD_OPTIONS = ['cs.AI', 'cs.CL', 'cs.CV', 'cs.LG']

/* ------------------------------------------------------------------ *
 * 一、论文卡片沉淀
 * ------------------------------------------------------------------ */
const keyword = ref('')
const field = ref<string | null>(null)
const searchLimit = ref(20)
const searchItems = ref<SearchPaperItem[]>([])
const searchTotal = ref<number | null>(null)
const searchLoading = ref(false)
const searchError = ref<string | null>(null)
const searchErrorCode = ref<string | null>(null)
const searchNotes = ref<string[]>([])

const activePaperId = ref<number | null>(null)
const paperDetail = ref<PaperDetail | null>(null)
const paperCard = ref<CardResponse | null>(null)
const paperDocs = ref<DocumentsResponse | null>(null)
const assetLoading = ref(false)
const assetError = ref<string | null>(null)
const assetErrorCode = ref<string | null>(null)

function describe(error: unknown): { code: string; message: string } {
  const candidate = error as { code?: string; message?: string }
  return {
    code: candidate?.code ?? 'unknown_error',
    message: candidate?.message ?? String(error),
  }
}

async function runSearch(): Promise<void> {
  searchLoading.value = true
  searchError.value = null
  searchErrorCode.value = null
  try {
    const response = await get<SearchResponse>('/papers/search', {
      query: {
        q: keyword.value.trim() || undefined,
        field: field.value ?? undefined,
        limit: searchLimit.value,
      },
    })
    searchItems.value = response?.items ?? []
    searchTotal.value = typeof response?.total === 'number' ? response.total : null
    const note = typeof response?.note === 'string' ? response.note : null
    searchNotes.value = note ? [note] : []
  } catch (error) {
    searchItems.value = []
    searchTotal.value = null
    const { code, message } = describe(error)
    searchErrorCode.value = code
    searchError.value = message
  } finally {
    searchLoading.value = false
  }
}

/** 读取一篇论文的三类资产：详情 / 8 字段卡片 / 解析文档（缺失一律如实标注） */
async function loadAsset(paperId: number): Promise<void> {
  activePaperId.value = paperId
  assetLoading.value = true
  assetError.value = null
  assetErrorCode.value = null
  paperDetail.value = null
  paperCard.value = null
  paperDocs.value = null
  const failures: string[] = []
  try {
    paperDetail.value = await fetchPaperDetail(paperId)
  } catch (error) {
    const { code, message } = describe(error)
    assetErrorCode.value = code
    failures.push(`论文详情：${message}`)
  }
  try {
    paperCard.value = await fetchCard(paperId)
  } catch (error) {
    const { message } = describe(error)
    failures.push(`解析卡片：${message}（未建卡时不展示任何字段，缺失不编造）`)
  }
  try {
    paperDocs.value = await fetchDocuments(paperId)
  } catch (error) {
    const { message } = describe(error)
    failures.push(`解析记录：${message}`)
  }
  assetError.value = failures.length ? failures.join('；') : null
  assetLoading.value = false
}


/** 卡片 8 字段的紧凑摘要（只做展示聚合，不做任何推断） */
const cardFieldRows = computed(() => {
  const content = paperCard.value?.card
  if (!content) return []
  return CARD_FIELDS.map((def) => {
    const stats = paperCard.value?.by_field?.[def.key as string]
    const value = content[def.key]
    let text: string
    if (Array.isArray(value)) {
      const entries = value as CardEntry[]
      text = entries
        .slice(0, 2)
        .map(
          (entry) =>
            entry.point ?? entry.conclusion ?? entry.limitation ?? entry.description ?? '未获取',
        )
        .join('；')
      if (entries.length > 2) text += ` …（共 ${entries.length} 条）`
    } else if (value && typeof value === 'object') {
      const setup = value as { metrics?: string[]; datasets?: string[]; baselines?: string[] }
      text = `metrics ${setup.metrics?.join('/') ?? '未获取'} · datasets ${
        setup.datasets?.join('/') ?? '未获取'
      } · baselines ${setup.baselines?.join('/') ?? '未获取'}`
    } else {
      text = String(value ?? '未获取')
    }
    return {
      key: def.key as string,
      label: def.label,
      text,
      located: stats?.located ?? null,
      total: stats?.total ?? null,
      unlocated: stats?.unlocated ?? null,
    }
  })
})

const cardEvidenceSpans = computed(() => {
  const content = paperCard.value?.card
  if (!content) return []
  const spans: Array<{
    field: string
    label: string
    verdict: string
    section: string
    page: number | null
    quote: string
  }> = []
  CARD_FIELDS.forEach((def) => {
    const value = content[def.key]
    if (!Array.isArray(value)) return
    for (const entry of value as CardEntry[]) {
      const span = entry.evidence_span
      if (!span) continue
      spans.push({
        field: def.key as string,
        label: def.label,
        verdict: span.verification?.verdict ?? '未校验',
        section: span.section_name ?? '章节未标注',
        page: span.page_number ?? null,
        quote: span.quote_text ?? '',
      })
    }
  })
  return spans
})

/* ------------------------------------------------------------------ *
 * 二、证据沉淀（受控值域 + 哈希优先校验）
 * ------------------------------------------------------------------ */
const evidenceTypes = ref<string[]>([])
const evidenceGate = ref<{ parse_status: string; min_coverage: number; rule: string } | null>(null)
const typesLoading = ref(false)
const typesError = ref<string | null>(null)
const evidenceType = ref('paper_span')
const evidenceId = ref('')
const idKind = ref<'evidence' | 'native'>('native')
const evidence = ref<EvidenceDetail | null>(null)
const evidenceLoading = ref(false)
const evidenceError = ref<string | null>(null)
const evidenceErrorCode = ref<string | null>(null)

async function loadEvidenceTypes(): Promise<void> {
  typesLoading.value = true
  typesError.value = null
  try {
    const payload = await getEvidenceTypes()
    evidenceTypes.value = payload.evidence_types ?? []
    evidenceGate.value = payload.fulltext_gate ?? null
    if (evidenceTypes.value.length && !evidenceTypes.value.includes(evidenceType.value)) {
      evidenceType.value = evidenceTypes.value[0] as string
    }
  } catch (error) {
    const { message } = describe(error)
    evidenceTypes.value = []
    typesError.value = message
  } finally {
    typesLoading.value = false
  }
}

async function loadEvidence(): Promise<void> {
  if (!evidenceId.value.trim()) {
    evidenceError.value = '请先填写 evidence_id（`evidences.id` 或该类型自身主键，取决于 id_kind）'
    evidenceErrorCode.value = 'missing_argument'
    return
  }
  evidenceLoading.value = true
  evidenceError.value = null
  evidenceErrorCode.value = null
  try {
    evidence.value = await getEvidenceDetail(evidenceType.value, evidenceId.value.trim(), {
      idKind: idKind.value,
    })
  } catch (error) {
    evidence.value = null
    const { code, message } = describe(error)
    evidenceErrorCode.value = code
    evidenceError.value = message
  } finally {
    evidenceLoading.value = false
  }
}


/* ------------------------------------------------------------------ *
 * 三、决策沉淀
 * ------------------------------------------------------------------ */
const projectId = ref<number | null>(session.currentProjectId)
const decisions = ref<DecisionOverview | null>(null)
const decisionsLoading = ref(false)
const decisionsError = ref<string | null>(null)
const decisionsErrorCode = ref<string | null>(null)
const costLimits = ref<{ limit_usd: number; quota_usd: number; rule: string } | null>(null)
const costLimitsError = ref<string | null>(null)

const DECISION_POINT_LABELS: Record<string, string> = {
  D1: '检索策略',
  D2: '方案选型',
  D3: '实验配置',
  D4: '失败处置',
  D5: '迭代判据',
  D6: '写作结构',
}

async function loadDecisions(): Promise<void> {
  if (projectId.value === null) {
    decisionsError.value = '请先选择 Project（决策记录按 project_id 归档）'
    decisionsErrorCode.value = 'missing_project'
    return
  }
  decisionsLoading.value = true
  decisionsError.value = null
  decisionsErrorCode.value = null
  try {
    decisions.value = await getDecisionOverview(projectId.value)
  } catch (error) {
    decisions.value = null
    const { code, message } = describe(error)
    decisionsErrorCode.value = code
    decisionsError.value = message
  } finally {
    decisionsLoading.value = false
  }
}

async function loadCostLimits(): Promise<void> {
  try {
    costLimits.value = await getCostLimits()
  } catch (error) {
    costLimits.value = null
    costLimitsError.value = describe(error).message
  }
}

const decisionGroups = computed(() => {
  const groups: Record<string, DecisionRecord[]> = {}
  for (const item of decisions.value?.items ?? []) {
    const key = String(item.decision_point)
    if (!groups[key]) groups[key] = []
    groups[key].push(item)
  }
  return Object.entries(groups).sort((a, b) => a[0].localeCompare(b[0]))
})

/** 决策沉淀的降级：回退到 WP09 决策日志（无节点映射与阈值来源）或护栏阈值缺失 */

/* ------------------------------------------------------------------ *
 * 四、Experiment Passport 沉淀
 * ------------------------------------------------------------------ */
const runs = ref<PipelineRunInfo[]>([])
const runsLoading = ref(false)
const runsError = ref<string | null>(null)
const runsErrorCode = ref<string | null>(null)
const selectedRunId = ref<number | null>(null)
const passport = ref<PassportRecord | null>(null)
const passportLoading = ref(false)
const passportError = ref<string | null>(null)
const passportErrorCode = ref<string | null>(null)
const passportUnavailable = ref<string | null>(null)

async function loadRuns(): Promise<void> {
  if (projectId.value === null) {
    runsError.value = '请先选择 Project（流水线运行按 project_id 归档）'
    runsErrorCode.value = 'missing_project'
    return
  }
  runsLoading.value = true
  runsError.value = null
  runsErrorCode.value = null
  try {
    const payload = await listPipelineRuns({ project_id: projectId.value, page: 1, page_size: 50 })
    runs.value = payload.items ?? []
    if (runs.value.length && selectedRunId.value === null) {
      selectedRunId.value = runs.value[0]?.id ?? null
    }
  } catch (error) {
    runs.value = []
    const { code, message } = describe(error)
    runsErrorCode.value = code
    runsError.value = message
  } finally {
    runsLoading.value = false
  }
}

async function loadPassport(): Promise<void> {
  if (selectedRunId.value === null) {
    passportError.value = '请先选择 run（Passport 挂在具体运行上）'
    passportErrorCode.value = 'missing_run'
    return
  }
  passportLoading.value = true
  passportError.value = null
  passportErrorCode.value = null
  passportUnavailable.value = null
  passport.value = null
  const result = await getRunPassport(selectedRunId.value)
  if (result.available) {
    passport.value = result.data
  } else {
    passportUnavailable.value = `${result.waitingFor}：${result.reason}`
  }
  passportLoading.value = false
}


/* ------------------------------------------------------------------ *
 * 组合
 * ------------------------------------------------------------------ */
const projectOptions = computed(() => session.projects)

function onProjectChange(value: number | null): void {
  projectId.value = value
  if (value !== null) session.selectProject(value)
  decisions.value = null
  passport.value = null
  selectedRunId.value = null
  runs.value = []
}

async function retryActive(): Promise<void> {
  if (tab.value === 'cards') {
    await runSearch()
    if (activePaperId.value !== null) await loadAsset(activePaperId.value)
    return
  }
  if (tab.value === 'evidence') {
    await Promise.all([loadEvidenceTypes(), loadEvidence()])
    return
  }
  if (tab.value === 'decisions') {
    await Promise.all([loadDecisions(), loadCostLimits()])
    return
  }
  await loadRuns()
  if (selectedRunId.value !== null) await loadPassport()
}

const activeError = computed(() => {
  switch (tab.value) {
    case 'cards':
      return searchError.value
    case 'evidence':
      return evidenceError.value ?? typesError.value
    case 'decisions':
      return decisionsError.value
    default:
      return runsError.value ?? passportError.value
  }
})
const activeErrorCode = computed(() => {
  switch (tab.value) {
    case 'cards':
      return searchErrorCode.value
    case 'evidence':
      return evidenceErrorCode.value ?? 'types_unavailable'
    case 'decisions':
      return decisionsErrorCode.value
    default:
      return passportErrorCode.value ?? runsErrorCode.value
  }
})
const activeLoading = computed(() => {
  switch (tab.value) {
    case 'cards':
      return searchLoading.value || assetLoading.value
    case 'evidence':
      return typesLoading.value || evidenceLoading.value
    case 'decisions':
      return decisionsLoading.value
    default:
      return runsLoading.value || passportLoading.value
  }
})
/** 权限拒绝：仅当失败确为 401/403 时展示（本页只读，不无差别标注） */
const activePermissionDenied = computed(
  () => ['owner_token_required', 'forbidden', 'unauthorized'].includes(activeErrorCode.value ?? ''),
)

onMounted(async () => {
  await Promise.all([runSearch(), loadEvidenceTypes(), loadCostLimits()])
  if (session.currentProjectId === null) await session.loadProjects()
  if (projectId.value === null) projectId.value = session.currentProjectId
})
</script>

<template>
  <section class="knowledge">


    <header class="knowledge__head">
      <div>
        <h1>知识资产</h1>
      </div>
      <div class="knowledge__head-side">
        <el-select
          :model-value="projectId"
          class="knowledge__project"
          size="small"
          filterable
          placeholder="选择 Project（决策 / Passport 用）"
          @update:model-value="(value: number | null) => onProjectChange(value)"
        >
          <el-option
            v-for="item in projectOptions"
            :key="item.id"
            :label="`${item.name} · #${item.id}`"
            :value="item.id"
          />
        </el-select>
      </div>
    </header>

    <!-- 成本双线（合规强制元素）：护栏 8.0 与演示配额 3.0 并列 -->
    <section class="cost" aria-label="成本双线">
      <span class="cost__title">成本双线</span>
      <span class="cost__line">
        护栏值（硬熔断）<strong>{{ costLimits ? costLimits.limit_usd.toFixed(1) : '8.0' }}</strong> USD
      </span>
      <span class="cost__line">
        演示配额（只告警）<strong>{{ costLimits ? costLimits.quota_usd.toFixed(1) : '3.0' }}</strong> USD
      </span>
      <span class="cost__note">
        {{ costLimits?.rule ?? '接口未取到：以上为契约常量双线（8.0 / 3.0），不与真实消耗混算' }}
      </span>
    </section>

    <ViewStatePanel
      :loading="activeLoading"
      loading-text="正在读取知识资产…"
      :error="activeError"
      :error-code="activeErrorCode"
      error-title="知识资产读取失败"
      :permission-denied="activePermissionDenied"
      :permission-note="activeError"
      retryable
      retry-label="重试当前标签页"
      :busy="activeLoading"
      @retry="retryActive"
    />

    <p v-if="ownerDenied" class="knowledge__readonly">
      当前为 public_demo 只读面：本页只做检索与核验，不提供写操作（建卡 / 回放 / 重跑等入口在 Workbench 内并已按访问面禁用）。
    </p>

    <el-tabs v-model="tab" class="knowledge__tabs">
      <el-tab-pane label="论文卡片" name="cards">
        <div class="toolbar">
          <el-input
            v-model="keyword"
            size="small"
            class="toolbar__input"
            placeholder="标题 / 摘要关键词（留空则按 rank_score 列出）"
            clearable
            @keyup.enter="runSearch"
          />
          <el-select v-model="field" size="small" class="toolbar__field" placeholder="全部领域" clearable>
            <el-option v-for="item in FIELD_OPTIONS" :key="item" :label="item" :value="item" />
          </el-select>
          <el-button size="small" type="primary" :loading="searchLoading" @click="runSearch">检索卡片</el-button>
          <span class="toolbar__meta">
            返回 {{ searchItems.length }} 条<template v-if="searchTotal !== null"> / 共 {{ searchTotal }} 条</template>
          </span>
        </div>

        <el-skeleton v-if="searchLoading && searchItems.length === 0" :rows="4" animated />
        <el-empty
          v-else-if="searchItems.length === 0"
          description="没有匹配的论文资产（未获取 ≠ 0 条：可调整关键词或留空后重试）"
        />

        <div v-else class="split">
          <ul class="asset-list scroll-y">
            <li
              v-for="item in searchItems"
              :key="item.id"
              class="asset-item"
              :class="{ 'asset-item--active': item.id === activePaperId }"
              @click="loadAsset(item.id)"
            >
              <p class="asset-item__title">{{ item.title }}</p>
              <p class="asset-item__meta">
                #{{ item.id }} · {{ item.source ?? '来源未获取' }} · {{ item.published_at ?? '时间未获取' }} ·
                venue {{ item.venue ?? '未获取' }} · 引用 {{ item.citation_count ?? '未获取' }} · rank_score
                {{ item.rank_score ?? '未获取' }}
              </p>
            </li>
          </ul>

          <section class="asset-detail">
            <el-skeleton v-if="assetLoading" :rows="6" animated />
            <template v-else-if="activePaperId === null">
              <el-empty description="点击左侧任一条目读取该论文的卡片 / 文档 / 证据沉淀" />
            </template>
            <template v-else>
              <header class="asset-detail__head">
                <h3>{{ paperDetail?.title ?? `论文 #${activePaperId}` }}</h3>
                <RouterLink class="link" :to="`/papers/parse/${activePaperId}`">前往解析页核验原文</RouterLink>
              </header>

              <div class="asset-detail__badges">
                <CoverageTag
                  :parse-status="paperCard?.parse_status ?? null"
                  :coverage="paperCard?.coverage ?? paperDocs?.summary?.coverage ?? null"
                  :scope="paperCard?.available_scope ?? paperDocs?.evidence_scope ?? null"
                  :source="paperCard?.document_version ? 'paper_cards' : null"
                  :note="paperCard?.coverage_tag ?? paperDocs?.coverage_note ?? null"
                />
                <span class="chip">卡片版本 v{{ paperCard?.version ?? '未获取' }}</span>
                <span class="chip">
                  已定位 {{ paperCard?.located_count ?? '未获取' }}/{{ (paperCard?.located_count ?? 0) + (paperCard?.unlocated_count ?? 0) }}
                </span>
                <span class="chip" :class="paperCard?.llm_call_log_traceable ? 'chip--ok' : 'chip--warn'">
                  llm_call_log 可溯源：{{ paperCard?.llm_call_log_traceable === true ? '是' : '否/未获取' }}
                </span>
                <span v-if="paperCard?.llm_call_log?.is_replay" class="chip chip--warn">is_replay=true（回放）</span>
              </div>

              <table v-if="cardFieldRows.length" class="fields">
                <tbody>
                  <tr v-for="row in cardFieldRows" :key="row.key">
                    <th>{{ row.label }}</th>
                    <td>
                      <span class="fields__text">{{ row.text }}</span>
                      <span v-if="row.total !== null" class="fields__stat">
                        定位 {{ row.located ?? 0 }}/{{ row.total }}<template v-if="row.unlocated"> · 未定位 {{ row.unlocated }}</template>
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
              <el-empty v-else description="该论文尚无解析卡片：不展示字段内容（缺失不编造，建卡需 Owner 面）" />

              <section class="spans">
                <h4>可定位证据片段（{{ cardEvidenceSpans.length }}）</h4>
                <el-empty v-if="cardEvidenceSpans.length === 0" description="卡片条目未附可定位片段（证据覆盖范围受限）" />
                <ul v-else>
                  <li v-for="(span, idx) in cardEvidenceSpans" :key="`${span.field}-${idx}`">
                    <span class="verdict" :class="`verdict--${span.verdict}`">{{ span.verdict }}</span>
                    <span class="sl-source-tag">
                      {{ span.label }} · {{ span.section }} · 页 {{ span.page ?? '未获取' }}
                    </span>
                    <blockquote>{{ span.quote }}</blockquote>
                  </li>
                </ul>
              </section>

              <p class="asset-detail__note">
                文档版本 {{ paperDocs?.summary?.document_version ?? '未获取' }} · parser
                {{ paperDocs?.summary?.parser ?? '未获取' }} · 覆盖率
                {{ paperDocs?.summary?.coverage ?? '未获取' }}（缺失显示未获取，不代表 0）
              </p>
            </template>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="证据" name="evidence">
        <p class="toolbar__meta">
          受控值域：{{ evidenceTypes.length ? evidenceTypes.join(' / ') : '未获取' }}
          <template v-if="evidenceGate">
            ｜门禁：parse_status={{ evidenceGate.parse_status }} 且 coverage≥{{ evidenceGate.min_coverage }}
          </template>
        </p>
        <div class="toolbar">
          <el-select v-model="evidenceType" size="small" class="toolbar__field" placeholder="证据类型">
            <el-option v-for="item in evidenceTypes" :key="item" :label="item" :value="item" />
          </el-select>
          <el-select v-model="idKind" size="small" class="toolbar__field">
            <el-option label="id_kind=evidence（evidences.id）" value="evidence" />
            <el-option label="id_kind=native（该类型自身主键）" value="native" />
          </el-select>
          <el-input
            v-model="evidenceId"
            size="small"
            class="toolbar__input"
            placeholder="evidence_id（如 paper_span 的 span id）"
            @keyup.enter="loadEvidence"
          />
          <el-button size="small" type="primary" :loading="evidenceLoading" @click="loadEvidence">解析证据</el-button>
        </div>

        <el-skeleton v-if="evidenceLoading" :rows="4" animated />
        <el-empty
          v-else-if="!evidence"
          description="尚无证据结果：填写 evidence_id 后点击「解析证据」（不存在的对象返回 404，不补造）"
        />
        <section v-else class="evidence">
          <div class="asset-detail__badges">
            <span class="chip">{{ evidence.evidence_type }}</span>
            <span class="chip" :class="evidence.verdict === 'valid' ? 'chip--ok' : 'chip--warn'">
              verdict={{ evidence.verdict ?? '未提供' }}
            </span>
            <span class="chip">hash_match={{ evidence.verification?.hash_match ?? '未提供' }}</span>
            <span class="chip">offset_match={{ evidence.verification?.offset_match ?? '未提供' }}</span>
            <span class="chip">fulltext_gate {{ evidence.gate?.ok === true ? '通过' : '未通过/未提供' }}</span>
          </div>
          <blockquote v-if="evidence.quote_text" class="quote">{{ evidence.quote_text }}</blockquote>
          <p class="asset-detail__note">
            论文 {{ evidence.paper?.title ?? evidence.paper_id ?? '未提供' }} · 章节
            {{ evidence.span?.section_name ?? evidence.section_name ?? '未标注' }} · 页
            {{ evidence.span?.page_number ?? evidence.page_number ?? '未获取' }} · document_version
            {{ evidence.document_version ?? '未携带' }}
          </p>
          <p class="asset-detail__note">
            覆盖率 {{ evidence.gate?.coverage ?? '未提供' }} · 证据范围
            {{ evidence.gate?.evidence_scope ?? evidence.evidence_scope ?? '未提供' }} ·
            {{ evidence.gate?.coverage_note ?? evidence.gate?.reason ?? '无门禁说明' }}
          </p>
          <p v-if="evidence.verification?.reason" class="asset-detail__note">
            校验理由：{{ evidence.verification.reason }}
          </p>
          <RouterLink
            v-if="evidence.paper_id"
            class="link"
            :to="`/papers/parse/${evidence.paper_id}`"
          >
            前往解析页核验论文 #{{ evidence.paper_id }}
          </RouterLink>
        </section>
      </el-tab-pane>

      <el-tab-pane label="决策" name="decisions">
        <div class="toolbar">
          <el-button size="small" type="primary" :loading="decisionsLoading" @click="loadDecisions">
            读取决策记录
          </el-button>
          <span class="toolbar__meta">
            共 {{ decisions?.items?.length ?? 0 }} 条 ·
            策略版本 {{ decisions?.policy_version ?? decisions?.thresholds?.policy_version ?? '未获取' }} ·
            来源 {{ decisions?.source ?? '未获取' }}
          </span>
        </div>

        <el-skeleton v-if="decisionsLoading" :rows="4" animated />
        <el-empty
          v-else-if="decisionGroups.length === 0"
          description="该项目暂无决策记录"
        />
        <section v-else class="decisions">
          <el-alert
            v-if="decisions?.guardrail_limits"
            type="info"
            :closable="false"
            class="guardrail"
            :title="`护栏版本 ${decisions.guardrail_limits.version}：成本硬线 ${decisions.guardrail_limits.cost_hard_limit_usd} USD / 演示配额 ${decisions.guardrail_limits.cost_demo_quota_usd} USD（阈值不可被 LLM 覆盖）`"
          />
          <div v-for="[point, items] in decisionGroups" :key="point" class="decision-group">
            <h4>{{ point }} · {{ DECISION_POINT_LABELS[point] ?? '决策点' }}（{{ items.length }}）</h4>
            <ul>
              <li v-for="item in items" :key="item.id" class="decision-item">
                <div class="decision-item__head">
                  <span class="chip" :class="`chip--policy-${item.policy_action}`">{{ item.policy_action }}</span>
                  <span class="chip">{{ stageLabel(item.stage) }}</span>
                  <span class="chip">chosen={{ item.chosen }}</span>
                  <span class="chip">risk {{ item.risk_score ?? '未获取' }}</span>
                  <span class="chip">confidence {{ item.confidence_score ?? '未获取' }}</span>
                  <span class="chip">reversibility {{ item.reversibility_score ?? '未获取' }}</span>
                  <span class="chip">policy_version {{ item.policy_version }}</span>
                </div>
                <p class="decision-item__rationale">{{ item.rationale }}</p>
                <p class="asset-detail__note">
                  候选：{{ item.options_considered.join(' / ') || '未提供' }} · 成本
                  {{ item.cost_usd ?? '未获取' }} USD · 时间 {{ item.created_at ?? '未获取' }}
                </p>
              </li>
            </ul>
          </div>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Experiment Passport" name="passports">
        <div class="toolbar">
          <el-button size="small" type="primary" :loading="runsLoading" @click="loadRuns">读取运行列表</el-button>
          <el-select
            v-model="selectedRunId"
            size="small"
            class="toolbar__field"
            placeholder="选择 run"
            :disabled="runs.length === 0"
          >
            <el-option
              v-for="run in runs"
              :key="run.id"
              :label="runLabel(run)"
              :value="run.id"
            />
          </el-select>
          <el-button size="small" :loading="passportLoading" :disabled="selectedRunId === null" @click="loadPassport">
            读取 Passport
          </el-button>
          <span class="toolbar__meta">回放 / 重跑入口在 Workbench 的 Passport 面板（写操作按访问面禁用）</span>
        </div>

        <el-alert
          v-if="stopReasonHint"
          type="info"
          :closable="false"
          class="guardrail"
          :title="stopReasonHint"
        />

        <el-skeleton v-if="runsLoading || passportLoading" :rows="4" animated />
        <el-empty
          v-else-if="runs.length === 0"
          description="该项目暂无流水线运行记录（未获取 ≠ 0：请选择 Project 后重试）"
        />
        <el-empty v-else-if="!passport" :description="passportUnavailable ?? '选择 run 后点击「读取 Passport」'" />
        <section v-else class="passport">
          <div class="asset-detail__badges">
            <span class="chip">passport #{{ passport.id }}</span>
            <span class="chip" :class="passport.status === 'complete' ? 'chip--ok' : 'chip--warn'">
              status={{ passport.status }}
            </span>
            <span class="chip" :class="passport.is_replay ? 'chip--warn' : ''">
              is_replay={{ passport.is_replay ?? false }}
            </span>
            <span class="chip">parent {{ passport.parent_passport_id ?? '—' }}</span>
          </div>
          <table class="fields">
            <tbody>
              <tr>
                <th>dataset</th>
                <td>{{ passport.dataset_name ?? '—' }} · {{ passport.dataset_version ?? '—' }}</td>
              </tr>
              <tr>
                <th>dataset_sha256</th>
                <td class="mono">{{ passport.dataset_sha256 ?? '—' }}</td>
              </tr>
              <tr>
                <th>prompt_sha256</th>
                <td class="mono">{{ passport.prompt_sha256 ?? '—' }} · {{ passport.prompt_version ?? '版本未提供' }}</td>
              </tr>
              <tr>
                <th>code_commit_sha</th>
                <td class="mono">{{ passport.code_commit_sha ?? '—' }}</td>
              </tr>
              <tr>
                <th>dependency_lock_sha256</th>
                <td class="mono">{{ passport.dependency_lock_sha256 ?? '—' }}</td>
              </tr>
              <tr>
                <th>provider / model</th>
                <td>{{ passport.provider ?? '—' }} · {{ passport.model_id ?? '—' }}</td>
              </tr>
              <tr>
                <th>template</th>
                <td>{{ passport.template_id ?? '—' }}</td>
              </tr>
              <tr>
                <th>metrics</th>
                <td class="mono">{{ json(passport.metrics) }}</td>
              </tr>
              <tr>
                <th>cost_usd</th>
                <td>{{ passport.cost_usd === null || passport.cost_usd === undefined ? '—' : `$${Number(passport.cost_usd).toFixed(4)}` }}</td>
              </tr>
              <tr>
                <th>artifact_manifest</th>
                <td class="mono">{{ json(passport.artifact_manifest) }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<script lang="ts">
/**
 * 页面级只读常量：stop_reason 的中文说明与其展示位置（避免在模板内做逻辑推断）。
 */
export default { name: 'KnowledgeView' }
</script>

<style scoped>
.knowledge {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.knowledge__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.knowledge__head h1 {
  margin: 0;
  font-size: var(--font-size-xl);
}


.knowledge__head-side {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.knowledge__project {
  width: 280px;
}

.cost {
  display: flex;
  align-items: baseline;
  gap: var(--space-4);
  flex-wrap: wrap;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-md);
  background-color: var(--color-card-bg);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.cost__title {
  color: var(--color-text-primary);
  font-weight: 600;
}

.cost__line strong {
  font-family: var(--font-family-mono);
  font-size: var(--font-size-lg);
  color: var(--color-text-primary);
}

.cost__note {
  color: var(--color-text-disabled);
}

.knowledge__readonly {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.knowledge__tabs {
  padding: var(--space-2) var(--space-3) var(--space-4);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}

.toolbar__input {
  width: 320px;
}

.toolbar__field {
  width: 260px;
}

.toolbar__meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.split {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(420px, 2fr);
  gap: var(--space-3);
  align-items: start;
}

@media (max-width: 1100px) {
  .split {
    grid-template-columns: 1fr;
  }
}

.asset-list {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 640px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.asset-item {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  cursor: pointer;
}

.asset-item:last-child {
  border-bottom: none;
}

.asset-item:hover {
  background-color: var(--color-bg-subtle);
}

.asset-item--active {
  background-color: var(--color-brand-soft);
}

.asset-item__title {
  margin: 0;
  font-size: var(--font-size-sm);
  line-height: var(--line-height-tight);
}

.asset-item__meta {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.asset-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.asset-detail__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}

.asset-detail__head h3 {
  margin: 0;
  font-size: var(--font-size-md);
}

.asset-detail__badges {
  display: flex;
  gap: var(--space-1);
  flex-wrap: wrap;
  align-items: center;
}

.asset-detail__note {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.chip {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.chip--ok {
  border-color: var(--color-success);
  color: var(--color-success);
  background-color: var(--color-success-soft);
}

.chip--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.chip--policy-auto_execute {
  border-color: var(--color-success);
  color: var(--color-success);
}

.chip--policy-need_human {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.chip--policy-circuit_break {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.fields {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.fields th {
  width: 150px;
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  font-weight: 400;
  text-align: left;
  vertical-align: top;
}

.fields td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  vertical-align: top;
  overflow-wrap: anywhere;
}

.fields__text {
  display: block;
}

.fields__stat {
  display: block;
  margin-top: var(--space-1);
  color: var(--color-text-secondary);
  font-family: var(--font-family-mono);
}

.spans h4,
.decision-group h4 {
  margin: var(--space-3) 0 var(--space-2);
  font-size: var(--font-size-sm);
}

.spans ul,
.decision-group ul {
  margin: 0;
  padding: 0;
  list-style: none;
}

.spans li {
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-border);
}

.verdict {
  margin-right: var(--space-2);
  padding: 0 var(--space-1);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
}

.verdict--valid {
  border-color: var(--color-success);
  color: var(--color-success);
}

.verdict--valid_by_hash {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.verdict--invalid {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.quote,
.spans blockquote {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  border-left: 3px solid var(--color-border-strong);
  background-color: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  line-height: var(--line-height-base);
}

.evidence,
.passport {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.link {
  font-size: var(--font-size-xs);
  transition:
    color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    filter 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.link:hover {
  color: var(--color-brand);
  filter: brightness(1.1);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.link:active {
  filter: brightness(0.95);
}

.mono {
  font-family: var(--font-family-mono);
}

.decision-item {
  padding: var(--space-2);
  margin-bottom: var(--space-2);
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-brand);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-subtle);
}

.decision-item__head {
  display: flex;
  gap: var(--space-1);
  flex-wrap: wrap;
}

.decision-item__rationale {
  margin: var(--space-2) 0 var(--space-1);
  font-size: var(--font-size-sm);
}

.guardrail {
  margin-bottom: var(--space-3);
}
</style>
