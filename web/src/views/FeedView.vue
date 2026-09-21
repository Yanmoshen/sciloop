<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文库：三视图 Tab（推荐默认）+ 筛选器 + 分页 + 演示数据标识（快照 / 回放）。
 *
 * 口径分离（计划书 §2.7.5）：
 *   推荐视图 rank_score DESC ｜ 影响力视图 influence_score DESC（辅助分）｜ 最新视图 published_at DESC
 * 2026-09-21 结构（用户指定）：**表格 / 卡片 两种形态**，默认表格。
 *   · 表格视图 = 从「文献总览」搬来的逐篇挑选与批量操作（全库筛选/排序、跨页选择、批量栏），
 *     自带筛选行与分页，见 PaperTable.vue；
 *   · 卡片视图 = 原有的三视图卡片流（推荐/影响力/最新 + 时间范围等筛选）。
 *   数据源健康已搬到「文献总览」（该页负责"库的整体状况"，本页负责"逐篇挑选"）。
 * 不硬编码颜色：全部引用 tokens.css 变量。
 */
import { computed, onMounted, ref, watch } from 'vue'

import PaperCard from '@/components/PaperCard.vue'
import PaperTable from '@/components/PaperTable.vue'
import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { FIELD_OPTIONS, type FeedViewName } from '@/api/feed'
import { useFeedStore } from '@/stores/feed'

const store = useFeedStore()
const filters = store.filters

/** 列表形态：表格（默认） / 卡片 */
const layout = ref<'table' | 'card'>('table')

const viewTabs: Array<{ value: FeedViewName; label: string; hint: string }> = [
  { value: 'recommended', label: '推荐视图', hint: '默认 · 按四维加权' },
  { value: 'influence', label: '影响力视图', hint: '辅助分 · 不用于默认排序' },
  { value: 'latest', label: '最新视图', hint: '按时间倒序' },
]

/** store.current 经 Pinia 代理已解包为普通对象，这里再包一层 computed 以跟随视图切换 */
const current = computed(() => store.current)
const meta = computed(() => current.value.meta)
const items = computed(() => current.value.items)

/** 四维权重 / 影响力权重：传给列表项组件用于权重提示 */
const rankWeights = computed(() => meta.value?.ranking?.rank_weights ?? {})
const influenceWeights = computed(() => meta.value?.ranking?.influence_weights ?? {})

/** 本页数据来源如实标注（要求过演示快照却回退成实时数据时，也要说清楚） */
const dataSourceLabel = computed(() => {
  switch (current.value.dataSource) {
    case 'snapshot':
      return '演示数据（快照）'
    case 'replay':
      return '演示数据（回放）'
    case 'live':
      return '实时数据'
    default:
      return '数据来源未获取'
  }
})

/** 降级：请求了快照但服务端回退实时数据（后端如实标注 data_source_note） */
const snapshotFallback = computed(
  () => filters.snapshot && current.value.dataSource === 'live' && Boolean(current.value.dataSourceNote),
)

/** 降级：接口自报 notes 提示分项缺失/来源不可用 */

/** 权限拒绝：写接口 401/403（本页为只读，仍如实区分该状态而非笼统报错） */
const permissionDenied = computed(
  () => current.value.errorStatus === 401 || current.value.errorStatus === 403,
)

const errorMessage = computed(() => (permissionDenied.value ? null : current.value.error))

function selectView(view: FeedViewName): void {
  store.setView(view)
}

function onDateRangeChange(value: [string, string] | null): void {
  store.setDateRange(value && value.length === 2 && value[0] && value[1] ? [value[0], value[1]] : null)
}

function onPageChange(page: number): void {
  store.setPage(store.activeView, page)
  void store.load(store.activeView, { force: true })
}

function onPageSizeChange(size: number): void {
  store.setPageSize(store.activeView, size)
  void store.load(store.activeView, { force: true })
}

function reload(): void {
  void store.load(store.activeView, { force: true })
}

onMounted(() => {
  void store.load(store.activeView)
})

watch(
  () => store.activeView,
  (view) => {
    store.ensureLoaded(view)
  },
)

watch(
  () => store.stamp,
  () => {
    store.setPage(store.activeView, 1)
    void store.load(store.activeView, { force: true })
  },
)
</script>

<template>
  <section class="feed">


    <header class="feed__head">
      <h1>论文库</h1>
      <span v-if="layout === 'card'" class="feed__count">
        <template v-if="current.total > 0">共 {{ current.total }} 篇</template>
        <template v-else>共 0 篇</template>
      </span>
      <span
        v-if="layout === 'card'"
        class="feed__source-tag"
        :class="{ 'feed__source-tag--live': current.dataSource === 'live' }"
      >
        {{ dataSourceLabel }}
      </span>
      <div class="feed__layout">
        <el-radio-group v-model="layout" size="small">
          <el-radio-button value="table">表格</el-radio-button>
          <el-radio-button value="card">卡片</el-radio-button>
        </el-radio-group>
      </div>
      <el-button class="feed__reload" size="small" text :loading="current.loading" @click="reload">
        刷新
      </el-button>
    </header>

    <div class="feed__tabs">
      <el-radio-group
        :model-value="store.activeView"
        size="default"
        @update:model-value="(v: string | number | boolean) => selectView(v as FeedViewName)"
      >
        <el-radio-button v-for="tab in viewTabs" :key="tab.value" :value="tab.value">
          {{ tab.label }}
        </el-radio-button>
      </el-radio-group>
    </div>

    <!-- 表格形态：全库筛选/排序 + 跨页选择 + 批量操作（从文献总览搬来） -->
    <PaperTable v-if="layout === 'table'" />

    <template v-else>
      <p v-if="snapshotFallback" class="feed__notice">
        已按要求请求演示数据，但服务端本次返回的是实时数据（状态以接口为准）。
      </p>
      <div class="filters sl-card">
        <div class="filter-item">
          <span class="filter-label">领域</span>
          <el-select
            :model-value="filters.field"
            placeholder="全部领域"
            size="small"
            clearable
            class="filter-control"
            @update:model-value="(v: string | null) => store.setField(v ?? null)"
          >
            <el-option v-for="field in FIELD_OPTIONS" :key="field" :label="field" :value="field" />
          </el-select>
        </div>

        <div class="filter-item">
          <span class="filter-label">时间范围</span>
          <el-date-picker
            :model-value="filters.dateRange"
            type="daterange"
            size="small"
            value-format="YYYY-MM-DD"
            start-placeholder="起始"
            end-placeholder="结束"
            class="filter-control filter-control--wide"
            @update:model-value="(v: unknown) => onDateRangeChange((v ?? null) as [string, string] | null)"
          />
        </div>

        <div class="filter-item">
          <span class="filter-label">查询词</span>
          <el-input
            :model-value="filters.query"
            size="small"
            class="filter-control filter-control--wide"
            placeholder="留空则检索相关性不参与排序"
            clearable
            @update:model-value="(v: string) => store.setQuery(v)"
          />
        </div>

        <div class="filter-item">
          <span class="filter-label">演示数据</span>
          <el-checkbox
            :model-value="filters.snapshot"
            size="small"
            label="使用演示数据"
            title="开启后请求演示快照；接口回退实时数据时会如实标注为实时数据"
            @update:model-value="(v: string | number | boolean) => store.setSnapshot(Boolean(v))"
          />
        </div>

        <div class="filter-item filter-item--actions">
          <el-button size="small" @click="store.resetFilters()">重置筛选</el-button>
          <span class="filter-current">{{ store.filtersDirtyHint }}</span>
        </div>
      </div>

      <!-- 六类状态：loading / error / permission denied / retry（empty 见下方 el-empty） -->
      <ViewStatePanel
        :loading="current.loading && items.length > 0"
        loading-text="正在刷新当前视图…（旧结果保留但已置灰，状态以新响应为准）"
        :error="errorMessage"
        :error-code="current.errorCode"
        error-title="论文库加载失败"
        :permission-denied="permissionDenied"
        :permission-note="current.error"
        retryable
        retry-label="重试加载论文库"
        :busy="current.loading"
        @retry="reload"
      />

      <div v-if="current.loading && items.length === 0" class="feed__skeleton">
        <el-skeleton v-for="i in 3" :key="`sk-${i}`" :rows="3" animated />
      </div>

      <el-empty
        v-else-if="!current.loading && items.length === 0 && !current.error"
        description="当前筛选条件下没有论文（未获取不等于 0，可放宽筛选后重试）"
      />

      <div v-else class="feed__list" :class="{ 'feed__list--stale': current.loading }">
        <PaperCard
          v-for="(item, idx) in items"
          :key="`${store.activeView}-${item.id}`"
          :item="item"
          :active-view="store.activeView"
          :rank-weights="rankWeights"
          :influence-weights="influenceWeights"
          :index="(current.page - 1) * current.pageSize + idx"
        />
      </div>

      <el-pagination
        v-if="current.total > 0"
        class="feed__pager"
        background
        layout="total, sizes, prev, pager, next"
        :total="current.total"
        :current-page="current.page"
        :page-size="current.pageSize"
        :page-sizes="[10, 20, 50]"
        @current-change="onPageChange"
        @size-change="onPageSizeChange"
      />
    </template>

  </section>
</template>

<style scoped>
.feed {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.feed__head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.feed__head h1 {
  margin: 0;
  font-size: var(--font-size-xl);
}

.feed__source {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.feed__source--live {
  border-color: var(--color-success);
  color: var(--color-success);
  background-color: var(--color-success-soft);
}

.feed__count {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

/* 数据来源如实标注：演示数据用中性标签，实时数据用成功色 */
.feed__source-tag {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.feed__source-tag--live {
  border-color: var(--color-success);
  color: var(--color-success);
  background-color: var(--color-success-soft);
}

.feed__notice {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-warning);
  border-radius: var(--radius-md);
  background: var(--color-warning-soft);
  color: var(--color-warning);
  font-size: var(--font-size-sm);
}

.feed__layout {
  margin-left: auto;
}

.feed__reload {
  margin-left: var(--space-2);
}

/* 顶部固定排序依据说明条 */













.feed__tabs {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}


.filters {
  display: flex;
  gap: var(--space-4);
  flex-wrap: wrap;
  align-items: center;
  padding: var(--space-3);
}

.filter-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.filter-item--actions {
  margin-left: auto;
}

.filter-label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.filter-control {
  width: 140px;
}

.filter-control--wide {
  width: 220px;
}

.filter-current {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.feed__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.feed__list--stale {
  opacity: 0.6;
}

.feed__skeleton {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.feed__pager {
  justify-content: flex-end;
}

</style>
