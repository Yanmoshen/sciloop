<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文解析首屏：**点「论文解析」进来的第一个界面**。
 *
 * 两个区块，各自聚焦一件事：
 * - **最近解析**：每个解析任务一行（标题 + 时间 + 行内状态图标），固定高度、超出的滚轮看
 *   —— 行里**只放标题和时间**，状态用图标（解析中转圈 / 失败标记），细节点进去再看；
 *
 * 数据来源是**一个**接口（`GET /papers/parse-home`）：服务端已把"进行中的任务 ∪ 已落库的最新卡片"
 * 合并好、标题也拼好，前端不再各自拼一套时间与标题口径。
 *
 * 空状态只保留两个区块标题 —— 页面上没有"您还没有任何解析"这类说明性小字。
 */
import { ElMessageBox } from 'element-plus'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { fetchParseHome, rebuildCard, type ParseHomeResponse } from '@/api/parse'
import PaperImportDialog from '@/components/PaperImportDialog.vue'
import PaperPickerDialog from '@/components/PaperPickerDialog.vue'
import ViewStatePanel from '@/components/ViewStatePanel.vue'

const router = useRouter()

const loading = ref(true)
const error = ref<string | null>(null)
const data = ref<ParseHomeResponse | null>(null)
const pickerOpen = ref(false)
const importOpen = ref(false)

const recent = ref<ParseHomeResponse['recent']>([])

/** 「今天 10:02」/「昨天 18:20」/「09-22 20:11」——首屏要一眼看出新旧 */
function formatWhen(value: string | null | undefined): string {
  if (!value) return '时间未获取'
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return '时间未获取'
  const pad = (n: number): string => String(n).padStart(2, '0')
  const clock = `${pad(at.getHours())}:${pad(at.getMinutes())}`
  const now = new Date()
  const dayStart = (d: Date): number =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()
  const diffDays = Math.round((dayStart(now) - dayStart(at)) / 86400000)
  if (diffDays === 0) return `今天 ${clock}`
  if (diffDays === 1) return `昨天 ${clock}`
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${clock}`
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const response = await fetchParseHome(20)
    data.value = response
    recent.value = response.recent
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
    ensurePolling()
  }
}

// --------------------------------------------------------------------------- #
// 自动更新（用户 2026-09-24 实测反馈：提交后不刷新、跑完也不会变绿，都得手动刷）
//
// 口径：**有任务在跑就每 3 秒拉一次，跑到没有进行中就自动停**；
// 最多盯 5 分钟 —— 超过就停止轮询并如实提示"可能异常"，不做无限轮询。
// 标签页切到后台时暂停（省流量，回来再继续）。
// --------------------------------------------------------------------------- #
const POLL_INTERVAL_MS = 3000
const POLL_TIMEOUT_MS = 5 * 60 * 1000

const pollTimer = ref<number | null>(null)
const pollStartedAt = ref(0)
/** 盯超过 5 分钟仍未结束：停止轮询并提示可能异常 */
const pollTimedOut = ref(false)

function stopPolling(): void {
  if (pollTimer.value !== null) {
    window.clearTimeout(pollTimer.value)
    pollTimer.value = null
  }
}

function ensurePolling(): void {
  const running = recent.value.some((row) => row.status === 'running')
  if (!running) {
    // 都跑完了：复位并停表
    pollStartedAt.value = 0
    pollTimedOut.value = false
    stopPolling()
    return
  }
  if (!pollStartedAt.value) pollStartedAt.value = Date.now()
  if (Date.now() - pollStartedAt.value > POLL_TIMEOUT_MS) {
    pollTimedOut.value = true
    stopPolling()
    return
  }
  if (pollTimer.value !== null) return
  if (document.hidden) return // 后台标签页不轮询；回到前台由 visibilitychange 接管
  pollTimer.value = window.setTimeout(() => {
    pollTimer.value = null
    void load()
  }, POLL_INTERVAL_MS)
}

/** 弹窗提交了解析任务 → 立刻拉一次并开始盯（否则要等下一轮才发现有新任务） */
function onPickerSubmitted(): void {
  pollStartedAt.value = Date.now()
  pollTimedOut.value = false
  void load()
}

function onVisibilityChange(): void {
  if (!document.hidden) ensurePolling()
}

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange)
  void load()
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibilityChange)
  stopPolling()
})

function openPaper(paperId: number): void {
  void router.push({ path: `/papers/parse/${paperId}` })
}

/** 导入完成 → 追问是否解析（产品口径：导入成功才问，且只问一次）。
 *
 * 一次导入多篇时按"各按单篇解析"处理 —— 聚合是"多篇放一起比"，语义上不等价，
 * 不该替研究者替他决定。并发同样限 3。
 */
async function onImported(paperIds: number[]): Promise<void> {
  importOpen.value = false
  const count = paperIds.length
  try {
    await ElMessageBox.confirm(
      count === 1
        ? '已导入 1 篇论文，是否立即解析？'
        : `已导入 ${count} 篇论文，是否解析这 ${count} 篇（各按单篇解析）？`,
      '导入完成',
      { confirmButtonText: '开始解析', cancelButtonText: '暂不解析', type: 'info' },
    )
  } catch {
    return // 选了"暂不解析"：什么都不做，论文已在库里，随时可从「解析论文」再选
  }

  const queue = [...paperIds]
  const worker = async (): Promise<void> => {
    for (;;) {
      const id = queue.shift()
      if (id === undefined) return
      try {
        await rebuildCard(id, true)
      } catch {
        /* 单篇失败不连累其它：首屏列表会如实把它标成失败 */
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, () => worker()))
  await load()
}

</script>

<template>
  <div class="ph">
    <header class="ph__head">
      <div class="ph__title-block">
        <h1>论文解析</h1>
      </div>
      <div class="ph__actions">
        <button class="ph-btn ph-btn--primary" type="button" @click="pickerOpen = true">
          解析论文
        </button>
        <button class="ph-btn" type="button" @click="importOpen = true">外部导入</button>
      </div>
    </header>

    <ViewStatePanel
      :loading="loading"
      loading-text="正在加载解析记录…"
      :error="error"
      error-title="解析记录加载失败"
      retryable
      retry-label="重试加载"
      :busy="loading"
      @retry="load"
    />

    <section class="ph__block">
      <h2 class="ph__block-title">最近解析</h2>
      <div v-if="recent.length" class="ph__list scroll-y" data-role="recent-parse">
        <button
          v-for="row in recent"
          :key="row.paper_id"
          class="ph__row"
          type="button"
          @click="openPaper(row.paper_id)"
        >
          <span
            class="ph__dot"
            :class="`ph__dot--${row.status}`"
            :title="
              row.status === 'running' ? '解析中' : row.status === 'failed' ? '解析失败' : '已完成'
            "
            aria-hidden="true"
          />
          <span class="ph__row-title">{{ row.title || `论文 ${row.paper_id}` }}</span>
          <span class="ph__row-time">{{ formatWhen(row.at) }}</span>
        </button>
      </div>
      <!-- 盯满 5 分钟仍未结束：停止轮询并如实提示（不做无限轮询，也不假装它还在跑） -->
      <p v-if="pollTimedOut" class="ph__stale" data-role="poll-timeout">
        解析已超过 5 分钟仍未完成，可能异常，可稍后手动刷新查看。
      </p>
    </section>

    <!-- 「聚合解析」区块 2026-09-24 从界面下掉（后端聚合能力与数据保留） -->

    <PaperPickerDialog v-model:open="pickerOpen" @submitted="onPickerSubmitted" />
    <PaperImportDialog v-model:open="importOpen" @imported="onImported" />
  </div>
</template>

<style scoped>
.ph {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.ph__head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

.ph__title-block {
  flex: 1;
  min-width: 0;
}

.ph__title-block h1 {
  margin: 0;
  font-size: var(--font-size-xl);
  line-height: var(--line-height-tight);
}

.ph__actions {
  display: flex;
  gap: var(--space-2);
}

.ph-btn {
  padding: 5px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.ph-btn:hover {
  background: var(--color-bg-subtle);
}

.ph-btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.ph-btn--primary {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.ph__block-title {
  margin: 0 0 var(--space-2);
  font-size: var(--font-size-md);
}

/* 固定高度内滚：解析条数再多也不会把页面撑长（整页仍可滚） */
.ph__list {
  max-height: 300px;
  overflow-y: auto;
}

.ph__row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: 9px 0;
  border: 0;
  border-bottom: 1px solid var(--color-border);
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.ph__row:hover {
  background: var(--color-bg-subtle);
}

.ph__row:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: -2px;
}

/* 状态图标只用形状区分，不靠颜色单打独斗：实心=已完成 / 虚线圈=解析中 / 方块=失败 */
.ph__dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.ph__dot--ok {
  background: var(--color-success);
}

.ph__dot--running {
  border: 1.5px dashed var(--color-text-secondary);
  /* 虚线圆自转 = 经典 loading。以前这里只有虚线、没有动画，
     而文件注释写着"解析中转圈" —— 自述与实现不一致（用户一眼就看出来了）。 */
  animation: ph-dot-spin 0.9s linear infinite;
}

@keyframes ph-dot-spin {
  to {
    transform: rotate(360deg);
  }
}

/* 尊重系统的「减少动效」偏好：不转，但仍保留虚线形状以区分"进行中" */
@media (prefers-reduced-motion: reduce) {
  .ph__dot--running {
    animation: none;
  }
}

.ph__dot--failed {
  border-radius: 0;
  background: var(--color-danger);
}

.ph__stale {
  margin: var(--space-2) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.ph__row-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--font-size-sm);
  white-space: nowrap;
  text-overflow: ellipsis;
}

.ph__row-time {
  flex: 0 0 auto;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
</style>
