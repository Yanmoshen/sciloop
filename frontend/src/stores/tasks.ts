/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全局任务状态（Pinia）：同步任务的历史列表 + 当前跟踪任务的轮询与控制。
 *
 * 为什么放 store 而不是留在 PapersView：任务监控窗口现在有两处入口
 * （文献总览点「立即同步」、顶栏「任务」看历史再打开某一条），
 * 轮询/暂停/终止只能有一份实现，否则两个页面各写一遍必然走偏。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  cancelFetchJob,
  fetchFetchJob,
  listFetchJobs,
  pauseFetchJob,
  resumeFetchJob,
  triggerFetch,
  type FetchJobStatus,
  type FetchTaskSummary,
} from '@/api/papers'

const TERMINAL = ['done', 'failed', 'cancelled']
const POLL_INTERVAL_MS = 2500
const WATCH_INTERVAL_MS = 15000

export const useTaskStore = defineStore('tasks', () => {
  /** 历史任务（新的在前） */
  const recent = ref<FetchTaskSummary[]>([])
  const loadingRecent = ref(false)
  /** 当前被跟踪的任务快照（监控窗口的数据源） */
  const activeJob = ref<FetchJobStatus | null>(null)
  /** 是否正在跟踪一个未结束的任务 */
  const tracking = computed(
    () => Boolean(activeJob.value) && !TERMINAL.includes(String(activeJob.value?.status)),
  )
  /** 是否有任务在跑（顶栏图标发亮 + 旋转的依据） */
  const hasRunning = computed(
    () => tracking.value || recent.value.some((item) => !TERMINAL.includes(item.status)),
  )

  let pollTimer: ReturnType<typeof setInterval> | null = null
  let watchTimer: ReturnType<typeof setInterval> | null = null

  async function loadRecent(limit = 30): Promise<void> {
    loadingRecent.value = true
    try {
      const result = await listFetchJobs(limit)
      recent.value = result.tasks ?? []
    } catch {
      /* 历史拉取失败不打断界面，保留上一次结果 */
    } finally {
      loadingRecent.value = false
    }
  }

  /** 定时轻量刷新历史（顶栏图标据此判断"是否有任务在跑"） */
  function startWatch(): void {
    if (watchTimer !== null) return
    void loadRecent(8)
    watchTimer = setInterval(() => {
      void loadRecent(8)
    }, WATCH_INTERVAL_MS)
  }

  function stopWatch(): void {
    if (watchTimer !== null) {
      clearInterval(watchTimer)
      watchTimer = null
    }
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  async function refreshActive(): Promise<void> {
    const taskId = activeJob.value?.task_id
    if (!taskId) return
    try {
      activeJob.value = await fetchFetchJob(taskId)
    } catch {
      /* 单次失败保留上一份快照 */
    }
  }

  /** 跟踪某个任务：立即取一次快照，未结束则开始轮询 */
  async function track(taskId: string): Promise<void> {
    activeJob.value = { task_id: taskId, status: 'accepted' } as FetchJobStatus
    stopPolling()
    await refreshActive()
    if (TERMINAL.includes(String(activeJob.value?.status))) {
      await loadRecent(8)
      return
    }
    pollTimer = setInterval(() => {
      void (async () => {
        await refreshActive()
        if (TERMINAL.includes(String(activeJob.value?.status))) {
          stopPolling()
          await loadRecent(8)
        }
      })()
    }, POLL_INTERVAL_MS)
  }

  /** 提交一轮新的抓取并跟踪它 */
  async function startFetch(limit = 30): Promise<{ ok: true; taskId: string } | { ok: false; status?: number; message: string }> {
    try {
      const task = await triggerFetch({ limit })
      await track(task.task_id)
      return { ok: true, taskId: task.task_id }
    } catch (error) {
      const status = (error as { status?: number })?.status
      return {
        ok: false,
        status,
        message: error instanceof Error ? error.message : String(error),
      }
    }
  }

  /** 暂停 / 继续 / 终止（Owner 面） */
  async function control(kind: 'pause' | 'resume' | 'cancel'): Promise<{ ok: boolean; status?: number; message?: string }> {
    const taskId = activeJob.value?.task_id
    if (!taskId) return { ok: false, message: '没有正在跟踪的任务' }
    try {
      activeJob.value =
        kind === 'pause'
          ? await pauseFetchJob(taskId)
          : kind === 'resume'
            ? await resumeFetchJob(taskId)
            : await cancelFetchJob(taskId)
      if (kind === 'cancel') {
        stopPolling()
        await loadRecent(8)
      }
      return { ok: true }
    } catch (error) {
      const status = (error as { status?: number })?.status
      return {
        ok: false,
        status,
        message: error instanceof Error ? error.message : String(error),
      }
    }
  }

  return {
    recent,
    loadingRecent,
    activeJob,
    tracking,
    hasRunning,
    loadRecent,
    startWatch,
    stopWatch,
    track,
    startFetch,
    control,
    refreshActive,
    stopPolling,
  }
})
