<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 抓取任务监控窗口：绿色圆环进度 + 四个产出计数 + 各来源明细 + 事件流（固定高度滚动）。
 *
 * 数据全部来自 `GET /papers/fetch-jobs/{task_id}`（轮询由父组件负责）：
 * 计数/来源来自 `report`，事件流与阶段进度是后端逐条写入的真实数据（进度为阶段估算，文案如实标注"预计"）。
 * 失败或零产出时，顶部给出白话建议（406 / 429 / 403 / 网络）。
 */
import { computed, ref, watch } from 'vue'

import type { FetchSourceState, FetchJobStatus } from '@/api/papers'

const props = defineProps<{
  modelValue: boolean
  job: FetchJobStatus | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'retry'): void
  (e: 'pause'): void
  (e: 'resume'): void
  (e: 'cancel'): void
}>()

const SOURCE_LABELS: Record<string, string> = {
  arxiv: 'arXiv API',
  semantic_scholar: 'Semantic Scholar',
  openalex: 'OpenAlex',
  github: 'GitHub API',
}

const timelineRef = ref<HTMLElement | null>(null)

const status = computed(() => String(props.job?.status ?? 'accepted'))
const isRunning = computed(() => status.value === 'accepted' || status.value === 'running')
const isPaused = computed(() => Boolean(props.job?.control?.pause))
const isFailed = computed(() => status.value === 'failed')
const isCancelled = computed(() => status.value === 'cancelled')
const isDone = computed(() => status.value === 'done')

const counts = computed(() => props.job?.report?.counts ?? {})
const sources = computed(() => props.job?.report?.by_source ?? {})
const progress = computed(() => props.job?.progress ?? null)
const events = computed(() => props.job?.events ?? [])

/** 拉取异常：各来源失败请求次数之和 */
const failedTotal = computed(() =>
  Object.values(sources.value).reduce((sum, state) => sum + (state.failed ?? 0), 0),
)

const statusBadge = computed(() => {
  if (isFailed.value) return { text: '失败', cls: 'badge--fail' }
  if (isCancelled.value) return { text: '已终止', cls: 'badge--fail' }
  if (isPaused.value) return { text: '已暂停', cls: 'badge--warn' }
  if (isDone.value) return { text: '已完成', cls: 'badge--ok' }
  return { text: '进行中', cls: 'badge--run' }
})

const pct = computed(() => {
  if (isDone.value) return 100
  if (isFailed.value || isCancelled.value) return 100
  return Math.max(0, Math.min(100, Number(progress.value?.pct ?? 0)))
})

const ringOffset = computed(() => 439.8 * (1 - pct.value / 100))

const ringWhat = computed(() => {
  if (isCancelled.value) return '已终止'
  if (isFailed.value) return '已中断'
  if (isPaused.value) return '已暂停'
  if (isDone.value) return '已完成'
  return progress.value?.label ?? '准备中'
})

const ringEta = computed(() => {
  const used = Number(progress.value?.elapsed_seconds ?? 0)
  if (isDone.value || isFailed.value || isCancelled.value) return `用时 ${formatSeconds(used)}`
  if (isPaused.value) return '暂停中'
  const eta = progress.value?.eta_seconds
  return eta === null || eta === undefined ? '预计剩余 计算中' : `预计剩余 ${formatSeconds(eta)}`
})

function formatSeconds(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0s'
  if (value < 60) return `${Math.round(value)}s`
  const minutes = Math.floor(value / 60)
  const seconds = Math.round(value % 60)
  return `${minutes}m ${seconds}s`
}

function sourceRows(): Array<{ key: string; label: string; state: FetchSourceState }> {
  const order = ['arxiv', 'semantic_scholar', 'openalex', 'github']
  const keys = Object.keys(sources.value)
  return [...order.filter((k) => keys.includes(k)), ...keys.filter((k) => !order.includes(k))].map(
    (key) => ({ key, label: SOURCE_LABELS[key] ?? key, state: sources.value[key] ?? {} }),
  )
}

function sourceResult(state: FetchSourceState): { text: string; cls: string } {
  const requests = state.requests ?? 0
  const failed = state.failed ?? 0
  const ok = state.ok ?? 0
  if (requests === 0) return { text: '未请求', cls: 'tag--mute' }
  if (failed > 0 && ok === 0) return { text: '失败', cls: 'tag--fail' }
  if (failed > 0) return { text: '部分失败', cls: 'tag--warn' }
  if (ok > 0) return { text: '成功', cls: 'tag--ok' }
  return { text: '无结果', cls: 'tag--mute' }
}

/** 失败 / 零产出的白话建议 */
const advice = computed<{ title: string; body: string; tone: 'danger' | 'info' } | null>(() => {
  const discovered = Number(counts.value.discovered ?? 0)
  const reused = Number(counts.value.reused ?? 0)
  const created = Number(counts.value.created ?? 0)
  const codes = Object.values(sources.value)
    .map((state) => state.http_status)
    .filter((code): code is number => typeof code === 'number')
  const errors = Object.values(sources.value)
    .map((state) => String(state.last_error ?? ''))
    .join(' ')
    .toLowerCase()

  if (isCancelled.value) {
    return {
      title: '任务已终止',
      tone: 'info',
      body: '已经入库的论文都保留着，不会回滚。需要继续抓取就点右上角的重新同步。',
    }
  }

  const zeroYield = isDone.value && discovered === 0
  if (isFailed.value || zeroYield) {
    if (codes.includes(406) || errors.includes('406')) {
      return {
        title: '建议：arXiv 这次把我们拦下了（406）',
        tone: 'danger',
        body: '它是临时性的，不是配置坏了。等 1~2 分钟再点右上角的重新同步基本就好；连续失败可以把单轮条数从 30 降到 10。',
      }
    }
    if (codes.includes(429) || errors.includes('429')) {
      return {
        title: '建议：接口在限流（429）',
        tone: 'danger',
        body: '论文本身通常已经抓到，只是引用数和代码热度没补上。等 5 分钟再同步一次即可；想让 Semantic Scholar 稳定，去「设置」里补一个它的 API Key。',
      }
    }
    if (codes.includes(403) || errors.includes('403')) {
      return {
        title: '建议：缺少 Owner 令牌（403）',
        tone: 'danger',
        body: '去「设置」页把 Owner 令牌贴一次，再回来点重新同步。',
      }
    }
    if (errors.includes('timeout') || errors.includes('connect')) {
      return {
        title: '建议：请求超时或连不上外网',
        tone: 'danger',
        body: '多半是服务器出网不稳或对方响应慢。确认网络后重试；持续超时就降低单轮条数。',
      }
    }
    return {
      title: '建议：本轮没有取到数据',
      tone: 'danger',
      body: '看下面事件流里第一条红色的记录，那里写了具体原因；确认后点右上角的重新同步再跑一轮。',
    }
  }

  if (isDone.value && discovered > 0 && created === 0 && reused > 0) {
    return {
      title: '这批论文库里已经都有了',
      tone: 'info',
      body: `本轮解析到 ${discovered} 篇，全部与库内重复，没有新增。新投稿一般每天更新，明天再同步就会有新的。`,
    }
  }
  return null
})

function close(): void {
  emit('update:modelValue', false)
}

/** 事件流跟随最新一条（用户往上翻时不打扰） */
watch(
  () => events.value.length,
  async () => {
    await Promise.resolve()
    const el = timelineRef.value
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (nearBottom || isRunning.value) el.scrollTop = el.scrollHeight
  },
)

function timeOf(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

function finishText(): string {
  const used = Number(progress.value?.elapsed_seconds ?? 0)
  if (isRunning.value && !isPaused.value) return `已用时 ${formatSeconds(used)}`
  if (isPaused.value) return `已用时 ${formatSeconds(used)}（暂停中）`
  return `${isDone.value ? '完成于' : '结束于'} ${timeOf(props.job?.finished_at ?? '')} · 用时 ${formatSeconds(used)}`
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="scrim" @click.self="close">
      <div class="dialog scroll-y" role="dialog" aria-modal="true" aria-label="任务监控">
        <header class="head">
          <h2>任务监控</h2>
          <span class="badge" :class="statusBadge.cls">{{ statusBadge.text }}</span>
          <span class="spacer" />
          <button
            class="circle circle--retry"
            type="button"
            title="重新同步"
            aria-label="重新同步"
            :disabled="isRunning && !isPaused"
            @click="emit('retry')"
          >
            <svg width="17" height="17" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M3 8a5.1 5.1 0 0 1 8.6-3.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              <path d="M11.9 1.7v2.9H9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
              <path d="M13 8a5.1 5.1 0 0 1-8.6 3.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              <path d="M4.1 14.3v-2.9H7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
          <button
            class="circle circle--pause"
            type="button"
            :title="isPaused ? '继续任务' : '暂停任务'"
            :aria-label="isPaused ? '继续任务' : '暂停任务'"
            :disabled="!isRunning"
            @click="isPaused ? emit('resume') : emit('pause')"
          >
            <svg v-if="!isPaused" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <rect x="2.4" y="1.6" width="3.2" height="10.8" rx="1.6" fill="currentColor" />
              <rect x="8.4" y="1.6" width="3.2" height="10.8" rx="1.6" fill="currentColor" />
            </svg>
            <svg v-else width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
              <path d="M3.4 1.9 12.2 7l-8.8 5.1V1.9Z" fill="currentColor" />
            </svg>
          </button>
          <button
            class="circle circle--stop"
            type="button"
            title="终止任务（已拉取的论文会保留）"
            aria-label="终止任务"
            :disabled="!isRunning"
            @click="emit('cancel')"
          >
            <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true">
              <rect x="2" y="2" width="10" height="10" rx="2.4" fill="currentColor" />
            </svg>
          </button>
        </header>

        <div class="notice">关闭该窗口，任务仍可在后台继续运行</div>

        <div v-if="advice" class="advice" :class="`advice--${advice.tone}`">
          <span class="advice__mark">!</span>
          <div>
            <div class="advice__title">{{ advice.title }}</div>
            <div class="advice__body">{{ advice.body }}</div>
          </div>
        </div>

        <div class="main">
          <div class="ring-wrap" :class="{ 'ring-wrap--fail': isFailed || isCancelled }">
            <svg class="ring" width="164" height="164" viewBox="0 0 164 164" aria-hidden="true">
              <circle class="ring__track" cx="82" cy="82" r="70" />
              <circle class="ring__bar" cx="82" cy="82" r="70" :stroke-dashoffset="ringOffset" />
            </svg>
            <div class="ring-center">
              <div class="ring-pct">{{ pct }}%</div>
              <div class="ring-what">{{ ringWhat }}</div>
              <div class="ring-eta">{{ ringEta }}</div>
            </div>
          </div>

          <div class="counts">
            <div class="count">
              <div class="count__k">发现</div>
              <div class="count__v">{{ counts.discovered ?? 0 }}</div>
            </div>
            <div class="count">
              <div class="count__k">重复</div>
              <div class="count__v">{{ counts.reused ?? 0 }}</div>
            </div>
            <div class="count">
              <div class="count__k">实际拉取</div>
              <div class="count__v">{{ counts.created ?? 0 }}</div>
            </div>
            <div class="count" :class="{ 'count--bad': failedTotal > 0 }">
              <div class="count__k">拉取异常</div>
              <div class="count__v">{{ failedTotal }}</div>
            </div>
          </div>
        </div>

        <div class="section-head">
          <div class="section-title">各来源</div>
        </div>
        <table class="table">
          <thead>
            <tr>
              <th>来源</th>
              <th>结果</th>
              <th>HTTP</th>
              <th>请求</th>
              <th>失败</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in sourceRows()" :key="row.key">
              <td>{{ row.label }}</td>
              <td>
                <span class="tag" :class="sourceResult(row.state).cls">
                  {{ sourceResult(row.state).text }}
                </span>
              </td>
              <td class="mono">{{ row.state.http_status ?? '—' }}</td>
              <td>{{ row.state.requests ?? 0 }}</td>
              <td>{{ row.state.failed ?? 0 }}</td>
            </tr>
          </tbody>
        </table>

        <div class="section-head">
          <div class="section-title">事件流</div>
          <span class="count-badge">共 {{ events.length }} 条</span>
        </div>
        <div ref="timelineRef" class="timeline-wrap scroll-y">
          <ul class="timeline">
            <li v-for="(event, index) in events" :key="`${event.at}-${index}`" :class="event.level">
              <span class="t mono">{{ timeOf(event.at) }}</span>{{ event.text }}
            </li>
          </ul>
        </div>

        <footer class="foot">
          <span class="meta">{{ finishText() }}</span>
          <span class="meta mono">task_id={{ job?.task_id }}</span>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.scrim {
  position: fixed;
  inset: 0;
  z-index: 2400;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(15, 23, 42, 0.32);
  backdrop-filter: blur(10px) saturate(120%);
  -webkit-backdrop-filter: blur(10px) saturate(120%);
}

:global(:root[data-theme='dark']) .scrim {
  background: rgba(0, 0, 0, 0.58);
}

.dialog {
  width: min(780px, 100%);
  max-height: 88vh;
  overflow-y: auto;
  padding: 22px 26px 20px;
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-strong);
  border-radius: 18px;
  box-shadow: var(--shadow-popover);
  animation: pop 200ms cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes pop {
  from {
    opacity: 0;
    transform: translateY(8px) scale(0.985);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

.head {
  display: flex;
  align-items: center;
  gap: 12px;
}

.head h2 {
  margin: 0;
  font-size: var(--font-size-xl);
}

.spacer {
  flex: 1;
}

.circle {
  width: 36px;
  height: 36px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 50%;
  cursor: pointer;
  transition:
    transform 160ms cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 220ms,
    filter 160ms;
}

.circle svg {
  transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.circle:hover:not(:disabled) {
  transform: scale(1.07);
}

.circle:active:not(:disabled) {
  transform: scale(0.95);
}

.circle:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.circle--retry {
  background: var(--color-brand);
  color: var(--color-text-inverse);
}

.circle--retry:hover:not(:disabled) svg {
  transform: rotate(180deg);
  transition: transform 420ms cubic-bezier(0.16, 1, 0.3, 1);
}

.circle--pause {
  background: var(--color-success);
  color: #06210f; /* ui-polish-allow: 绿色圆底上的图标色，需固定深色保证对比度 */
}

.circle--stop {
  background: var(--color-danger);
  color: #2a0a0c; /* ui-polish-allow: 红色圆底上的图标色，需固定深色保证对比度 */
}

.notice {
  margin: 14px auto 0;
  width: fit-content;
  max-width: 100%;
  padding: 5px 16px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.advice {
  display: flex;
  gap: 10px;
  margin-top: 14px;
  padding: 12px 14px;
  border: 1px solid var(--color-border-strong);
  border-radius: 12px;
  background: var(--color-bg-subtle);
}

.advice--danger {
  border-color: var(--color-danger);
  background: var(--color-danger-soft);
}

.advice__mark {
  flex: none;
  font-weight: 700;
  color: var(--color-text-secondary);
}

.advice--danger .advice__mark,
.advice--danger .advice__title {
  color: var(--color-danger);
}

.advice__title {
  font-weight: 650;
}

.main {
  display: flex;
  align-items: center;
  gap: 22px;
  margin: 18px 0 6px;
}

.ring-wrap {
  position: relative;
  flex: none;
  width: 164px;
  height: 164px;
}

.ring {
  transform: rotate(-90deg);
}

.ring__track {
  fill: none;
  stroke: var(--color-bg-muted);
  stroke-width: 10;
}

.ring__bar {
  fill: none;
  stroke: var(--color-success);
  stroke-width: 10;
  stroke-linecap: round;
  stroke-dasharray: 439.8;
  transition:
    stroke-dashoffset 500ms cubic-bezier(0.4, 0, 0.2, 1),
    stroke 260ms;
}

.ring-wrap--fail .ring__bar {
  stroke: var(--color-danger);
}

.ring-center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  text-align: center;
}

.ring-pct {
  font-size: var(--font-size-xxl);
  font-weight: 700;
  line-height: 1;
}

.ring-what {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.ring-eta {
  font-size: var(--font-size-xs);
  color: var(--color-success);
}

.ring-wrap--fail .ring-eta {
  color: var(--color-danger);
}

.counts {
  flex: 1;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.count {
  padding: 12px 14px;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  transition:
    background-color 180ms,
    border-color 180ms,
    box-shadow 220ms,
    transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.count:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-card);
}

.count__k {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.count__v {
  font-size: var(--font-size-xl);
  line-height: 1.25;
  margin-top: 2px;
}

.count--bad .count__v {
  color: var(--color-danger);
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 18px 0 8px;
}

.section-title {
  font-size: var(--font-size-md);
  font-weight: 600;
}

.count-badge {
  padding: 1px 9px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.table {
  width: 100%;
  border-collapse: collapse;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
}

.table th,
.table td {
  padding: 9px 12px;
  text-align: left;
  border-bottom: 1px solid var(--color-border);
  font-size: var(--font-size-sm);
}

.table th {
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 500;
}

.table tbody tr {
  transition: background-color 160ms;
}

.table tbody tr:hover {
  background: var(--color-bg-subtle);
}

.table tbody tr:last-child td {
  border-bottom: 0;
}

.tag {
  display: inline-flex;
  padding: 1px 8px;
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
  font-size: var(--font-size-xs);
}

.tag--ok {
  background: var(--color-success-soft);
  color: var(--color-success);
  border-color: var(--color-success);
}

.tag--warn {
  background: var(--color-warning-soft);
  color: var(--color-warning);
  border-color: var(--color-warning);
}

.tag--fail {
  background: var(--color-danger-soft);
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.tag--mute {
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  border-color: var(--color-border-strong);
}

/* 事件流：固定高度窗口，内部滚动（条目再多也不拉长弹窗） */
.timeline-wrap {
  max-height: 176px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 10px 10px 0 12px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg-subtle);
  scrollbar-gutter: stable;
}


.timeline {
  margin: 0;
  padding: 0;
  list-style: none;
  border-left: 2px solid var(--color-border-strong);
}

.timeline li {
  position: relative;
  padding: 0 0 12px 16px;
  font-size: var(--font-size-sm);
  border-radius: 8px;
  transition: background-color 160ms;
}

.timeline li::before {
  content: '';
  position: absolute;
  left: -5px;
  top: 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-bg-subtle);
  border: 2px solid var(--color-border-strong);
  transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.timeline li:hover {
  background: var(--color-bg-muted);
}

.timeline li:hover::before {
  transform: scale(1.25);
}

.timeline li.ok::before {
  border-color: var(--color-success);
}

.timeline li.warn::before {
  border-color: var(--color-warning);
}

.timeline li.fail::before {
  border-color: var(--color-danger);
}

.timeline li.run::before {
  border-color: var(--color-success);
  background: var(--color-success);
}

.timeline .t {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  margin-right: 8px;
}

.foot {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--color-border);
}

.foot .meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.badge {
  display: inline-flex;
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  border: 1px solid transparent;
  font-size: var(--font-size-xs);
}

.badge--run,
.badge--ok {
  background: var(--color-success-soft);
  color: var(--color-success);
  border-color: var(--color-success);
}

.badge--warn {
  background: var(--color-warning-soft);
  color: var(--color-warning);
  border-color: var(--color-warning);
}

.badge--fail {
  background: var(--color-danger-soft);
  color: var(--color-danger);
  border-color: var(--color-danger);
}

@media (max-width: 720px) {
  .main {
    flex-direction: column;
  }
  .counts {
    width: 100%;
  }
}
</style>
