/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文库状态管理（WP07-T6）：筛选条件 / 分页 / 分视图缓存。
 *
 * 关键约束（WP07 risks）：三视图**按 view 维度隔离状态**，绝不共用同一分页对象，
 * 否则切视图会串数据（例如 influence 视图沿用 recommended 的 page=3 导致空页）。
 */

import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '@/api/client'
import { FEED_VIEW_META, fetchFeed, type FeedResponse, type FeedViewName } from '@/api/feed'

export interface FeedFiltersState {
  /** 领域：cs.AI / cs.CL / cs.CV / cs.LG */
  field: string | null
  /** 时间范围 [from, to]（YYYY-MM-DD），未选为 null */
  dateRange: [string, string] | null
  /** 仅看已识别顶会/期刊等级 */
  venueOnly: boolean
  /** 可选检索词：**不填时 relevance 不参与排序**（界面须提示） */
  query: string
  /** 演示快照：开启后请求 snapshot=demo，返回 data_source=snapshot 时展示 DemoBadge */
  snapshot: boolean
}

export interface FeedViewState {
  view: FeedViewName
  items: FeedResponse['items']
  total: number
  page: number
  pageSize: number
  meta: FeedResponse | null
  dataSource: FeedResponse['data_source'] | null
  dataSourceNote: string | null
  sortBy: string
  notes: string[]
  loading: boolean
  error: string | null
  errorCode: string | null
  /** HTTP 状态码：用于区分「权限拒绝（401/403）」与一般失败；null 表示非 HTTP 失败 */
  errorStatus: number | null
  loaded: boolean
  /** 数据标记：与全局 stamp 不一致时需要重新拉取 */
  stamp: number
  updatedAt: number | null
}

function emptyViewState(view: FeedViewName): FeedViewState {
  return {
    view,
    items: [],
    total: 0,
    page: 1,
    pageSize: 10,
    meta: null,
    dataSource: null,
    dataSourceNote: null,
    sortBy: FEED_VIEW_META[view].sortLabel,
    notes: [],
    loading: false,
    error: null,
    errorCode: null,
    errorStatus: null,
    loaded: false,
    stamp: -1,
    updatedAt: null,
  }
}

export const useFeedStore = defineStore('feed', () => {
  const filters = reactive<FeedFiltersState>({
    field: null,
    dateRange: null,
    venueOnly: false,
    query: '',
    snapshot: false,
  })

  const activeView = ref<FeedViewName>('recommended')
  const views = reactive<Record<FeedViewName, FeedViewState>>({
    recommended: emptyViewState('recommended'),
    influence: emptyViewState('influence'),
    latest: emptyViewState('latest'),
  })

  /** 每次筛选条件变化即自增：所有视图缓存失效 */
  const stamp = ref(0)
  /** 进行中的请求序号（按视图隔离），用于丢弃过期响应 */
  const inFlight = new Map<FeedViewName, number>()

  const current = computed<FeedViewState>(() => views[activeView.value])

  const filtersDirtyHint = computed(() => {
    const parts: string[] = []
    if (filters.field) parts.push(`领域=${filters.field}`)
    if (filters.dateRange) parts.push(`时间=${filters.dateRange[0]}~${filters.dateRange[1]}`)
    if (filters.venueOnly) parts.push('仅顶会')
    if (filters.query.trim()) parts.push(`查询词=“${filters.query.trim()}”`)
    if (filters.snapshot) parts.push('演示快照')
    return parts.length > 0 ? parts.join(' · ') : '无筛选（全库）'
  })

  function invalidate(): void {
    stamp.value += 1
  }

  function setView(view: FeedViewName): void {
    activeView.value = view
  }

  function setField(field: string | null): void {
    if (filters.field === field) return
    filters.field = field
    invalidate()
  }

  function setDateRange(range: [string, string] | null): void {
    filters.dateRange = range
    invalidate()
  }

  function setVenueOnly(value: boolean): void {
    if (filters.venueOnly === value) return
    filters.venueOnly = value
    invalidate()
  }

  function setQuery(value: string): void {
    if (filters.query === value) return
    filters.query = value
    invalidate()
  }

  function setSnapshot(value: boolean): void {
    if (filters.snapshot === value) return
    filters.snapshot = value
    invalidate()
  }

  function resetFilters(): void {
    filters.field = null
    filters.dateRange = null
    filters.venueOnly = false
    filters.query = ''
    filters.snapshot = false
    invalidate()
  }

  function setPage(view: FeedViewName, page: number): void {
    const state = views[view]
    const next = Math.max(1, page)
    if (state.page === next) return
    state.page = next
    state.loaded = false
  }

  function setPageSize(view: FeedViewName, size: number): void {
    const state = views[view]
    if (state.pageSize === size) return
    state.pageSize = size
    state.page = 1
    state.loaded = false
  }

  async function load(view: FeedViewName, options: { force?: boolean } = {}): Promise<void> {
    const state = views[view]
    const fresh = state.loaded && state.stamp === stamp.value && !options.force
    if (fresh || state.loading) return

    const seq = (inFlight.get(view) ?? 0) + 1
    inFlight.set(view, seq)
    state.loading = true
    state.error = null
    state.errorCode = null
    state.errorStatus = null

    const query = {
      view,
      field: filters.field,
      from: filters.dateRange?.[0] ?? null,
      to: filters.dateRange?.[1] ?? null,
      venue_only: filters.venueOnly,
      q: filters.query.trim() ? filters.query.trim() : null,
      snapshot: filters.snapshot ? 'demo' : null,
      page: state.page,
      page_size: state.pageSize,
    }

    try {
      const response = await fetchFeed(query)
      if (inFlight.get(view) !== seq) return // 过期响应，丢弃
      state.items = response.items
      state.total = response.total
      state.page = response.page
      state.pageSize = response.page_size
      state.meta = response
      state.dataSource = response.data_source
      state.dataSourceNote = response.data_source_note
      state.sortBy = response.sort_by
      state.notes = response.ranking?.notes ?? []
      state.loaded = true
      state.stamp = stamp.value
      state.updatedAt = Date.now()
    } catch (error) {
      if (inFlight.get(view) !== seq) return
      state.items = []
      state.total = 0
      state.meta = null
      state.dataSource = null
      state.dataSourceNote = null
      state.loaded = false
      if (error instanceof ApiError) {
        state.error = error.message
        state.errorCode = error.code
        state.errorStatus = error.status
      } else {
        state.error = (error as Error).message ?? '加载失败'
        state.errorCode = 'unknown_error'
        state.errorStatus = null
      }
    } finally {
      if (inFlight.get(view) === seq) state.loading = false
    }
  }

  function reloadAll(): void {
    invalidate()
    void load(activeView.value, { force: true })
  }

  /** 依据当前筛选条件判断是否需要重新拉取（视图切换时调用） */
  function ensureLoaded(view: FeedViewName): void {
    const state = views[view]
    if (!state.loaded || state.stamp !== stamp.value) void load(view)
  }

  return {
    filters,
    activeView,
    views,
    current,
    stamp,
    filtersDirtyHint,
    setView,
    setField,
    setDateRange,
    setVenueOnly,
    setQuery,
    setSnapshot,
    resetFilters,
    setPage,
    setPageSize,
    load,
    reloadAll,
    ensureLoaded,
  }
})
