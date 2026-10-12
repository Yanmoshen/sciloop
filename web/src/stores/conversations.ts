/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 会话列表状态（左栏「未分组 / 项目 / 已归档」三块 + 对话页写入后的刷新）。
 *
 * 为什么单独成 store：左栏由壳层（HomeLayout）渲染，而新建/续聊发生在对话页（HomeView）里，
 * 两边必须看同一份数据，否则「刚发的第一条对话」要刷新页面才出现在左栏。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { listConversations, type ConversationBrief } from '@/api/conversations'
import { safeErrorMessage } from '@/utils/messages'

export const useConversationStore = defineStore('conversations', () => {
  /** 未归档会话（全部项目 + 未分组） */
  const items = ref<ConversationBrief[]>([])
  /** 已归档会话（默认不取，展开「已归档」时才拉） */
  const archived = ref<ConversationBrief[]>([])
  const error = ref('')
  const archivedError = ref('')
  const loading = ref(false)
  let loadingRequest: Promise<void> | null = null
  let loadingArchivedRequest: Promise<void> | null = null

  const ungrouped = computed(() => items.value.filter((item) => item.project_id == null))

  const byProject = computed(() => {
    const map = new Map<number, ConversationBrief[]>()
    for (const item of items.value) {
      const projectId = item.project_id
      if (projectId == null) continue
      const bucket = map.get(projectId)
      if (bucket) bucket.push(item)
      else map.set(projectId, [item])
    }
    return map
  })

  function forProject(projectId: number): ConversationBrief[] {
    return byProject.value.get(projectId) ?? []
  }

  function message(error_: unknown): string {
    return safeErrorMessage(error_)
  }

  /**
   * The shell mounts while the API process may still be warming up. A single
   * failed list request used to clear the rail and leave a raw-looking error
   * until the next full page reload. Retry a couple of times before exposing a
   * recoverable message, and keep any already loaded rows visible throughout.
   */
  async function loadList(archivedRows: boolean): Promise<ConversationBrief[]> {
    let lastError: unknown
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const data = await listConversations({
          group: 'all',
          archived: archivedRows,
          limit: 500,
        })
        return data.items ?? []
      } catch (error_) {
        lastError = error_
        if (attempt < 2) await new Promise((resolve) => window.setTimeout(resolve, 250 * 2 ** attempt))
      }
    }
    throw lastError
  }

  function load(): Promise<void> {
    if (loadingRequest) return loadingRequest
    loading.value = true
    loadingRequest = (async () => {
      try {
        items.value = await loadList(false)
        error.value = ''
      } catch (error_) {
        // Keep a previously loaded rail usable when the backend is restarting.
        // On a first load the list is already empty, so only the friendly
        // recoverable message is shown.
        error.value = message(error_)
      } finally {
        loading.value = false
        loadingRequest = null
      }
    })()
    return loadingRequest
  }

  function loadArchived(): Promise<void> {
    if (loadingArchivedRequest) return loadingArchivedRequest
    loadingArchivedRequest = (async () => {
      try {
        archived.value = await loadList(true)
        archivedError.value = ''
      } catch (error_) {
        archivedError.value = message(error_)
      } finally {
        loadingArchivedRequest = null
      }
    })()
    return loadingArchivedRequest
  }

  /** 流式开场（新建会话）或收尾（标题生成）后，把这一条就地更新，避免整表重拉 */
  function upsert(record: ConversationBrief): void {
    if (record.archived) {
      items.value = items.value.filter((item) => item.id !== record.id)
      archived.value = [record, ...archived.value.filter((item) => item.id !== record.id)]
      return
    }
    const next = items.value.filter((item) => item.id !== record.id)
    items.value = [record, ...next]
    archived.value = archived.value.filter((item) => item.id !== record.id)
  }

  function remove(id: string): void {
    items.value = items.value.filter((item) => item.id !== id)
    archived.value = archived.value.filter((item) => item.id !== id)
  }

  return {
    archived,
    archivedError,
    byProject,
    error,
    forProject,
    items,
    load,
    loadArchived,
    loading,
    remove,
    ungrouped,
    upsert,
  }
})
