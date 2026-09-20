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
 * - 写操作（触发抓取、建卡、创建聚合）都需要「启用编辑」；被拒时给可行动的提示（utils/messages.ts）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { createAggregation } from '@/api/idea'
import {
  fetchPapersOverview,
  searchPapers,
  type PapersOverview,
  type PaperSearchItem,
} from '@/api/papers'
import { rebuildCard } from '@/api/parse'
import SmoothSelect from '@/components/SmoothSelect.vue'
import Pager from '@/components/Pager.vue'
import PaperImportDialog from '@/components/PaperImportDialog.vue'
import TaskMonitorDialog from '@/components/TaskMonitorDialog.vue'
import { useSessionStore } from '@/stores/session'
import { useTaskStore } from '@/stores/tasks'
import { writeDenied } from '@/utils/messages'

const route = useRoute()
const router = useRouter()
const session = useSessionStore()

/** 论文导入弹窗（原独立页 /papers/import 已并入此弹窗，2026-09-20） */
const importOpen = ref(false)

function openImport(): void {
  importOpen.value = true
}

/**
 * 深链口径：`/papers?import=1` 打开弹窗（旧的 `/papers/import` 路由也重定向到这里）；
 * 关闭时把参数抹掉，否则刷新页面又会被弹出来。
 */
watch(importOpen, (open) => {
  if (open && !route.query.import) {
    void router.replace({ query: { ...route.query, import: '1' } })
    return
  }
  if (!open && route.query.import) {
    const next = { ...route.query }
    delete next.import
    void router.replace({ query: next })
  }
})

const PAGE_SIZE = 20
const FIELD_OPTIONS = ['cs.AI', 'cs.CL', 'cs.CV', 'cs.LG']

/** 筛选区下拉项（平滑下拉需要 {value,label} 结构，原生 option 的文案原样搬过来） */
const FIELD_SELECT_OPTIONS = [
  { value: '', label: '全部领域' },
  ...FIELD_OPTIONS.map((item) => ({ value: item, label: item })),
]
const SOURCE_SELECT_OPTIONS = [
  { value: '', label: '全部来源' },
  { value: 'arxiv', label: 'arXiv' },
  { value: 'semantic_scholar', label: 'Semantic Scholar' },
  { value: 'openalex', label: 'OpenAlex' },
]
const PARSE_SELECT_OPTIONS = [
  { value: '', label: '全部解析状态' },
  { value: 'parsed', label: '已解析' },
  { value: 'unparsed', label: '未解析' },
]
const SORT_SELECT_OPTIONS = [
  { value: 'published', label: '按发表时间' },
  { value: 'citation', label: '按引用数' },
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

/**
 * 跨页选择：存**整行对象**（Map）而不是裸 id。
 * 理由：翻页后还要能"查看已选"列出标题 / 来源 —— 只存 id 的话得再发一轮请求去查。
 * 口径：翻页、改筛选、改排序都**不清空**；只有「清空」或显式移除才减（2026-09-20 改）。
 */
const selectedRows = ref(new Map<number, PaperSearchItem>())
const selectedIds = computed(() => [...selectedRows.value.keys()])
const selectedCount = computed(() => selectedRows.value.size)
const selectedList = computed(() => [...selectedRows.value.values()])
/** 「查看已选」弹窗 */
const pickedOpen = ref(false)

const busy = ref('')
const notice = ref('')
/** 旧后端会**静默忽略** source/parse_status/sort（FastAPI 不认的 query 直接丢掉）→ 如实提示，不装作筛过了 */
const staleFiltersNotice = ref('')

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

/** 服务端已按全库筛选/排序出当页数据，这里不再做任何切片（"仅本页生效"的口径已废） */
const visibleItems = computed(() => items.value)

function isSelected(id: number): boolean {
  return selectedRows.value.has(id)
}

function toggleRow(row: PaperSearchItem): void {
  const next = new Map(selectedRows.value)
  if (next.has(row.id)) next.delete(row.id)
  else next.set(row.id, row)
  selectedRows.value = next
}

function removeSelected(id: number): void {
  const next = new Map(selectedRows.value)
  next.delete(id)
  selectedRows.value = next
}

function clearSelection(): void {
  selectedRows.value = new Map()
  pickedOpen.value = false
}

const allOnPageSelected = computed(
  () =>
    visibleItems.value.length > 0 && visibleItems.value.every((row) => isSelected(row.id)),
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
      // 来源 / 解析状态 / 排序都交给服务端**全库**处理（分页 = 筛选结果的分页）
      source: sourceFilter.value,
      parseStatus: parseFilter.value,
      sort: sortKey.value,
      page: page.value,
      pageSize: PAGE_SIZE,
    })
    items.value = data.items ?? []
    total.value = data.total ?? 0
    // 回显校验：新后端会回 sort / source_filter；旧后端两者都没有。
    // 只要用户开了全库筛选/非默认排序却拿不到回显，就如实告知"服务端没生效"，避免"筛了等于没筛"。
    const filtersActive =
      Boolean(sourceFilter.value || parseFilter.value) || sortKey.value !== 'published'
    staleFiltersNotice.value =
      filtersActive && data.sort == null && data.source_filter == null
        ? '服务端未生效全库筛选/排序：后端需与本前端一起更新（旧后端会静默忽略这些参数）。'
        : ''
  } catch (error) {
    items.value = []
    total.value = 0
    listError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

/** 关键词 / 领域 / 来源 / 解析状态 / 排序任一变化 → 回到第 1 页重新拉全库结果（已选保留） */
function applyFilters(): void {
  page.value = 1
  void loadList()
}

function changePage(next: number): void {
  if (next < 1 || next > pageCount.value) return
  page.value = next
  // 翻页**不清空**已选（跨页对比/批处理是主场景）
  void loadList()
}

function toggleAllOnPage(): void {
  const next = new Map(selectedRows.value)
  if (allOnPageSelected.value) {
    visibleItems.value.forEach((row) => next.delete(row.id))
  } else {
    visibleItems.value.forEach((row) => next.set(row.id, row))
  }
  selectedRows.value = next
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
          ? writeDenied('重新同步（触发抓取）')
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
      ? writeDenied('暂停 / 继续 / 终止任务')
      : result.status === 409
        ? '任务已经结束，无需再操作（可点重新同步再跑一轮）。'
        : (result.message ?? '操作失败')
}

async function aggregateSelected(): Promise<void> {
  if (busy.value || selectedCount.value < 2) return
  busy.value = 'aggregate'
  notice.value = ''
  try {
    const aggregation = await createAggregation({
      paper_ids: selectedIds.value,
      project_id: session.currentProjectId,
    })
    await router.push({ name: 'aggregate', params: { id: String(aggregation.id) } })
  } catch (error) {
    notice.value =
      (error as { status?: number })?.status === 403
        ? writeDenied('创建对比分析（聚合）')
        : error instanceof Error
          ? error.message
          : String(error)
  } finally {
    busy.value = ''
  }
}

async function buildCardsForSelected(): Promise<void> {
  if (busy.value || selectedCount.value === 0) return
  busy.value = 'cards'
  notice.value = ''
  let ok = 0
  const failed: number[] = []
  for (const id of selectedIds.value) {
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
      : `已提交 ${ok} 篇；${failed.length} 篇失败（#${failed.join('、#')}）——若是权限被拒，请在「设置」里启用编辑后重试。`
  busy.value = ''
}

onMounted(() => {
  if (route.query.import) importOpen.value = true
  void loadOverview()
  void loadList()
})
</script>

<template>
  <section class="papers">
    <header class="papers__head">
      <h1>文献总览</h1>
      <div class="papers__actions">
        <button class="btn btn--primary" type="button" @click="openImport">导入</button>
      </div>
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
      <SmoothSelect v-model="sourceFilter" :options="SOURCE_SELECT_OPTIONS" @change="applyFilters" />
      <SmoothSelect v-model="parseFilter" :options="PARSE_SELECT_OPTIONS" @change="applyFilters" />
      <SmoothSelect v-model="sortKey" :options="SORT_SELECT_OPTIONS" @change="applyFilters" />
      <button class="btn" type="button" @click="applyFilters">检索</button>
      <Pager
        class="filters__pager"
        :page="page"
        :page-count="pageCount"
        :disabled="loading"
        @change="changePage"
      />
    </div>

    <p v-if="listError" class="hint hint--err">列表获取失败：{{ listError }}</p>
    <p v-if="staleFiltersNotice" class="hint hint--warn">{{ staleFiltersNotice }}</p>

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
          :class="{ 'is-selected': isSelected(row.id) }"
        >
          <td class="col-check">
            <input
              type="checkbox"
              :checked="isSelected(row.id)"
              @change="toggleRow(row)"
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
            <button class="link-btn link-btn--mute" type="button" @click="toggleRow(row)">
              {{ isSelected(row.id) ? '移出聚合' : '加入聚合' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="foot">
      <span>共 {{ total }} 篇 · 第 {{ page }} / {{ pageCount }} 页</span>
      <Pager :page="page" :page-count="pageCount" :disabled="loading" @change="changePage" />
    </div>

    <!-- 跨页固定操作栏：已选数量不随翻页/改筛选丢失 -->
    <div v-if="selectedCount > 0" class="bulk">
      已选 <b>{{ selectedCount }}</b> 篇
      <button class="btn" type="button" @click="pickedOpen = true">查看已选</button>
      <button
        class="btn btn--primary"
        type="button"
        :disabled="selectedCount < 2 || busy === 'aggregate'"
        :title="selectedCount < 2 ? '至少选择 2 篇才能对比' : 'POST /aggregations'"
        @click="aggregateSelected"
      >
        {{ busy === 'aggregate' ? '聚合中…' : '对比分析' }}
      </button>
      <button class="btn" type="button" :disabled="busy === 'cards'" @click="buildCardsForSelected">
        {{ busy === 'cards' ? '提交中…' : '批量深度解析' }}
      </button>
      <button class="link-btn link-btn--mute" type="button" @click="clearSelection">清空</button>
    </div>

    <!-- 「查看已选」：跨页清单（整行对象在手，不用再发请求） -->
    <Teleport to="body">
      <div v-if="pickedOpen" class="picked-overlay" @click.self="pickedOpen = false">
        <section class="picked" role="dialog" aria-modal="true" aria-label="已选论文">
          <header class="picked__head">
            <h2 class="picked__title">已选 {{ selectedCount }} 篇</h2>
            <span class="picked__spacer" />
            <button class="picked__close" type="button" aria-label="关闭" @click="pickedOpen = false">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
          </header>
          <ul class="picked__list scroll-y">
            <li v-for="row in selectedList" :key="row.id" class="picked__row">
              <a class="picked__name" href="#" @click.prevent="openParse(row.id)">{{ row.title }}</a>
              <span class="picked__meta">{{ sourceLabel(row.source) }} · {{ row.published_at ?? '时间未获取' }}</span>
              <button class="link-btn link-btn--mute" type="button" @click="removeSelected(row.id)">
                移除
              </button>
            </li>
          </ul>
          <footer class="picked__foot">
            <button class="btn" type="button" @click="clearSelection">清空</button>
            <button
              class="btn btn--primary"
              type="button"
              :disabled="selectedCount < 2 || busy === 'aggregate'"
              @click="aggregateSelected"
            >
              对比分析
            </button>
          </footer>
        </section>
      </div>
    </Teleport>

    <TaskMonitorDialog
      v-model="monitorOpen"
      :job="tasks.activeJob"
      @retry="sync"
      @pause="controlTask('pause')"
      @resume="controlTask('resume')"
      @cancel="controlTask('cancel')"
    />
    <!-- 论文导入弹窗（原独立页 /papers/import 已并入此弹窗） -->
    <PaperImportDialog v-model:open="importOpen" />
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

.hint--warn {
  color: var(--color-warning);
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

.filters__pager {
  margin-left: var(--space-2);
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

/* ---------- 「查看已选」弹窗（跨页清单） ---------- */
.picked-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  /* 与其它弹窗同口径：遮罩色与主题解耦，深色下加深 */
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .picked-overlay {
  background: rgba(0, 0, 0, 0.55);
}

.picked {
  width: min(560px, 100%);
  max-height: min(620px, 100%);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-card-bg);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.4);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.picked__head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.picked__title {
  margin: 0;
  font-size: var(--font-size-lg);
}

.picked__spacer {
  flex: 1;
}

.picked__close {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: border-color 160ms, color 160ms;
}

.picked__close:hover {
  border-color: var(--color-border-strong);
  color: var(--color-text-primary);
}

.picked__list {
  margin: 0;
  padding: var(--space-2) var(--space-4);
  list-style: none;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.picked__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-border);
}

.picked__row:last-child {
  border-bottom: 0;
}

.picked__name {
  overflow: hidden;
  color: var(--color-text-primary);
  text-decoration: none;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.picked__name:hover {
  color: var(--color-brand);
}

.picked__meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  white-space: nowrap;
}

.picked__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

@media (max-width: 1200px) {
  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
