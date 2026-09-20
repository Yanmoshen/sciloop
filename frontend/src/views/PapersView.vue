<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 文献调研 · 文献总览（进入文献调研后的第一个界面）。
 *
 * 定位：论文库总览 —— 后台定时拉取的论文、以及单篇深度解析 / 多篇聚合解析的发起处。
 *
 * 口径纪律：
 * - 统计数字全部来自 `GET /papers/overview` 的真实计数，`null` 显示「未获取」，不显示 0；
 * - 关键词与领域走服务端检索；来源 / 解析状态 / 排序为**本页筛选**（下拉里已标注「本页」）；
 * - 写操作（触发抓取、建卡、创建聚合）都是 Owner 面，403 时如实提示需要 OWNER_TOKEN。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { createAggregation } from '@/api/idea'
import {
  fetchPapersOverview,
  searchPapers,
  type PapersOverview,
  type PaperSearchItem,
} from '@/api/papers'
import { rebuildCard } from '@/api/parse'
import SmoothSelect from '@/components/SmoothSelect.vue'
import TaskMonitorDialog from '@/components/TaskMonitorDialog.vue'
import { useSessionStore } from '@/stores/session'
import { useTaskStore } from '@/stores/tasks'

const router = useRouter()
const session = useSessionStore()

const PAGE_SIZE = 20
const FIELD_OPTIONS = ['cs.AI', 'cs.CL', 'cs.CV', 'cs.LG']

/** 筛选区下拉项（平滑下拉需要 {value,label} 结构，原生 option 的文案原样搬过来） */
const FIELD_SELECT_OPTIONS = [
  { value: '', label: '全部领域' },
  ...FIELD_OPTIONS.map((item) => ({ value: item, label: item })),
]
const SOURCE_SELECT_OPTIONS = [
  { value: '', label: '全部来源（本页）' },
  { value: 'arxiv', label: 'arXiv' },
  { value: 'semantic_scholar', label: 'Semantic Scholar' },
  { value: 'openalex', label: 'OpenAlex' },
]
const PARSE_SELECT_OPTIONS = [
  { value: '', label: '全部解析状态（本页）' },
  { value: 'parsed', label: '已解析' },
  { value: 'unparsed', label: '未解析' },
]
const SORT_SELECT_OPTIONS = [
  { value: 'published', label: '按发表时间（本页）' },
  { value: 'citation', label: '按引用数（本页）' },
]

const overview = ref<PapersOverview | null>(null)
const overviewError = ref('')

const items = ref<PaperSearchItem[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const listError = ref('')

const keyword = ref('')
const field = ref('')
const sourceFilter = ref('')
const parseFilter = ref('')
const sortKey = ref<'published' | 'citation'>('published')

const selected = ref<number[]>([])
const busy = ref('')
const notice = ref('')

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

/** 本页筛选 + 排序（服务端检索结果默认按发表时间倒序） */
const visibleItems = computed(() => {
  let rows = [...items.value]
  if (sourceFilter.value) rows = rows.filter((row) => (row.source ?? '') === sourceFilter.value)
  if (parseFilter.value === 'parsed') rows = rows.filter((row) => row.is_parsed === true)
  if (parseFilter.value === 'unparsed') rows = rows.filter((row) => row.is_parsed !== true)
  if (sortKey.value === 'citation') {
    rows.sort((a, b) => (b.citation_count ?? -1) - (a.citation_count ?? -1))
  }
  return rows
})

const allOnPageSelected = computed(
  () =>
    visibleItems.value.length > 0 &&
    visibleItems.value.every((row) => selected.value.includes(row.id)),
)

function num(value: number | null | undefined): string {
  return value === null || value === undefined ? '未获取' : String(value)
}

function sourceLabel(source: string | null | undefined): string {
  const map: Record<string, string> = {
    arxiv: 'arXiv',
    semantic_scholar: 'Semantic Scholar',
    openalex: 'OpenAlex',
    github: 'GitHub',
  }
  return map[source ?? ''] ?? (source || '未记录')
}

/**
 * 论文链接：标题下面**只给一个真实可点的链接**；接口没给链接就整行不渲染
 * （不再打印「venue 未获取 / 引用 未获取」这类占位字）。
 * 优先 arXiv 摘要页（比裸 PDF 更适合阅读）→ DOI 解析 → 接口给的 pdf_url。
 */
function paperLink(row: PaperSearchItem): string | null {
  if (row.source === 'arxiv' && row.external_id) {
    return `https://arxiv.org/abs/${row.external_id}`
  }
  if (row.doi) return `https://doi.org/${row.doi}`
  if (row.pdf_url) return row.pdf_url
  return null
}

function parseBadge(row: PaperSearchItem): { text: string; cls: string } {
  if (row.is_parsed === true) return { text: '已解析', cls: 'badge--ok' }
  if (row.is_parsed === false) return { text: '未解析', cls: 'badge--mute' }
  return { text: '未获取', cls: 'badge--mute' }
}

async function loadOverview(): Promise<void> {
  try {
    overview.value = await fetchPapersOverview()
    overviewError.value = ''
  } catch (error) {
    overview.value = null
    overviewError.value = error instanceof Error ? error.message : String(error)
  }
}

async function loadList(): Promise<void> {
  loading.value = true
  listError.value = ''
  try {
    const data = await searchPapers({
      q: keyword.value.trim(),
      field: field.value,
      page: page.value,
      pageSize: PAGE_SIZE,
    })
    items.value = data.items ?? []
    total.value = data.total ?? 0
  } catch (error) {
    items.value = []
    total.value = 0
    listError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

function applyFilters(): void {
  page.value = 1
  selected.value = []
  void loadList()
}

function changePage(next: number): void {
  if (next < 1 || next > pageCount.value) return
  page.value = next
  selected.value = []
  void loadList()
}

function toggleRow(id: number): void {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((item) => item !== id)
    : [...selected.value, id]
}

function toggleAllOnPage(): void {
  const ids = visibleItems.value.map((row) => row.id)
  selected.value = allOnPageSelected.value
    ? selected.value.filter((id) => !ids.includes(id))
    : Array.from(new Set([...selected.value, ...ids]))
}

function clearSelection(): void {
  selected.value = []
}

function openParse(id: number): void {
  void router.push({ path: `/papers/parse/${id}` })
}

// ---- 任务监控：轮询与控制统一由 stores/tasks 负责（顶栏「任务」入口复用同一份实现）----
const tasks = useTaskStore()
const monitorOpen = ref(false)

async function sync(): Promise<void> {
  if (busy.value) return
  busy.value = 'sync'
  notice.value = ''
  try {
    monitorOpen.value = true
    const result = await tasks.startFetch(30)
    if (!result.ok) {
      notice.value =
        result.status === 403
          ? 'public_demo 只读面无法触发抓取（服务端 403）：请在「设置」页填入 OWNER_TOKEN 后重试。'
          : result.message
      return
    }
    await Promise.all([loadOverview(), loadList()])
  } finally {
    busy.value = ''
  }
}

/** 暂停 / 继续 / 终止（Owner 面；无权限或已结束会如实提示） */
async function controlTask(kind: 'pause' | 'resume' | 'cancel'): Promise<void> {
  const result = await tasks.control(kind)
  if (result.ok) {
    if (kind === 'cancel') await Promise.all([loadOverview(), loadList()])
    return
  }
  notice.value =
    result.status === 403
      ? 'public_demo 只读面无法操作任务（服务端 403）：请在「设置」页填入 OWNER_TOKEN 后重试。'
      : result.status === 409
        ? '任务已经结束，无需再操作（可点重新同步再跑一轮）。'
        : (result.message ?? '操作失败')
}

async function aggregateSelected(): Promise<void> {
  if (busy.value || selected.value.length < 2) return
  busy.value = 'aggregate'
  notice.value = ''
  try {
    const aggregation = await createAggregation({
      paper_ids: selected.value,
      project_id: session.currentProjectId,
    })
    await router.push({ name: 'aggregate', params: { id: String(aggregation.id) } })
  } catch (error) {
    notice.value =
      (error as { status?: number })?.status === 403
        ? 'public_demo 只读面无法创建聚合（服务端 403）：请在「设置」页填入 OWNER_TOKEN 后重试。'
        : error instanceof Error
          ? error.message
          : String(error)
  } finally {
    busy.value = ''
  }
}

async function buildCardsForSelected(): Promise<void> {
  if (busy.value || selected.value.length === 0) return
  busy.value = 'cards'
  notice.value = ''
  let ok = 0
  const failed: number[] = []
  for (const id of selected.value) {
    try {
      await rebuildCard(id, true)
      ok += 1
    } catch {
      failed.push(id)
    }
  }
  notice.value =
    failed.length === 0
      ? `已提交 ${ok} 篇的建卡任务（后台执行）。`
      : `已提交 ${ok} 篇；${failed.length} 篇失败（#${failed.join('、#')}）——若为 403，请在「设置」页填入 OWNER_TOKEN。`
  busy.value = ''
}

onMounted(() => {
  void loadOverview()
  void loadList()
})
</script>

<template>
  <section class="papers">
    <header class="papers__head">
      <h1>文献总览</h1>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <!-- 统计条：全部为库内真实计数，null 显示「未获取」 -->
    <div class="stats">
      <div class="stat">
        <div class="stat__label">论文总量</div>
        <div class="stat__value">{{ overview ? num(overview.papers_total) : '—' }}</div>
      </div>
      <div class="stat">
        <div class="stat__label">已解析全文</div>
        <div class="stat__value">{{ overview ? num(overview.documents_ok) : '—' }}</div>
      </div>
      <div class="stat">
        <div class="stat__label">已生成解析卡片</div>
        <div class="stat__value">{{ overview ? num(overview.cards_total) : '—' }}</div>
      </div>
      <div class="stat">
        <div class="stat__label">多篇聚合解析</div>
        <div class="stat__value">{{ overview ? num(overview.aggregations_total) : '—' }}</div>
      </div>
    </div>
    <p v-if="overviewError" class="hint hint--err">统计获取失败：{{ overviewError }}</p>

    <!-- 检索与筛选 -->
    <div class="filters">
      <input
        v-model="keyword"
        class="input"
        placeholder="搜索标题 / 摘要"
        @keyup.enter="applyFilters"
      />
      <SmoothSelect v-model="field" :options="FIELD_SELECT_OPTIONS" @change="applyFilters" />
      <SmoothSelect v-model="sourceFilter" :options="SOURCE_SELECT_OPTIONS" />
      <SmoothSelect v-model="parseFilter" :options="PARSE_SELECT_OPTIONS" />
      <SmoothSelect v-model="sortKey" :options="SORT_SELECT_OPTIONS" />
      <button class="btn" type="button" @click="applyFilters">检索</button>
    </div>

    <p v-if="listError" class="hint hint--err">列表获取失败：{{ listError }}</p>

    <table class="table">
      <thead>
        <tr>
          <th class="col-check">
            <input
              type="checkbox"
              :checked="allOnPageSelected"
              :disabled="visibleItems.length === 0"
              @change="toggleAllOnPage"
            />
          </th>
          <th>论文</th>
          <th class="col-source">来源</th>
          <th class="col-date">发表时间</th>
          <th class="col-cite">引用</th>
          <th class="col-parse">解析状态</th>
          <th class="col-ops">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading">
          <td colspan="7" class="empty">加载中…</td>
        </tr>
        <tr v-else-if="visibleItems.length === 0">
          <td colspan="7" class="empty">
            没有符合条件的论文：可放宽筛选，或点「立即同步」触发一轮抓取。
          </td>
        </tr>
        <tr
          v-for="row in visibleItems"
          v-else
          :key="row.id"
          :class="{ 'is-selected': selected.includes(row.id) }"
        >
          <td class="col-check">
            <input
              type="checkbox"
              :checked="selected.includes(row.id)"
              @change="toggleRow(row.id)"
            />
          </td>
          <td class="title-cell">
            <a href="#" @click.prevent="openParse(row.id)">{{ row.title }}</a>
            <a
              v-if="paperLink(row)"
              class="title-cell__link"
              :href="paperLink(row) ?? '#'"
              target="_blank"
              rel="noopener noreferrer"
            >{{ paperLink(row) }}</a>
          </td>
          <td>{{ sourceLabel(row.source) }}</td>
          <td>{{ row.published_at ?? '未获取' }}</td>
          <td>{{ num(row.citation_count) }}</td>
          <td><span class="badge" :class="parseBadge(row).cls">{{ parseBadge(row).text }}</span></td>
          <td class="row-actions">
            <button class="link-btn" type="button" @click="openParse(row.id)">深度解析</button>
            <button class="link-btn link-btn--mute" type="button" @click="toggleRow(row.id)">
              {{ selected.includes(row.id) ? '移出聚合' : '加入聚合' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="foot">
      <span>共 {{ total }} 篇 · 第 {{ page }} / {{ pageCount }} 页</span>
      <div class="pager">
        <button class="btn" type="button" :disabled="page <= 1 || loading" @click="changePage(page - 1)">
          上一页
        </button>
        <button
          class="btn"
          type="button"
          :disabled="page >= pageCount || loading"
          @click="changePage(page + 1)"
        >
          下一页
        </button>
      </div>
    </div>

    <div v-if="selected.length > 0" class="bulk">
      已选 <b>{{ selected.length }}</b> 篇
      <button
        class="btn btn--primary"
        type="button"
        :disabled="selected.length < 2 || busy === 'aggregate'"
        :title="selected.length < 2 ? '至少选择 2 篇才能聚合' : 'POST /aggregations'"
        @click="aggregateSelected"
      >
        {{ busy === 'aggregate' ? '聚合中…' : '多篇聚合解析' }}
      </button>
      <button class="btn" type="button" :disabled="busy === 'cards'" @click="buildCardsForSelected">
        {{ busy === 'cards' ? '提交中…' : '批量深度解析' }}
      </button>
      <button class="link-btn link-btn--mute" type="button" @click="clearSelection">取消选择</button>
    </div>

    <TaskMonitorDialog
      v-model="monitorOpen"
      :job="tasks.activeJob"
      @retry="sync"
      @pause="controlTask('pause')"
      @resume="controlTask('resume')"
      @cancel="controlTask('cancel')"
    />
  </section>
</template>

<style scoped>
.papers {
  padding: var(--space-4);
}

.papers__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

.papers__head h1 {
  margin: 0;
  font-size: var(--font-size-xxl);
}

.notice {
  margin: 0 0 var(--space-4);
  padding: var(--space-2) var(--space-3);
  border-left: 3px solid var(--color-brand);
  border-radius: var(--radius-sm);
  background: var(--color-brand-soft);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.stat {
  padding: var(--space-4);
  background: var(--color-card-bg);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
}

.stat__label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.stat__value {
  margin-top: var(--space-1);
  font-size: var(--font-size-xxl);
  line-height: 1.2;
  color: var(--color-text-primary);
}

.card {
  padding: var(--space-4);
  margin-bottom: var(--space-4);
  background: var(--color-card-bg);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
}

.card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.card__title {
  margin: 0;
  font-size: var(--font-size-lg);
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
  margin-bottom: var(--space-3);
}

.input,
.select {
  height: 32px;
  padding: 0 var(--space-3);
  background: var(--color-bg-page);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
}

.input {
  min-width: 240px;
}

.hint {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.hint--err {
  color: var(--color-danger);
}

.table {
  width: 100%;
  border-collapse: collapse;
  background: var(--color-card-bg);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-card);
}

.table th,
.table td {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  font-size: var(--font-size-sm);
  vertical-align: top;
}

.table th {
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 500;
}

.table tbody tr:hover {
  background: var(--color-bg-subtle);
}

.table tbody tr.is-selected {
  background: var(--color-brand-soft);
}

.col-check {
  width: 36px;
}
.col-source {
  width: 120px;
}
.col-date {
  width: 104px;
}
.col-cite {
  width: 72px;
}
.col-parse {
  width: 96px;
}
.col-ops {
  width: 150px;
}

.title-cell {
  max-width: 520px;
}

.title-cell a {
  color: var(--color-brand);
}

.title-cell__link {
  display: block;
  margin-top: 2px;
  font-size: var(--font-size-xs);
  word-break: break-all;
}

.title-cell__link:hover {
  text-decoration: underline;
}

.empty {
  padding: var(--space-5);
  color: var(--color-text-secondary);
  text-align: center;
}

.badge {
  display: inline-flex;
  padding: 1px var(--space-2);
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
  font-size: var(--font-size-xs);
}

.badge--ok {
  background: var(--color-success-soft);
  color: var(--color-success);
  border-color: var(--color-success);
}

.badge--mute {
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  border-color: var(--color-border-strong);
}

.row-actions {
  display: flex;
  gap: var(--space-2);
}

.link-btn {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-brand);
  font: inherit;
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition: filter 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.link-btn:hover {
  filter: brightness(1.15);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.link-btn:active {
  filter: brightness(0.95);
}
.link-btn--mute:hover {
  color: var(--color-brand);
}

.link-btn--mute {
  color: var(--color-text-secondary);
}

.btn {
  height: 32px;
  padding: 0 var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.btn:hover:not(:disabled) {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn--primary {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.btn--primary:hover:not(:disabled) {
  color: var(--color-text-inverse);
}

.foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-top: var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.pager {
  display: flex;
  gap: var(--space-2);
}

.bulk {
  position: fixed;
  left: 50%;
  bottom: var(--space-5);
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-4);
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  box-shadow: var(--shadow-popover);
  font-size: var(--font-size-sm);
  z-index: var(--z-popover);
}

.bulk b {
  color: var(--color-brand);
}

@media (max-width: 1200px) {
  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
