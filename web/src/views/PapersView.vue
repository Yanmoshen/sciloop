<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 文献总览（2026-09-21 结构调整）：讲"这个库的整体状况"。
 *   · 数据源健康（从论文库搬来：先看数据新不新鲜、有没有降级）
 *   · 四项关键计数（「已解析」为统一口径：全文成功 **且** 有解析卡片）
 *   · 两个图：解析构成（圆环）+ 论文与解析趋势（累计线 + 新增细柱）
 *   · 逐篇挑选与批量操作已搬到论文库（原筛选行/表格/分页/批量栏）
 */
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { fetchPapersOverview, type PapersOverview } from '@/api/papers'
import PaperImportDialog from '@/components/PaperImportDialog.vue'
import PaperTrendCharts from '@/components/PaperTrendCharts.vue'
import SourceHealthBar from '@/components/SourceHealthBar.vue'
import TaskMonitorDialog from '@/components/TaskMonitorDialog.vue'
import { useTaskStore } from '@/stores/tasks'
import { writeDenied } from '@/utils/messages'

const route = useRoute()
const tasks = useTaskStore()

const overview = ref<PapersOverview | null>(null)
const overviewError = ref('')

const busy = ref('')
const notice = ref('')

/** 论文导入弹窗（原独立页 /papers/import 已并入此弹窗） */
const importOpen = ref(false)

/** 任务监控：轮询与控制统一由 stores/tasks 负责（顶栏「任务」入口复用同一份实现） */
const monitorOpen = ref(false)

function num(value: number | null | undefined): string {
  return value === null || value === undefined ? '未获取' : String(value)
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

function openImport(): void {
  importOpen.value = true
}

async function sync(): Promise<void> {
  if (busy.value) return
  busy.value = 'sync'
  notice.value = ''
  try {
    monitorOpen.value = true
    const result = await tasks.startFetch(30)
    if (!result.ok) {
      notice.value =
        result.status === 403 ? writeDenied('重新同步（触发抓取）') : result.message
      return
    }
    await loadOverview()
  } finally {
    busy.value = ''
  }
}

/** 暂停 / 继续 / 终止（Owner 面；无权限或已结束会如实提示） */
async function controlTask(kind: 'pause' | 'resume' | 'cancel'): Promise<void> {
  const result = await tasks.control(kind)
  if (result.ok) {
    if (kind === 'cancel') await loadOverview()
    return
  }
  notice.value =
    result.status === 403
      ? writeDenied('暂停 / 继续 / 终止任务')
      : result.status === 409
        ? '任务已经结束，无需再操作。'
        : (result.message ?? '操作失败')
}

onMounted(() => {
  if (route.query.import) importOpen.value = true
  void loadOverview()
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

    <!-- 数据源健康：能不能同步、有没有降级，看数字之前先看这里 -->
    <SourceHealthBar />

    <!-- 四项关键计数：全部为库内真实计数，null 显示「未获取」，不做估算 -->
    <div class="stats">
      <div class="stat">
        <div class="stat__label">论文总量</div>
        <div class="stat__value">{{ overview ? num(overview.papers_total) : '—' }}</div>
      </div>
      <div class="stat">
        <div class="stat__label">已解析</div>
        <div class="stat__value">{{ overview ? num(overview.papers_parsed) : '—' }}</div>
      </div>
      <div class="stat">
        <div class="stat__label">未解析</div>
        <div class="stat__value">{{ overview ? num(overview.papers_unparsed) : '—' }}</div>
      </div>
      <div class="stat">
        <div class="stat__label">多篇聚合解析</div>
        <div class="stat__value">{{ overview ? num(overview.aggregations_total) : '—' }}</div>
      </div>
    </div>
    <p v-if="overviewError" class="hint hint--err">统计获取失败：{{ overviewError }}</p>

    <!-- 两个图：数据来自 /papers/overview 与 /papers/trends（口径与上面的计数完全一致） -->
    <PaperTrendCharts :overview="overview" />

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
</style>
