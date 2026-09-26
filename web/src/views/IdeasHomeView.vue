<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究构想 · **任务列表**（2026-09-26 研究者口径）。
 *
 * 打开「研究构想」先看到的是**最近的研究任务**，选一个才进工作区 ——
 * 工作区（四个方向 + 可行性分析）是"某个任务"的界面，不是这一页的默认内容。
 *
 * 一个任务 = 一个聚合（一次「选论文 → 生成四个方向」）。
 * 这里只做两件事：**列出最近的任务**、**新建任务**。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { writeDenied } from '@/utils/messages'

import {
  createAggregation,
  listAggregations,
  listIdeas,
  MECHANISM_LABELS,
  type Aggregation,
} from '@/api/idea'
import { searchPapers, type PaperSearchItem } from '@/api/papers'

const router = useRouter()

const aggregations = ref<Aggregation[]>([])
const titleById = ref<Record<number, string>>({})
const ideaCounts = ref<Record<number, number>>({})
const directionCounts = ref<Record<number, number>>({})
const loading = ref(false)
/** 统计每个任务的 idea 数要跑好几条请求，这段时间不能让行里显示"还没有生成"（那是假话） */
const statsLoading = ref(false)
const busy = ref(false)
const notice = ref('')

const pickerOpen = ref(false)
const papers = ref<PaperSearchItem[]>([])
const pickedIds = ref<number[]>([])
const pickerKeyword = ref('')

/** 一次最多给多少个任务统计 idea（避免为整个历史逐个查库） */
const STAT_LIMIT = 12

const inputMode = computed(() =>
  pickedIds.value.length >= 2
    ? '多篇交叉聚合'
    : pickedIds.value.length === 1
      ? '单篇精读发散'
      : '未选择论文',
)
const pickerItems = computed(() => {
  const keyword = pickerKeyword.value.trim().toLowerCase()
  if (!keyword) return papers.value.slice(0, 60)
  return papers.value
    .filter((paper) => (paper.title ?? '').toLowerCase().includes(keyword))
    .slice(0, 60)
})

function shortTitle(title: string | null | undefined): string {
  const value = title ?? ''
  return value.length > 52 ? `${value.slice(0, 52)}…` : value
}

function paperLabel(item: Aggregation): string {
  const ids = item.paper_ids ?? []
  if (ids.length === 1) return `#${ids[0]} · ${shortTitle(titleById.value[ids[0]]) || '（标题未在本页范围内）'}`
  return `${ids.length} 篇论文 · ${ids.slice(0, 3).map((id) => `#${id}`).join('、')}${ids.length > 3 ? '…' : ''}`
}

function createdAtText(item: Aggregation): string {
  const raw = (item as { created_at?: string }).created_at
  if (!raw) return ''
  try {
    return new Date(raw).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return raw
  }
}

function directionSummary(id: number): string {
  // 统计还没回来时如实说"正在统计" —— 不能显示"还没有生成 idea"（那是假话，会让人以为白干了）
  if (statsLoading.value || ideaCounts.value[id] === undefined) return '正在统计…'
  const total = ideaCounts.value[id] ?? 0
  if (!total) return '还没有生成 idea'
  const directions = directionCounts.value[id] ?? 0
  return `已生成 ${total} 条 idea · 覆盖 ${directions}/4 个方向`
}

async function loadList(): Promise<void> {
  loading.value = true
  notice.value = ''
  try {
    const [list, papersPage] = await Promise.all([
      listAggregations({ limit: 30 }),
      searchPapers({ pageSize: 100 }).catch(() => null),
    ])
    aggregations.value = list.items ?? []
    if (papersPage) {
      titleById.value = Object.fromEntries(
        (papersPage.items ?? []).map((paper) => [paper.id, paper.title]),
      )
      papers.value = papersPage.items ?? []
    }
    // 只给最近 STAT_LIMIT 个任务统计 idea（免得为整段历史逐个查库）
    statsLoading.value = true
    await Promise.all(
      aggregations.value.slice(0, STAT_LIMIT).map(async (item) => {
        try {
          const page = await listIdeas({ aggregationId: item.id, pageSize: 100 })
          const items = page.items ?? []
          ideaCounts.value = { ...ideaCounts.value, [item.id]: items.length }
          directionCounts.value = {
            ...directionCounts.value,
            [item.id]: new Set(items.map((idea) => idea.mechanism).filter(Boolean)).size,
          }
        } catch {
          /* 统计失败不影响列表本身 */
        }
      }),
    )
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  } finally {
    statsLoading.value = false
    loading.value = false
  }
}

function togglePaper(id: number): void {
  const index = pickedIds.value.indexOf(id)
  if (index >= 0) pickedIds.value.splice(index, 1)
  else pickedIds.value.push(id)
}

async function createTask(): Promise<void> {
  if (pickedIds.value.length === 0) {
    notice.value = '先选择至少一篇论文'
    return
  }
  busy.value = true
  notice.value = ''
  try {
    const aggregation = await createAggregation({ paper_ids: [...pickedIds.value] })
    const id = Number(
      aggregation.id ?? (aggregation as { aggregation_id?: number }).aggregation_id,
    )
    if (!Number.isFinite(id)) {
      notice.value = '任务创建失败：没有返回任务号'
      return
    }
    pickerOpen.value = false
    pickedIds.value = []
    await router.push(`/ideas/${id}`)
  } catch (error) {
    const status = (error as { status?: number })?.status
    notice.value =
      status === 403 ? writeDenied('新建研究任务') : error instanceof Error ? error.message : String(error)
  } finally {
    busy.value = false
  }
}

function openTask(item: Aggregation): void {
  void router.push(`/ideas/${item.id}`)
}

onMounted(loadList)
</script>

<template>
  <section class="ideas-home">
    <header class="ideas-home__head">
      <h1>研究构想</h1>
      <span class="spacer" />
      <button class="btn btn--primary" type="button" @click="pickerOpen = true">新建研究任务</button>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <article class="panel">
      <header class="panel__head">
        <h2>最近的任务</h2>
        <span class="spacer" />
        <span class="muted">{{ aggregations.length }} 个</span>
      </header>

      <p v-if="loading" class="empty">正在加载任务列表…</p>
      <p v-else-if="aggregations.length === 0" class="empty">
        还没有研究任务。点右上角「新建研究任务」，选一篇或多篇论文开始。
      </p>
      <div v-else class="tasks">
        <button
          v-for="item in aggregations"
          :key="item.id"
          class="task"
          type="button"
          @click="openTask(item)"
        >
          <span class="task__top">
            <span class="tag tag--brand">#{{ item.id }}</span>
            <span class="task__mode">{{ (item.paper_ids ?? []).length >= 2 ? '多篇交叉聚合' : '单篇精读发散' }}</span>
            <span class="spacer" />
            <span class="muted">{{ createdAtText(item) }}</span>
          </span>
          <span class="task__papers">{{ paperLabel(item) }}</span>
          <span class="task__stat">{{ directionSummary(item.id) }}</span>
        </button>
      </div>
    </article>

    <Teleport to="body">
      <div v-if="pickerOpen" class="overlay" @click.self="pickerOpen = false">
        <section class="dialog">
          <header class="dialog__head">
            <h2>选择论文</h2>
            <span class="pill">{{ inputMode }}</span>
            <span class="spacer" />
            <button class="icon-btn" type="button" aria-label="关闭" @click="pickerOpen = false">✕</button>
          </header>
          <div class="dialog__body">
            <input v-model="pickerKeyword" class="field__input" type="search" placeholder="按标题筛选" />
            <div class="picklist">
              <label v-for="paper in pickerItems" :key="paper.id" class="pickrow">
                <input
                  type="checkbox"
                  :checked="pickedIds.includes(paper.id)"
                  @change="togglePaper(paper.id)"
                />
                <span class="pickrow__title">#{{ paper.id }} · {{ paper.title }}</span>
                <span class="muted">{{ paper.venue ?? paper.source ?? '' }}</span>
              </label>
              <p v-if="pickerItems.length === 0" class="empty">没有匹配的论文。</p>
            </div>
          </div>
          <footer class="dialog__foot">
            <span class="muted">已选 {{ pickedIds.length }} 篇</span>
            <span class="spacer" />
            <button class="btn" type="button" @click="pickedIds = []">清空</button>
            <button class="btn btn--primary" type="button" :disabled="busy" @click="createTask">
              {{ busy ? '创建中…' : '创建任务' }}
            </button>
          </footer>
        </section>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.ideas-home {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.ideas-home__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.ideas-home__head h1 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.spacer {
  flex: 1;
}
.panel {
  padding: var(--space-4);
  background: var(--color-card-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.panel__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.panel__head h2 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.muted {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.tasks {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.task {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 12px 14px;
  text-align: left;
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: inherit;
  font: inherit;
  transition: border-color 160ms, background-color 160ms;
}
.task:hover {
  border-color: var(--color-brand);
}
.task__top {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.task__mode {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.task__papers {
  font-size: var(--font-size-sm);
  line-height: 1.6;
}
.task__stat {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.empty {
  margin: 0;
  padding: var(--space-4) 0;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

/* 选论文弹窗 */
.overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: rgba(15, 23, 42, 0.32);
}
.dialog {
  width: min(680px, 100%);
  max-height: min(78vh, 100%);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
}
.dialog__head,
.dialog__foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
}
.dialog__head {
  border-bottom: 1px solid var(--color-border);
}
.dialog__head h2 {
  margin: 0;
  font-size: var(--font-size-md);
}
.dialog__foot {
  border-top: 1px solid var(--color-border);
}
.dialog__body {
  padding: var(--space-3);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.icon-btn {
  width: 30px;
  height: 30px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.pill {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--font-size-xs);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
}
.picklist {
  display: flex;
  flex-direction: column;
}
.pickrow {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 8px 4px;
  border-bottom: 1px solid var(--color-border);
  font-size: var(--font-size-sm);
  cursor: pointer;
}
.pickrow:last-child {
  border-bottom: 0;
}
.pickrow__title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.field__input {
  height: 34px;
  padding: 0 12px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
}
</style>
