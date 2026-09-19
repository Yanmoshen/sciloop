<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 同步任务历史：列出之前跑过的抓取任务（新的在前）。
 *
 * 点任意一行 → 打开该任务的**完整监控窗口**（圆环进度 / 四项计数 / 各来源 / 事件流），
 * 而不是只展开事件流。数据来自 `GET /papers/fetch-jobs`（内存任务 + 落盘快照）。
 */
import { computed, watch } from 'vue'

import type { FetchTaskSummary } from '@/api/papers'
import { useTaskStore } from '@/stores/tasks'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'open', taskId: string): void
}>()

const tasks = useTaskStore()

const total = computed(() => tasks.recent.length)

watch(
  () => props.modelValue,
  (open) => {
    if (open) void tasks.loadRecent(30)
  },
  { immediate: true },
)

function statusTag(status: string): { text: string; cls: string } {
  if (status === 'done') return { text: '已完成', cls: 'tag--ok' }
  if (status === 'cancelled') return { text: '已终止', cls: 'tag--mute' }
  if (status === 'failed') return { text: '失败', cls: 'tag--fail' }
  if (status === 'running' || status === 'accepted') return { text: '进行中', cls: 'tag--warn' }
  return { text: status, cls: 'tag--mute' }
}

function timeOf(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : parsed.toLocaleString('zh-CN', {
        hour12: false,
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
}

function duration(item: FetchTaskSummary): string {
  const seconds = Number(item.duration_seconds ?? 0)
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
}

function openTask(task: FetchTaskSummary): void {
  emit('update:modelValue', false)
  emit('open', task.task_id)
}

function close(): void {
  emit('update:modelValue', false)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="scrim" @click.self="close">
      <div class="dialog scroll-y" role="dialog" aria-modal="true" aria-label="同步任务历史">
        <header class="head">
          <h2>同步任务历史</h2>
          <span class="badge">共 {{ total }} 条</span>
          <span class="spacer" />
          <button
            class="circle circle--refresh"
            type="button"
            title="刷新列表"
            aria-label="刷新列表"
            :disabled="tasks.loadingRecent"
            @click="tasks.loadRecent(30)"
          >
            <svg width="17" height="17" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M3 8a5.1 5.1 0 0 1 8.6-3.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              <path d="M11.9 1.7v2.9H9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
              <path d="M13 8a5.1 5.1 0 0 1-8.6 3.7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
              <path d="M4.1 14.3v-2.9H7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </header>

        <p v-if="tasks.loadingRecent && total === 0" class="state">加载中…</p>
        <p v-else-if="total === 0" class="state">还没有同步任务记录</p>

        <ul v-else class="history">
          <li v-for="item in tasks.recent" :key="item.task_id" class="item">
            <button class="row" type="button" @click="openTask(item)">
              <span class="row__time">{{ timeOf(item.started_at) }}</span>
              <span class="tag" :class="statusTag(item.status).cls">{{ statusTag(item.status).text }}</span>
              <span class="row__nums">
                <span class="num"><b>{{ item.counts?.discovered ?? 0 }}</b>发现</span>
                <span class="num"><b>{{ item.counts?.created ?? 0 }}</b>实际拉取</span>
                <span class="num"><b>{{ item.counts?.reused ?? 0 }}</b>重复</span>
                <span class="num" :class="{ 'num--bad': (item.failed_total ?? 0) > 0 }">
                  <b>{{ item.failed_total ?? 0 }}</b>异常
                </span>
                <span class="num"><b>{{ duration(item) }}</b>用时</span>
              </span>
              <span class="row__id mono">{{ item.task_id }}</span>
              <span class="row__go">查看监控</span>
            </button>
          </li>
        </ul>
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
  background: rgba(15, 23, 42, 0.32); /* ui-polish-allow: 遮罩色与主题解耦 */
  backdrop-filter: blur(10px) saturate(120%);
  -webkit-backdrop-filter: blur(10px) saturate(120%);
}

:global(:root[data-theme='dark']) .scrim {
  background: rgba(0, 0, 0, 0.58); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.dialog {
  width: min(900px, 100%);
  max-height: 86vh;
  overflow-y: auto;
  padding: 22px 26px 20px;
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-strong);
  border-radius: 18px;
  box-shadow: var(--shadow-popover);
  animation: pop 200ms cubic-bezier(0.4, 0, 0.2, 1);
  scrollbar-gutter: stable;
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
  margin-bottom: 16px;
}

.head h2 {
  margin: 0;
  font-size: var(--font-size-xl);
}

.spacer {
  flex: 1;
}

.badge {
  display: inline-flex;
  padding: 3px 10px;
  border-radius: var(--radius-pill);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.circle {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition:
    transform 160ms cubic-bezier(0.16, 1, 0.3, 1),
    color 160ms,
    border-color 160ms;
}

.circle:hover:not(:disabled) {
  color: var(--color-brand);
  border-color: var(--color-brand);
  transform: scale(1.06);
}

.circle:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.state {
  margin: 0;
  padding: 16px 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.history {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.item {
  border: 1px solid var(--color-border);
  border-radius: 10px;
  overflow: hidden;
  background: var(--color-bg-subtle);
  transition: border-color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.item:hover {
  border-color: var(--color-brand);
}

.row {
  width: 100%;
  display: grid;
  grid-template-columns: 150px 78px 1fr 130px 96px;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 0;
  background: transparent;
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.row:hover {
  background: var(--color-bg-muted);
}

.row__time {
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

.row__nums {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}

.num {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.num b {
  color: var(--color-text-primary);
  font-weight: 600;
  font-size: var(--font-size-sm);
  margin-right: 4px;
}

.num--bad b {
  color: var(--color-danger);
}

.row__id {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.row__go {
  justify-self: end;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  transition: color 160ms;
}

.row:hover .row__go {
  color: var(--color-brand);
}

.tag {
  display: inline-flex;
  justify-content: center;
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

@media (max-width: 860px) {
  .row {
    grid-template-columns: 1fr 78px 96px;
  }
  .row__nums,
  .row__id {
    display: none;
  }
}
</style>
