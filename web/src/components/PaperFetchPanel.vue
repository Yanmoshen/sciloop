<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 「按条件抓取论文」面板（**唯一实现**，两个入口共用）。
 *
 * 为什么抽出来：`文献总览 → 导入 → 按条件抓取` 与 `设置 → 拉取设置 → 手动抓取`
 * 必须是同一套请求与同一套参数口径，否则哪天改一处就会出现"设置页能选、导入页不认"。
 *
 * 口径（修改书 §3.3）
 * - 关键词：必填，提交前校验空值与数量（建议 2–5 个，多个按 AND 组合）；
 * - 时间区间：起止均含；用项目日期控件；
 * - 自动下载全文：固定开启（本轮不提供关闭开关）→ 下载失败计入抓取失败；
 * - 下载后立即解析：可选，默认关闭；解析失败与下载失败**分开**提示；
 * - 写入知识库：固定开启（本轮不提供关闭开关）。
 */
import { computed, onBeforeUnmount, ref } from 'vue'

import {
  triggerFetch,
  fetchFetchJob,
  type FetchJobCounts,
  type FetchJobStatus,
} from '@/api/papers'

const props = withDefaults(
  defineProps<{
    /** 是否允许写（Owner 面）；false 时提交按钮禁用 */
    canFetch?: boolean
    /** 是否显示"查看任务记录 / 前往论文库"链接（设置页用） */
    showLinks?: boolean
    /** 标题：设置页叫「手动抓取」，导入弹窗里叫「按条件抓取论文」 */
    title?: string
  }>(),
  { canFetch: false, showLinks: false, title: '按条件抓取论文' },
)

const keywords = ref('')
const dateRange = ref<[string, string] | null>(null)
const parseFulltext = ref(false)
const busy = ref(false)
const notice = ref('')
const noticeTone = ref<'ok' | 'fail'>('ok')
const accepted = ref<{ taskId: string; window: string } | null>(null)
const job = ref<FetchJobStatus | null>(null)

let pollTimer: ReturnType<typeof setInterval> | null = null

const TERMINAL = ['done', 'failed', 'cancelled', 'succeeded', 'partial']
const MAX_KEYWORDS = 5

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling(taskId: string): void {
  stopPolling()
  pollTimer = setInterval(() => {
    void (async () => {
      try {
        const snapshot = await fetchFetchJob(taskId)
        job.value = snapshot
        if (TERMINAL.includes(String(snapshot.status))) stopPolling()
      } catch {
        /* 轮询抖动不打断界面 */
      }
    })()
  }, 2000)
}

function ownerHint(error: unknown): string {
  const status = (error as { status?: number })?.status
  if (status === 403) return '当前为浏览模式：抓取是写操作，需先在「设置」里启用编辑。'
  return error instanceof Error ? error.message : String(error)
}

const parsedKeywords = computed(() =>
  keywords.value
    .split(/[,，;；\n]+/)
    .map((item) => item.trim())
    .filter(Boolean),
)

const windowLabel = computed(() => accepted.value?.window ?? '按默认时间窗')

const counts = computed<FetchJobCounts>(() => job.value?.counts ?? {})
/** 抓取任务级失败（整个任务没跑起来 / 中途挂了） */
const jobFailed = computed(() => String(job.value?.status ?? '') === 'failed')
/** 全文下载失败：**计入抓取失败**，单独一行说明 */
const downloadFailed = computed(() => Number(counts.value.fulltext_failed ?? 0))
/** 解析失败：与下载失败分开提示（下载到全文但解析没成） */
const jobErrors = computed(() => job.value?.report?.errors ?? [])
const parseErrors = computed(() =>
  jobErrors.value.filter((item) => String(item.stage ?? '').toLowerCase().includes('parse')),
)
/** 除解析外的抓取环节失败 */
const fetchErrors = computed(() =>
  jobErrors.value.filter((item) => !String(item.stage ?? '').toLowerCase().includes('parse')),
)

const statusText = computed(() => {
  const status = String(job.value?.status ?? '')
  if (status === 'done' || status === 'succeeded') return '已完成'
  if (status === 'failed') return '失败'
  if (status === 'partial') return '部分完成'
  if (status === 'cancelled') return '已取消'
  if (TERMINAL.includes(status)) return status
  return '进行中'
})

const statusTone = computed(() => {
  if (jobFailed.value) return 'fail'
  if (String(job.value?.status ?? '') === 'done' || String(job.value?.status ?? '') === 'succeeded') {
    return 'ok'
  }
  return 'warn'
})

async function submit(): Promise<void> {
  if (busy.value || !props.canFetch) return
  if (parsedKeywords.value.length === 0) {
    noticeTone.value = 'fail'
    notice.value = '请先填写关键词（建议 2–5 个英文关键词或短语）。'
    return
  }
  if (parsedKeywords.value.length > MAX_KEYWORDS) {
    noticeTone.value = 'fail'
    notice.value = `关键词最多 ${MAX_KEYWORDS} 个，多个关键词按 AND 组合。`
    return
  }
  busy.value = true
  notice.value = ''
  try {
    const result = await triggerFetch({
      keywords: parsedKeywords.value,
      date_from: dateRange.value?.[0] ?? null,
      date_to: dateRange.value?.[1] ?? null,
      download_fulltext: true,
      parse_fulltext: parseFulltext.value,
      write_knowledge_base: true,
    })
    const taskId = String(result.task_id ?? '')
    const window = dateRange.value ? `${dateRange.value[0]} ~ ${dateRange.value[1]}` : '按默认时间窗'
    accepted.value = { taskId, window }
    job.value = { task_id: taskId, status: 'accepted' }
    noticeTone.value = 'ok'
    notice.value = `抓取任务已创建：${taskId}。全文会自动下载；解析${
      parseFulltext.value ? '已启用' : '暂不执行，可在论文解析模块单独执行'
    }。`
    if (taskId) startPolling(taskId)
  } catch (error) {
    noticeTone.value = 'fail'
    notice.value = ownerHint(error)
  } finally {
    busy.value = false
  }
}

onBeforeUnmount(() => stopPolling())
</script>

<template>
  <article class="fetchp">
    <div class="fetchp__head">
      <h2 class="fetchp__title">{{ title }}</h2>
      <span class="fetchp__chip">全文自动下载</span>
      <span class="fetchp__chip">写入知识库</span>
    </div>

    <el-input
      v-model="keywords"
      type="textarea"
      :rows="2"
      placeholder="关键词（建议 2–5 个英文关键词或短语；多个关键词按 AND 组合，例如 large language model, inference optimization）"
    />

    <el-date-picker
      v-model="dateRange"
      type="daterange"
      value-format="YYYY-MM-DD"
      range-separator="至"
      start-placeholder="发布时间起始"
      end-placeholder="发布时间结束"
      :clearable="true"
    />

    <div class="fetchp__fixed">
      <span class="fetchp__fixed-item">自动下载全文：默认开启</span>
      <span class="fetchp__fixed-item">写入知识库：默认开启</span>
    </div>

    <el-checkbox v-model="parseFulltext">下载后立即解析全文（可选，默认关闭）</el-checkbox>

    <p v-if="notice" class="fetchp__notice" :class="`fetchp__notice--${noticeTone}`">
      {{ notice }}
    </p>

    <div class="fetchp__foot">
      <button
        class="btn btn--primary"
        type="button"
        :disabled="busy || !canFetch"
        @click="submit"
      >
        {{ busy ? '提交中…' : '开始抓取' }}
      </button>
      <template v-if="showLinks">
        <router-link class="fetchp__link" to="/papers">查看任务记录</router-link>
        <router-link class="fetchp__link" to="/papers/feed">前往论文库</router-link>
      </template>
    </div>

    <section v-if="accepted" class="fetchp__task">
      <div class="fetchp__task-row">
        <span class="fetchp__task-k">任务 ID</span>
        <span class="fetchp__task-v mono">{{ accepted.taskId }}</span>
        <span class="fetchp__tag" :class="`fetchp__tag--${statusTone}`">{{ statusText }}</span>
      </div>
      <div class="fetchp__task-row">
        <span class="fetchp__task-k">时间窗</span>
        <span class="fetchp__task-v">{{ windowLabel }}</span>
      </div>
      <div class="fetchp__task-row">
        <span class="fetchp__task-k">抓取结果</span>
        <span class="fetchp__task-v">
          新建 {{ counts.created ?? 0 }} · 复用 {{ counts.reused ?? 0 }} · 跳过重复 {{ counts.skipped_duplicate ?? 0 }}
        </span>
      </div>
      <div class="fetchp__task-row">
        <span class="fetchp__task-k">全文下载</span>
        <span class="fetchp__task-v">
          成功 {{ counts.fulltext_downloaded ?? 0 }}
          <template v-if="downloadFailed > 0"> · <em class="fetchp__bad">失败 {{ downloadFailed }}（计入抓取失败）</em></template>
        </span>
      </div>
      <div v-if="fetchErrors.length" class="fetchp__task-row">
        <span class="fetchp__task-k">抓取失败</span>
        <span class="fetchp__task-v fetchp__bad">
          {{ fetchErrors.length }} 项：{{ fetchErrors.map((item) => item.error ?? '未返回原因').join('；') }}
        </span>
      </div>
      <div v-if="parseErrors.length" class="fetchp__task-row">
        <span class="fetchp__task-k">解析失败</span>
        <span class="fetchp__task-v fetchp__bad">
          {{ parseErrors.length }} 项：{{ parseErrors.map((item) => item.error ?? '未返回原因').join('；') }}
        </span>
      </div>
      <p v-if="jobFailed && job?.error" class="fetchp__notice fetchp__notice--fail">{{ job.error }}</p>
      <router-link v-if="showLinks" class="fetchp__link" to="/papers">查看任务进度</router-link>
    </section>
  </article>
</template>

<style scoped>
.fetchp {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-card-bg);
}

.fetchp__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.fetchp__title {
  margin: 0;
  font-size: var(--font-size-lg);
}

.fetchp__chip {
  padding: 2px 8px;
  border-radius: var(--radius-pill);
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}

.fetchp__fixed {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.fetchp__notice {
  margin: 0;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--font-size-sm);
}

.fetchp__notice--ok {
  border-left: 3px solid var(--color-brand);
  color: var(--color-text-primary);
}

.fetchp__notice--fail {
  border-left: 3px solid var(--color-danger);
  color: var(--color-danger);
}

.fetchp__foot {
  display: flex;
  align-items: center;
  gap: 14px;
}

.btn {
  height: 34px;
  padding: 0 16px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.btn--primary {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.fetchp__link {
  color: var(--color-brand);
  font-size: var(--font-size-sm);
  text-decoration: none;
}

.fetchp__link:hover {
  text-decoration: underline;
}

.fetchp__task {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.fetchp__task-row {
  display: flex;
  align-items: baseline;
  gap: 12px;
  font-size: var(--font-size-sm);
}

.fetchp__task-k {
  width: 72px;
  flex: none;
  color: var(--color-text-secondary);
}

.fetchp__task-v {
  min-width: 0;
  overflow-wrap: anywhere;
}

.mono {
  font-family: var(--font-family-mono);
}

.fetchp__bad {
  color: var(--color-danger);
  font-style: normal;
}

.fetchp__tag {
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--color-border);
  font-size: var(--font-size-xs);
}

.fetchp__tag--ok {
  color: var(--color-success);
  border-color: var(--color-success);
}

.fetchp__tag--warn {
  color: var(--color-warning);
  border-color: var(--color-warning);
}

.fetchp__tag--fail {
  color: var(--color-danger);
  border-color: var(--color-danger);
}
</style>
