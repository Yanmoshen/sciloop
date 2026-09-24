<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文库 · 表格视图（2026-09-21 从「文献总览」整体搬来：筛选行 + 分页 + 表格 + 跨页选择 + 批量操作）。
 *
 * 为什么表格在论文库而不是文献总览：文献总览讲"库的整体状况"（数据源健康、统计、图表），
 * 论文库才是"逐篇挑选与批量操作"的地方 —— 来源/解析状态/排序都走服务端**全库**生效，
 * 分页是"筛选结果的分页"，跨页选择保留（用户实测过的痛点）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { createAggregation } from '@/api/idea'
import { searchPapers, type PaperSearchItem } from '@/api/papers'
import { rebuildCard } from '@/api/parse'
import Pager from '@/components/Pager.vue'
import SmoothSelect from '@/components/SmoothSelect.vue'
import { useSessionStore } from '@/stores/session'
import { writeDenied } from '@/utils/messages'

const PAGE_SIZE = 20
const FIELD_OPTIONS = ['cs.AI', 'cs.CL', 'cs.CV', 'cs.LG']

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

const router = useRouter()
const session = useSessionStore()

/** 选择模式：给「解析首屏」的选文弹窗复用。
 *
 * 打开后**只隐藏自带的批量操作栏**（那排按钮由弹窗自己提供），
 * 搜索 / 筛选 / 分页 / 跨页选择 / 「查看已选」全部照旧 —— 论文库自身的用法一字不变。
 */
const props = withDefaults(defineProps<{ selectMode?: boolean }>(), { selectMode: false })

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

/** 跨页选择：存**整行对象**（Map），翻页/改筛选都不清空，只在「清空」或显式移除时减 */
const selectedRows = ref(new Map<number, PaperSearchItem>())
const selectedIds = computed(() => [...selectedRows.value.keys()])
const selectedCount = computed(() => selectedRows.value.size)
const selectedList = computed(() => [...selectedRows.value.values()])
const pickedOpen = ref(false)

const busy = ref('')
const notice = ref('')
/** 旧后端会静默忽略 source/parse_status/sort → 如实提示，不装作筛过了 */
const staleFiltersNotice = ref('')

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const visibleItems = computed(() => items.value)

const allOnPageSelected = computed(
  () => visibleItems.value.length > 0 && visibleItems.value.every((row) => isSelected(row.id)),
)

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

function toggleAllOnPage(): void {
  const next = new Map(selectedRows.value)
  if (allOnPageSelected.value) {
    visibleItems.value.forEach((row) => next.delete(row.id))
  } else {
    visibleItems.value.forEach((row) => next.set(row.id, row))
  }
  selectedRows.value = next
}

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

/** 论文链接：只给一个真实可点的链接，接口没给就整行不渲染 */
function paperLink(row: PaperSearchItem): string | null {
  if (row.source === 'arxiv' && row.external_id) return `https://arxiv.org/abs/${row.external_id}`
  if (row.doi) return `https://doi.org/${row.doi}`
  if (row.pdf_url) return row.pdf_url
  return null
}

function parseBadge(row: PaperSearchItem): { text: string; cls: string } {
  if (row.is_parsed === true) return { text: '已解析', cls: 'badge--ok' }
  if (row.is_parsed === false) return { text: '未解析', cls: 'badge--mute' }
  return { text: '未获取', cls: 'badge--mute' }
}

async function loadList(): Promise<void> {
  loading.value = true
  listError.value = ''
  try {
    const data = await searchPapers({
      q: keyword.value.trim(),
      field: field.value,
      source: sourceFilter.value,
      parseStatus: parseFilter.value,
      sort: sortKey.value,
      page: page.value,
      pageSize: PAGE_SIZE,
    })
    items.value = data.items ?? []
    total.value = data.total ?? 0
    const filtersActive =
      Boolean(sourceFilter.value || parseFilter.value) || sortKey.value !== 'published'
    staleFiltersNotice.value =
      filtersActive && data.sort == null && data.source_filter == null
        ? '服务端未生效全库筛选/排序：后端需与本前端一起更新。'
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
  void loadList()
}

function openParse(id: number): void {
  void router.push({ path: `/papers/parse/${id}` })
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
  // **并发上限 3**：一次把几十篇全推给上游会把链路打满、失败原因也混在一起；
  // 顺序取任务、3 个 worker 并行，既压住并发又不把总时长拖成串行。
  const queue = [...selectedIds.value]
  const worker = async (): Promise<void> => {
    for (;;) {
      const id = queue.shift()
      if (id === undefined) return
      try {
        await rebuildCard(id, true)
        ok += 1
      } catch {
        failed.push(id)
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, () => worker()))
  notice.value =
    failed.length === 0
      ? `已提交 ${ok} 篇的建卡任务（后台执行）。`
      : `已提交 ${ok} 篇；${failed.length} 篇失败（#${failed.join('、#')}）——若是权限被拒，请在「设置」里启用编辑后重试。`
  busy.value = ''
}

onMounted(() => {
  void loadList()
})

/** 给复用方（解析首屏的选文弹窗）的接口：不改变论文库自身的任何用法 */
defineExpose({
  selectedCount,
  selectedList,
  aggregateSelected,
  buildCardsForSelected,
  openPicked: () => {
    pickedOpen.value = true
  },
  closePicked: () => {
    pickedOpen.value = false
  },
})
</script>

<template>
  <div class="paper-table">
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

    <p v-if="notice" class="hint hint--warn">{{ notice }}</p>
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
          <td colspan="7" class="empty">没有符合条件的论文：可放宽筛选后重试。</td>
        </tr>
        <tr
          v-for="row in visibleItems"
          v-else
          :key="row.id"
          :class="{ 'is-selected': isSelected(row.id) }"
        >
          <td class="col-check">
            <input type="checkbox" :checked="isSelected(row.id)" @change="toggleRow(row)" />
          </td>
          <td class="title-cell">
            <a href="#" @click.prevent="openParse(row.id)">{{ row.title }}</a>
            <a
              v-if="paperLink(row)"
              class="title-cell__link"
              :href="paperLink(row) ?? '#'"
              target="_blank"
              rel="noreferrer noopener"
            >
              {{ paperLink(row) }}
            </a>
          </td>
          <td class="col-source">{{ sourceLabel(row.source) }}</td>
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

    <!-- 跨页固定操作栏：已选数量不随翻页/改筛选丢失（选择模式下由复用方自备操作栏） -->
    <div v-if="!props.selectMode && selectedCount > 0" class="bulk">
      已选 <b>{{ selectedCount }}</b> 篇
      <button class="btn" type="button" @click="pickedOpen = true">查看已选</button>
      <button
        class="btn btn--primary"
        type="button"
        :disabled="selectedCount < 2 || busy === 'aggregate'"
        :title="selectedCount < 2 ? '至少选择 2 篇才能对比' : ''"
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
  </div>
</template>

<style scoped>
.paper-table {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
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
</style>
