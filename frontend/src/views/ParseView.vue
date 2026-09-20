<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 单篇论文解析页（WP07-T5）：左侧 8 字段卡片 + 右侧原文/摘要视图。
 *
 * 交互与诚实标注（计划书 §2.8 / WP05/WP06 边界）：
 * - 点击字段条目 → 右侧滚动到对应 char 区间并高亮（偏移口径与 paper_spans 一致，文档级）；
 * - 显示 document_version / parser / page_number；HTML 版本 page_number 恒为 1 且**不是物理页码**；
 * - 定位不可用（无 span / verdict=valid_by_hash）时提示「以引用文本为准」；
 * - 顶部显示证据覆盖范围；解析失败/未解析论文只给摘要级视图，不伪报已定位；
 * - 覆盖率与定位状态一律用接口真实值，缺失显示「未获取」。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { ApiError } from '@/api/client'
import { useSessionStore } from '@/stores/session'
import {
  CARD_FIELDS,
  fetchAllSpans,
  fetchCard,
  fetchDocuments,
  fetchPaperDetail,
  rebuildCard,
  fetchCardJob,
  type CardEntry,
  type CardFieldDef,
  type CardResponse,
  type DocumentsResponse,
  type PaperDetail,
  type PaperSpan,
} from '@/api/parse'

const route = useRoute()
const session = useSessionStore()

const paperId = computed(() => String(route.params.paperId ?? ''))

const paper = ref<PaperDetail | null>(null)
const paperError = ref<string | null>(null)
/** 论文详情失败的**真实** `ApiError.code`（不在前端编造代号；未取到时由组件显示 unknown_error） */
const paperErrorCode = ref<string | null>(null)
const card = ref<CardResponse | null>(null)
const cardError = ref<{ code: string; message: string } | null>(null)
const documents = ref<DocumentsResponse | null>(null)
const documentsError = ref<string | null>(null)
const spans = ref<PaperSpan[]>([])
const spansTotal = ref(0)
const spansTruncated = ref(false)
const spansLoading = ref(false)
const spansError = ref<string | null>(null)
const selectedVersion = ref<string | null>(null)
const selectedCardVersion = ref<number | null>(null)
const loading = ref(true)

/** 当前定位目标（字段条目 → 文档级 char 区间） */
interface LocatorTarget {
  fieldKey: string
  label: string
  entryIndex: number
  version: string
  start: number
  end: number
  verdict: string | null
  quote: string
  pageNumber: number | null
}
const target = ref<LocatorTarget | null>(null)
const notice = ref<string | null>(null)
const rebuilding = ref(false)
const rebuildMessage = ref<string | null>(null)
const rebuildError = ref<string | null>(null)

const versions = computed(() => card.value?.versions ?? [])

/** 卡片缺失（card_not_found）属「尚未产出」，与「加载失败」分开表达 */
const cardMissing = computed(() => cardError.value?.code === 'card_not_found')
const cardErrorText = computed(() => (cardMissing.value ? null : (cardError.value?.message ?? null)))
/** 权限拒绝：重新解析/建卡是写操作，public_demo 匿名面必然 403 */
const rebuildDenied = computed(() => !session.isOwner)

/** 标题下第一行：只给「作者 · 发布时间」，不列 id / 来源 / venue / 引用数（那些是检索面的事） */
const subMeta = computed(() => {
  const parts: string[] = []
  const names = (paper.value?.authors ?? [])
    .map((item) => (item?.name ?? '').trim())
    .filter(Boolean)
  if (names.length) parts.push(names.join('、'))
  if (paper.value?.published_at) parts.push(`发布时间 ${paper.value.published_at}`)
  return parts.join(' · ')
})

/**
 * 解析状态（三态，放在标题下第二行）：
 * ``parsing`` 建卡任务进行中 / ``parsed`` 卡片已产出（能解析论文就能解析成卡片，
 * 所以「已解析」就是「卡片已生成」）/ ``unparsed`` 其余情况。
 */
const parseState = computed<{ text: string; tone: 'ok' | 'busy' | 'muted' }>(() => {
  if (rebuilding.value) return { text: '解析中', tone: 'busy' }
  if (card.value) return { text: '已解析', tone: 'ok' }
  return { text: '未解析', tone: 'muted' }
})

/**
 * AI 总结：内容**全部取自 AI 产出的卡片字段**（研究问题 / 核心方法 / 主要结论），
 * 逐句可回溯，不做任何补写；卡片缺失时返回空串，由模板决定不渲染。
 */
const aiSummary = computed(() => {
  const payload = card.value?.card
  if (!payload) return []
  const rows: Array<{ label: string; text: string }> = []
  const research = String(payload.research_problem ?? '').trim()
  if (research && !isUnknown(research)) rows.push({ label: '研究问题', text: research })
  const method = String(payload.core_method ?? '').trim()
  if (method && !isUnknown(method)) rows.push({ label: '核心方法', text: method })
  const conclusions = Array.isArray(payload.main_conclusions) ? payload.main_conclusions : []
  const first = conclusions
    .map((item) => String(item?.conclusion ?? item?.point ?? '').trim())
    .filter((text) => text && !isUnknown(text))
    .join('；')
  if (first) rows.push({ label: '主要结论', text: first })
  return rows
})

const documentOptions = computed(() => {
  const list = (documents.value?.items ?? []).map((doc) => ({
    value: doc.document_version,
    label: `${doc.parser} · ${doc.source_type} · ${doc.parse_status}${
      doc.coverage === null ? '（覆盖率未获取）' : `（覆盖 ${Math.round(doc.coverage * 1000) / 10}%）`
    }`,
  }))
  const cardVersion = card.value?.document_version
  if (cardVersion && !list.some((item) => item.value === cardVersion)) {
    list.unshift({ value: cardVersion, label: `${cardVersion}（卡片定位版本）` })
  }
  return list
})

/** 当前高亮片段 */
const activeSpan = computed<PaperSpan | null>(() => {
  const current = target.value
  if (!current) return null
  return (
    spans.value.find(
      (span) =>
        span.document_version === current.version &&
        span.char_start <= current.start &&
        span.char_end >= current.end,
    ) ?? null
  )
})

/** 高亮片段内部的子区间（仅高亮字段引用的那一段） */
const markRange = computed<[number, number] | null>(() => {
  const span = activeSpan.value
  const current = target.value
  if (!span || !current) return null
  const start = Math.max(current.start, span.char_start) - span.char_start
  const end = Math.min(current.end, span.char_end) - span.char_start
  if (end <= start) return null
  return [start, end]
})

function spanParts(span: PaperSpan): { before: string; mark: string; after: string } {
  const range = span.id === activeSpan.value?.id ? markRange.value : null
  if (!range) return { before: span.quote_text, mark: '', after: '' }
  const [start, end] = range
  return {
    before: span.quote_text.slice(0, start),
    mark: span.quote_text.slice(start, end),
    after: span.quote_text.slice(end),
  }
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '未获取'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

function isUnknown(value: string | null | undefined): boolean {
  return !value || value.trim().toLowerCase() === 'unknown'
}

function entryText(entry: CardEntry, def: CardFieldDef): string {
  if (def.key === 'technical_route') {
    const label = entry.step ? `${entry.step}：` : ''
    return `${label}${entry.description ?? entry.point ?? entry.conclusion ?? '未获取'}`
  }
  if (def.textKey && entry[def.textKey]) return String(entry[def.textKey])
  return entry.point ?? entry.conclusion ?? entry.limitation ?? entry.description ?? '未获取'
}

function entriesOf(def: CardFieldDef): CardEntry[] {
  const value = card.value?.card?.[def.key]
  return Array.isArray(value) ? (value as CardEntry[]) : []
}

/** 字段定位状态（优先用后端 by_field 统计，缺失则本地统计）；无可说状态时返回空文本，模板不渲染 */
function fieldStatus(def: CardFieldDef): {
  text: string
  tone: 'ok' | 'warn' | 'muted'
  detail: string
} {
  if (def.kind === 'text' || def.kind === 'setup') {
    return {
      text: '',
      tone: 'muted',
      detail: '该字段为卡片聚合字段，未附行内引用条目 → 以卡片文本为准，无定位区间',
    }
  }
  const stats = card.value?.by_field?.[def.key as string]
  const entries = entriesOf(def)
  const total = stats?.total ?? entries.length
  const located = stats?.located ?? entries.filter((entry) => entry.evidence_span).length
  const unlocated = stats?.unlocated ?? Math.max(total - located, 0)
  if (total === 0) return { text: '', tone: 'muted', detail: '卡片未产出该字段条目' }
  if (located === 0) {
    return {
      text: `未定位 0/${total}`,
      tone: 'warn',
      detail: '所有条目未在原文中找到（证据范围受限）→ 以引用文本为准',
    }
  }
  if (unlocated > 0) {
    return {
      text: `部分定位 ${located}/${total}`,
      tone: 'warn',
      detail: `${unlocated} 条未定位，仅已定位条目可跳转原文`,
    }
  }
  return { text: `已定位 ${located}/${total}`, tone: 'ok', detail: '该字段条目均已定位到原文片段' }
}

function verdictLabel(entry: CardEntry): string {
  const verdict = entry.evidence_span?.verification?.verdict
  if (!verdict) return '未校验'
  switch (verdict) {
    case 'valid':
      return '精确校验通过'
    case 'valid_by_hash':
      return '哈希命中（偏移不可校验）'
    default:
      return '校验失败'
  }
}

async function locate(def: CardFieldDef, entry: CardEntry, index: number): Promise<void> {
  const span = entry.evidence_span
  if (!span) {
    target.value = null
    notice.value = `该条目未定位（evidence_note=${entry.evidence_note ?? '未提供'}）：定位不可用，以引用文本为准`
    return
  }
  if (span.document_version !== selectedVersion.value) {
    const exists = documentOptions.value.some((item) => item.value === span.document_version)
    if (exists) {
      selectedVersion.value = span.document_version
      await loadSpans()
    } else {
      notice.value = `条目定位版本（${span.document_version}）不在解析记录中，无法切换视图；以引用文本为准`
    }
  }
  target.value = {
    fieldKey: def.key as string,
    label: def.label,
    entryIndex: index,
    version: span.document_version,
    start: span.char_start,
    end: span.char_end,
    verdict: span.verification?.verdict ?? null,
    quote: span.quote_text,
    pageNumber: span.page_number ?? null,
  }
  notice.value =
    span.verification?.verdict === 'valid_by_hash'
      ? '精确定位不可用（全文缓存缺失，偏移不可校验）→ 已按哈希命中的片段高亮，以引用文本为准'
      : entry.evidence_note
        ? `定位方式：${entry.evidence_note}`
        : null
  await nextTick()
  scrollToActive()
}

function scrollToActive(): void {
  const span = activeSpan.value
  if (!span) return
  const element = document.querySelector<HTMLElement>(`[data-span-id="${span.id}"]`)
  element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function gotoSpan(span: PaperSpan): void {
  target.value = {
    fieldKey: 'manual',
    label: '手动查看片段',
    entryIndex: 0,
    version: span.document_version,
    start: span.char_start,
    end: span.char_end,
    verdict: span.verification?.verdict ?? null,
    quote: span.quote_text,
    pageNumber: span.page_number ?? null,
  }
}

async function loadSpans(): Promise<void> {
  if (!selectedVersion.value) {
    spans.value = []
    spansTotal.value = 0
    return
  }
  spansLoading.value = true
  spansError.value = null
  try {
    const result = await fetchAllSpans(paperId.value, {
      document_version: selectedVersion.value,
      maxPages: 6,
    })
    spans.value = result.items
    spansTotal.value = result.total
    spansTruncated.value = result.truncated
  } catch (error) {
    spans.value = []
    spansTotal.value = 0
    spansError.value = error instanceof ApiError ? error.message : (error as Error).message
  } finally {
    spansLoading.value = false
  }
}

async function loadCard(version?: number | null): Promise<void> {
  try {
    const response = await fetchCard(
      paperId.value,
      version ? { version } : {},
    )
    card.value = response
    cardError.value = null
    selectedCardVersion.value = response.version
    if (!selectedVersion.value && response.document_version) {
      selectedVersion.value = response.document_version
      await loadSpans()
    }
  } catch (error) {
    card.value = null
    if (error instanceof ApiError) {
      cardError.value = { code: error.code, message: error.message }
    } else {
      cardError.value = { code: 'unknown_error', message: (error as Error).message }
    }
  }
}

async function loadAll(): Promise<void> {
  loading.value = true
  paperError.value = null
  paperErrorCode.value = null
  documentsError.value = null
  target.value = null
  notice.value = null
  try {
    paper.value = await fetchPaperDetail(paperId.value)
  } catch (error) {
    paper.value = null
    paperError.value = error instanceof ApiError ? error.message : (error as Error).message
    paperErrorCode.value = error instanceof ApiError ? error.code : 'unknown_error'
  }
  try {
    documents.value = await fetchDocuments(paperId.value)
    if (documents.value.summary?.document_version) {
      selectedVersion.value = documents.value.summary.document_version
    }
  } catch (error) {
    documents.value = null
    documentsError.value = error instanceof ApiError ? error.message : (error as Error).message
  }
  await loadCard(null)
  if (!selectedVersion.value) {
    const first = documents.value?.items?.[0]?.document_version ?? null
    selectedVersion.value = first
  }
  await loadSpans()
  loading.value = false
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/** 重新解析：POST /papers/{id}/card（owner 面，长任务）→ 有界轮询 → 重新读取卡片 */
async function rebuild(): Promise<void> {
  rebuilding.value = true
  rebuildMessage.value = null
  rebuildError.value = null
  try {
    const job = await rebuildCard(paperId.value, true)
    rebuildMessage.value = `已提交重解析任务 task_id=${job.task_id}（长任务：结果以卡片接口为准，不用响应体冒充结果）`
    let finished = false
    for (let attempt = 0; attempt < 20 && !finished; attempt += 1) {
      await sleep(1500)
      try {
        const jobStatus = await fetchCardJob(paperId.value, job.task_id)
        if (jobStatus.status === 'done' || jobStatus.status === 'failed') {
          finished = true
          rebuildMessage.value = `${rebuildMessage.value}；任务状态=${jobStatus.status}`
        }
      } catch {
        // 任务登记在进程内，重启会丢失：直接以卡片接口为准，不阻塞用户
        finished = true
      }
    }
    await loadCard(null)
  } catch (error) {
    if (error instanceof ApiError && error.isForbidden) {
      rebuildError.value =
        '重新解析需要 Owner 令牌（X-Owner-Token）：当前为 public_demo 只读面，写操作被拒绝（403 owner_token_required）'
    } else if (error instanceof ApiError) {
      rebuildError.value = `重新解析失败（${error.code}）：${error.message}`
    } else {
      rebuildError.value = `重新解析失败：${(error as Error).message}`
    }
  } finally {
    rebuilding.value = false
  }
}

async function changeCardVersion(version: number): Promise<void> {
  await loadCard(version)
}

/** 「重试」统一入口：重新拉论文详情 / 解析记录 / 卡片 / 片段（不伪造进度） */
async function retryAll(): Promise<void> {
  await loadAll()
}

/** 单卡片重试：卡片接口失败时只重取卡片（不动已取到的其它数据） */
async function retryCard(): Promise<void> {
  if (cardMissing.value) {
    notice.value = rebuildDenied.value
      ? '该论文尚无解析卡片；建卡属 Owner 写操作，当前为 public_demo 只读面（403 owner_token_required）'
      : '该论文尚无解析卡片：可点击右上角「解析并重建卡片」生成 version=1'
    return
  }
  await loadCard(selectedCardVersion.value)
}

watch(
  () => route.params.paperId,
  () => {
    void loadAll()
  },
)

onMounted(() => {
  void loadAll()
})
</script>

<template>
  <section class="parse">


    <header class="parse__head">
      <div class="parse__title-block">
        <h1>
          <template v-if="paper">{{ paper.title }}</template>
          <template v-else>论文解析 #{{ paperId }}</template>
        </h1>
        <p v-if="subMeta" class="parse__meta">{{ subMeta }}</p>
        <p class="parse__status">
          <span class="status-tag" :class="`status-tag--${parseState.tone}`">{{ parseState.text }}</span>
        </p>
      </div>
      <div class="parse__actions">
        <el-select
          v-if="versions.length > 1"
          :model-value="selectedCardVersion"
          size="small"
          class="parse__version"
          @update:model-value="(v: number) => changeCardVersion(Number(v))"
        >
          <el-option
            v-for="item in versions"
            :key="item.version"
            :label="`卡片 v${item.version}（${formatTime(item.created_at)}）`"
            :value="item.version"
          />
        </el-select>
        <el-tooltip
          :content="
            session.isOwner
              ? 'force=true：生成 version+1 的新卡片，旧版本保留'
              : 'public_demo 只读面不允许写操作：需 Owner 令牌'
          "
          placement="bottom"
        >
          <el-button
            type="primary"
            size="small"
            :loading="rebuilding"
            :disabled="rebuildDenied"
            data-action="rebuild-card"
            @click="rebuild"
          >
            解析并重建卡片
          </el-button>
        </el-tooltip>
      </div>
    </header>

    <!-- 六类状态：loading / error / permission denied / retry（empty 见「卡片未获取」提示） -->
    <ViewStatePanel
      :loading="loading || spansLoading"
      loading-text="正在加载论文详情 / 解析记录 / 卡片 / 原文片段…"
      :error="paperError"
      :error-code="paperErrorCode"
      error-title="论文详情加载失败"
      :permission-denied="rebuildDenied && Boolean(rebuildError)"
      :permission-note="rebuildError"
      retryable
      retry-label="重试加载解析页"
      :busy="loading"
      @retry="retryAll"
    />

    <el-alert
      v-if="rebuildMessage || rebuildError"
      class="parse__alert"
      :type="rebuildError ? 'error' : 'success'"
      :closable="false"
      show-icon
      :title="rebuildError ?? rebuildMessage ?? ''"
    />

    <el-alert
      v-if="cardErrorText"
      class="parse__alert"
      type="error"
      :closable="false"
      show-icon
      :title="`解析卡片未获取（${cardError?.code ?? 'unknown_error'}）：${cardErrorText}`"
    >
      <template #default>
        <span class="parse__alert-body">
          未建卡时不会显示任何字段内容（缺失不编造）。
        </span>
        <el-button size="small" text type="primary" @click="retryCard">重试读取卡片</el-button>
      </template>
    </el-alert>

    <div class="parse__grid">
      <!-- 左：8 字段卡片 -->
      <div class="parse__fields">
        <el-skeleton v-if="loading" :rows="8" animated />
        <template v-else>
          <section
            v-for="def in CARD_FIELDS"
            :key="def.key as string"
            class="field-card sl-card"
            :data-field-key="def.key as string"
          >
            <header class="field-card__head">
              <h2>{{ def.label }}</h2>
              <span
                v-if="fieldStatus(def).text"
                class="field-status"
                :class="`field-status--${fieldStatus(def).tone}`"
                :title="fieldStatus(def).detail"
              >
                {{ fieldStatus(def).text }}
              </span>
            </header>

            <!-- 单值字段 -->
            <template v-if="def.kind === 'text'">
              <p v-if="!card" class="field-empty">卡片未获取</p>
              <p v-else class="field-text">
                <span v-if="isUnknown(String(card.card[def.key] ?? ''))" class="missing">未获取</span>
                <template v-else>{{ card.card[def.key] }}</template>
              </p>
            </template>

            <!-- 实验设置 -->
            <template v-else-if="def.kind === 'setup'">
              <template v-if="card">
                <dl class="setup">
                  <div class="setup__row">
                    <dt>metrics</dt>
                    <dd>
                      <span
                        v-for="metric in card.card.experimental_setup.metrics ?? []"
                        :key="`m-${metric}`"
                        class="chip"
                        :class="{ 'chip--unknown': isUnknown(metric) }"
                      >
                        {{ isUnknown(metric) ? '未报告' : metric }}
                      </span>
                      <span v-if="!(card.card.experimental_setup.metrics ?? []).length" class="missing">
                        未获取
                      </span>
                    </dd>
                  </div>
                  <div class="setup__row">
                    <dt>datasets</dt>
                    <dd>
                      <span
                        v-for="dataset in card.card.experimental_setup.datasets ?? []"
                        :key="`d-${dataset}`"
                        class="chip"
                        :class="{ 'chip--unknown': isUnknown(dataset) }"
                      >
                        {{ isUnknown(dataset) ? '未报告' : dataset }}
                      </span>
                      <span v-if="!(card.card.experimental_setup.datasets ?? []).length" class="missing">
                        未获取
                      </span>
                    </dd>
                  </div>
                  <div class="setup__row">
                    <dt>baselines</dt>
                    <dd>
                      <span
                        v-for="baseline in card.card.experimental_setup.baselines ?? []"
                        :key="`b-${baseline}`"
                        class="chip"
                        :class="{ 'chip--unknown': isUnknown(baseline) }"
                      >
                        {{ isUnknown(baseline) ? '未报告' : baseline }}
                      </span>
                      <span v-if="!(card.card.experimental_setup.baselines ?? []).length" class="missing">
                        未获取
                      </span>
                    </dd>
                  </div>
                </dl>
                <p v-if="card.unknown_fields.length" class="field-note sl-source-tag">
                  卡片自报未报告项：{{ card.unknown_fields.join('、') }}
                </p>
              </template>
              <p v-else class="field-empty">卡片未获取</p>
            </template>

            <!-- 列表型字段（可定位） -->
            <template v-else>
              <p v-if="!card" class="field-empty">卡片未获取</p>
              <ol v-else class="entry-list">
                <li
                  v-for="(entry, index) in entriesOf(def)"
                  :key="`${def.key as string}-${index}`"
                  class="entry-item"
                  :class="{
                    'entry-item--located': Boolean(entry.evidence_span),
                    'entry-item--active':
                      target?.fieldKey === (def.key as string) && target?.entryIndex === index,
                  }"
                  :data-field-key="def.key as string"
                  :data-entry-index="index"
                  :data-located="Boolean(entry.evidence_span)"
                >
                  <button
                    class="entry-item__text"
                    type="button"
                    @click="locate(def, entry, index)"
                  >
                    {{ entryText(entry, def) }}
                  </button>
                  <div class="entry-item__meta">
                    <span class="verdict" :class="`verdict--${entry.evidence_span?.verification?.verdict ?? 'none'}`">
                      {{ verdictLabel(entry) }}
                    </span>
                    <span v-if="entry.evidence_span" class="sl-source-tag">
                      char[{{ entry.evidence_span.char_start }}:{{ entry.evidence_span.char_end }}] ·
                      {{ entry.evidence_span.section_name ?? '章节未获取' }} · 页
                      {{ entry.evidence_span.page_number ?? '未获取' }}
                    </span>
                    <span v-else class="missing">未定位（{{ entry.evidence_note ?? '原因未提供' }}）</span>
                  </div>
                </li>
              </ol>
            </template>
          </section>
        </template>
      </div>

      <!-- 右：原文片段定位（无可定位片段时退化为 AI 总结，不放原始摘要） -->
      <aside class="parse__source sl-card scroll-y">
        <header class="source-head">
          <h2>原文片段视图</h2>
          <el-select
            v-if="documentOptions.length"
            :model-value="selectedVersion"
            size="small"
            class="source-head__version"
            @update:model-value="(v: string) => { selectedVersion = v; target = null; void loadSpans() }"
          >
            <el-option
              v-for="item in documentOptions"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </header>

        <p class="locator-status" data-role="locator-status">
          <template v-if="target">
            已定位：{{ target.label }}[{{ target.entryIndex }}] → document_version={{
              target.version
            }} char[{{ target.start }}:{{ target.end }}] verdict={{ target.verdict ?? '未校验' }}
          </template>
          <template v-else-if="spans.length">未选中字段（点击左侧条目以定位并高亮原文片段）</template>
          <template v-else>无可定位片段</template>
        </p>

        <el-alert
          v-if="notice"
          class="source-notice"
          type="info"
          :closable="false"
          show-icon
          :title="notice"
        />

        <el-alert
          v-if="spansError"
          class="source-notice"
          type="error"
          :closable="false"
          show-icon
          :title="`片段加载失败：${spansError}`"
        >
          <template #default>
            <el-button size="small" text type="primary" @click="loadSpans">重试片段</el-button>
          </template>
        </el-alert>

        <el-alert
          v-if="documentsError"
          class="source-notice"
          type="warning"
          :closable="false"
          show-icon
          :title="`解析记录未获取：${documentsError}`"
        >
          <template #default>
            <el-button size="small" text type="primary" @click="retryAll">重试解析记录</el-button>
          </template>
        </el-alert>

        <el-skeleton v-if="spansLoading" :rows="6" animated />

        <!-- 有可定位片段：原文片段视图 -->
        <div v-else-if="spans.length" class="source-body">
          <article
            v-for="span in spans"
            :key="span.id"
            class="span-block"
            :class="{ 'span-block--active': activeSpan?.id === span.id }"
            :data-span-id="span.id"
            @click="gotoSpan(span)"
          >
            <div class="span-block__meta sl-source-tag">
              <span class="chip chip--section">{{ span.section_name ?? 'other' }}</span>
              <span>char[{{ span.char_start }}:{{ span.char_end }}]</span>
              <span>页 {{ span.page_number ?? '未获取' }}</span>
              <span>verdict={{ span.verification?.verdict ?? '未获取' }}</span>
            </div>
            <p class="span-block__text">
              <template v-if="activeSpan?.id === span.id && markRange">
                {{ spanParts(span).before }}<mark class="hl">{{ spanParts(span).mark }}</mark
                >{{ spanParts(span).after }}
              </template>
              <template v-else>{{ span.quote_text }}</template>
            </p>
            <p v-if="activeSpan?.id === span.id" class="span-block__reason sl-source-tag">
              {{ span.verification?.reason ?? '未提供校验理由' }}
            </p>
          </article>
        </div>

        <!-- 无可定位片段：AI 总结（内容全部取自 AI 产出的卡片字段，不展示原始摘要） -->
        <div v-else-if="!spansLoading" class="source-abstract">
          <h3>AI 总结</h3>
          <template v-if="aiSummary.length">
            <div v-for="row in aiSummary" :key="row.label" class="summary-row">
              <span class="summary-row__label">{{ row.label }}</span>
              <p class="summary-row__text">{{ row.text }}</p>
            </div>
          </template>
          <p v-else class="summary-empty">该论文尚未生成解析卡片</p>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.parse {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.parse__head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

.parse__title-block {
  flex: 1;
  min-width: 0;
}

.parse__title-block h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xl);
  line-height: var(--line-height-tight);
}

.parse__meta {
  margin: 0;
}

.parse__actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.parse__status {
  margin: var(--space-1) 0 0;
}

/* 解析状态：三态徽标（未解析 / 解析中 / 已解析），不用灰色小字 */
.status-tag {
  display: inline-flex;
  align-items: center;
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.status-tag--ok {
  border-color: var(--color-success);
  color: var(--color-success);
}

.status-tag--busy {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.parse__version {
  width: 220px;
}

.parse__alert-body {
  font-size: var(--font-size-xs);
}

.parse__grid {
  display: grid;
  grid-template-columns: minmax(360px, 5fr) minmax(420px, 7fr);
  gap: var(--space-3);
  align-items: start;
}

.parse__fields {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.field-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

.field-card__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.field-card__head h2 {
  margin: 0;
  font-size: var(--font-size-md);
}

.field-status {
  margin-left: auto;
  padding: 0 var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.field-status--ok {
  border-color: var(--color-success);
  color: var(--color-success);
  background-color: var(--color-success-soft);
}

.field-status--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.field-status--muted {
  border-style: dashed;
}

.field-text {
  margin: 0;
  font-size: var(--font-size-sm);
}

.field-empty,
.field-note {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.setup {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
}

.setup__row {
  display: flex;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
}

.setup__row dt {
  flex: 0 0 72px;
  color: var(--color-text-secondary);
}

.setup__row dd {
  display: flex;
  gap: var(--space-1);
  flex-wrap: wrap;
  margin: 0;
}

.chip {
  padding: 0 var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-subtle);
  font-size: var(--font-size-xs);
}

.chip--unknown {
  border-style: dashed;
  color: var(--color-warning);
}

.chip--section {
  border-color: var(--color-brand);
  color: var(--color-brand);
  background-color: var(--color-brand-soft);
}

.entry-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding-left: var(--space-4);
}

.entry-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-2);
  border-left: 2px solid var(--color-border);
}

.entry-item--located {
  border-left-color: var(--color-brand);
}

.entry-item--active {
  background-color: var(--color-brand-soft);
}

.entry-item__text {
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text-primary);
  font-family: var(--font-family-base);
  font-size: var(--font-size-sm);
  line-height: var(--line-height-base);
  text-align: left;
  cursor: pointer;
}

.entry-item__text:hover {
  color: var(--color-brand);
  text-decoration: underline;
}

.entry-item__meta {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  align-items: center;
  font-size: var(--font-size-xs);
}

.verdict {
  padding: 0 var(--space-1);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}

.verdict--valid {
  border-color: var(--color-success);
  color: var(--color-success);
  background-color: var(--color-success-soft);
}

.verdict--valid_by_hash {
  border-color: var(--color-warning);
  color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.verdict--invalid {
  border-color: var(--color-danger);
  color: var(--color-danger);
  background-color: var(--color-danger-soft);
}

.parse__source {
  position: sticky;
  top: calc(var(--layout-header-height) + var(--space-3));
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: calc(100vh - var(--layout-header-height) - var(--layout-footer-height) - var(--space-6));
  padding: var(--space-3);
  overflow-y: auto;
}

.source-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.source-head h2 {
  margin: 0;
  font-size: var(--font-size-md);
}

.source-head__version {
  margin-left: auto;
  width: 260px;
}

.locator-status {
  margin: 0;
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-subtle);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.source-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.source-hint {
  margin: 0;
}

.span-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-page);
  cursor: pointer;
}

.span-block--active {
  border-color: var(--color-brand);
  background-color: var(--color-brand-soft);
}

.span-block__meta {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  align-items: center;
}

.span-block__text {
  margin: 0;
  font-size: var(--font-size-sm);
}

.hl {
  padding: 0 2px;
  background-color: var(--color-warning-soft);
  color: var(--color-text-primary);
  box-shadow: inset 0 -2px 0 var(--color-warning);
}

.span-block__reason {
  margin: 0;
}

.source-abstract {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.source-abstract h3 {
  margin: 0;
  font-size: var(--font-size-sm);
}

.summary-row {
  display: flex;
  gap: var(--space-2);
  align-items: baseline;
}

.summary-row__label {
  flex: 0 0 64px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.summary-row__text {
  margin: 0;
  flex: 1;
  min-width: 0;
  font-size: var(--font-size-sm);
  line-height: var(--line-height-base);
}

.summary-empty {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}

.missing {
  color: var(--color-warning);
}
</style>
