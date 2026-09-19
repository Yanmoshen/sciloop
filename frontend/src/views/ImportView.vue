<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文导入（核心模块 ①）：上传 PDF（拖拽 / 选择）+ 按 DOI / arXiv ID 批量导入，
 * 下方是本轮任务的实时进度与导入历史。
 *
 * 写操作全部是 Owner 面：public_demo 匿名调用会拿到 403，界面如实提示去「设置」页填令牌。
 */
import { computed, onUnmounted, ref } from 'vue'

import {
  fetchImportHistory,
  fetchImportJob,
  importIdentifiers,
  importPdfs,
  type ImportHistoryItem,
  type ImportJob,
} from '@/api/imports'

const files = ref<File[]>([])
const identifiers = ref('')
const dragging = ref(false)
const busy = ref('')
const notice = ref('')
const job = ref<ImportJob | null>(null)
const history = ref<ImportHistoryItem[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyLoading = ref(false)

let pollTimer: ReturnType<typeof setInterval> | null = null

const TERMINAL = ['done', 'failed', 'cancelled', 'succeeded', 'partial']
const isRunning = computed(() => !!job.value && !TERMINAL.includes(String(job.value.status)))
const fileSummary = computed(() =>
  files.value.reduce((sum, file) => sum + file.size, 0) / 1048576,
)

const MAX_FILES = 20
const MAX_BYTES = 20 * 1024 * 1024

function addFiles(list: FileList | null): void {
  if (!list) return
  notice.value = ''
  const next = [...files.value]
  for (const file of Array.from(list)) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      notice.value = `已跳过非 PDF 文件：${file.name}`
      continue
    }
    if (file.size > MAX_BYTES) {
      notice.value = `已跳过超过 20MB 的文件：${file.name}`
      continue
    }
    if (next.some((item) => item.name === file.name && item.size === file.size)) continue
    next.push(file)
  }
  if (next.length > MAX_FILES) {
    notice.value = `单次最多 ${MAX_FILES} 个文件，多余的已忽略`
    files.value = next.slice(0, MAX_FILES)
    return
  }
  files.value = next
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  addFiles(event.dataTransfer?.files ?? null)
}

function removeFile(index: number): void {
  files.value = files.value.filter((_, i) => i !== index)
}

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
        const snapshot = await fetchImportJob(taskId)
        job.value = snapshot
        if (TERMINAL.includes(String(snapshot.status))) {
          stopPolling()
          await loadHistory(1)
        }
      } catch {
        /* 轮询抖动不打断界面 */
      }
    })()
  }, 2000)
}

function ownerHint(error: unknown): string {
  const status = (error as { status?: number })?.status
  if (status === 403) {
    return 'public_demo 只读面无法导入（服务端 403）：请在「设置」页填入 OWNER_TOKEN 后重试。'
  }
  return error instanceof Error ? error.message : String(error)
}

async function submitFiles(): Promise<void> {
  if (files.value.length === 0 || busy.value) return
  busy.value = 'files'
  notice.value = ''
  try {
    const accepted = await importPdfs(files.value)
    files.value = []
    job.value = { task_id: accepted.task_id, status: 'accepted' }
    startPolling(accepted.task_id)
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

async function submitIdentifiers(): Promise<void> {
  const values = identifiers.value
    .split(/[\n,;、\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
  if (values.length === 0 || busy.value) return
  busy.value = 'ids'
  notice.value = ''
  try {
    const accepted = await importIdentifiers(values)
    identifiers.value = ''
    job.value = { task_id: accepted.task_id, status: 'accepted' }
    startPolling(accepted.task_id)
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

async function loadHistory(page: number): Promise<void> {
  historyLoading.value = true
  try {
    const result = await fetchImportHistory(page, 20)
    history.value = result.items ?? []
    historyTotal.value = result.total ?? 0
    historyPage.value = result.page ?? page
  } catch {
    /* 历史拉取失败不打断导入流程 */
  } finally {
    historyLoading.value = false
  }
}

function statusTag(status: string | null | undefined): { text: string; cls: string } {
  switch (status) {
    case 'created':
      return { text: '新建', cls: 'tag--ok' }
    case 'reused':
      return { text: '已存在', cls: 'tag--mute' }
    case 'duplicate':
      return { text: '重复', cls: 'tag--mute' }
    case 'failed':
      return { text: '失败', cls: 'tag--fail' }
    case 'done':
    case 'succeeded':
      return { text: '完成', cls: 'tag--ok' }
    case 'partial':
      return { text: '部分完成', cls: 'tag--warn' }
    case 'running':
    case 'accepted':
      return { text: '进行中', cls: 'tag--warn' }
    default:
      return { text: status ?? '—', cls: 'tag--mute' }
  }
}

function timeOf(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { hour12: false })
}

const counts = computed(() => job.value?.counts ?? {})
const jobItems = computed(() => job.value?.items ?? [])

void loadHistory(1)

onUnmounted(stopPolling)
</script>

<template>
  <section class="importer">
    <header class="importer__head">
      <h1>论文导入</h1>
      <span class="badge" :class="isRunning ? 'badge--warn' : 'badge--ok'">
        {{ isRunning ? '导入进行中' : '待导入' }}
      </span>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <div class="grid">
      <article class="panel">
        <div class="panel__head">
          <h2>上传 PDF</h2>
          <span class="chip">{{ files.length }} 个 · {{ fileSummary.toFixed(1) }} MB</span>
        </div>

        <label
          class="dropzone"
          :class="{ 'dropzone--on': dragging }"
          @dragover.prevent="dragging = true"
          @dragleave.prevent="dragging = false"
          @drop.prevent="onDrop"
        >
          <input
            class="dropzone__input"
            type="file"
            accept="application/pdf,.pdf"
            multiple
            @change="addFiles(($event.target as HTMLInputElement).files)"
          />
          <span class="dropzone__mark">PDF</span>
          <span class="dropzone__text">拖拽 PDF 到这里，或点击选择文件</span>
        </label>

        <ul v-if="files.length" class="filelist scroll-y">
          <li v-for="(file, index) in files" :key="`${file.name}-${index}`" class="filelist__item">
            <span class="filelist__name">{{ file.name }}</span>
            <span class="filelist__size">{{ (file.size / 1048576).toFixed(2) }} MB</span>
            <button class="icon-btn" type="button" title="移除" aria-label="移除" @click="removeFile(index)">
              <svg width="13" height="13" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
          </li>
        </ul>

        <footer class="panel__foot">
          <button
            class="btn btn--primary"
            type="button"
            :disabled="files.length === 0 || busy === 'files'"
            @click="submitFiles"
          >
            {{ busy === 'files' ? '提交中…' : '开始导入' }}
          </button>
          <button v-if="files.length" class="btn" type="button" @click="files = []">清空</button>
        </footer>
      </article>

      <article class="panel">
        <div class="panel__head">
          <h2>按标识符导入</h2>
        </div>
        <textarea
          v-model="identifiers"
          class="ids scroll-y"
          rows="7"
          placeholder="每行一个 DOI 或 arXiv ID，例如：&#10;10.48550/arXiv.2609.20756&#10;2609.20756"
        />
        <footer class="panel__foot">
          <button
            class="btn btn--primary"
            type="button"
            :disabled="!identifiers.trim() || busy === 'ids'"
            @click="submitIdentifiers"
          >
            {{ busy === 'ids' ? '提交中…' : '导入标识符' }}
          </button>
        </footer>
      </article>
    </div>

    <article v-if="job" class="panel">
      <div class="panel__head">
        <h2>本次导入</h2>
        <span class="tag" :class="statusTag(job.status).cls">{{ statusTag(job.status).text }}</span>
        <span class="mono task">{{ job.task_id }}</span>
      </div>

      <div class="counts">
        <div class="count"><div class="count__k">新建</div><div class="count__v">{{ counts.created ?? 0 }}</div></div>
        <div class="count"><div class="count__k">已存在</div><div class="count__v">{{ counts.reused ?? 0 }}</div></div>
        <div class="count"><div class="count__k">重复</div><div class="count__v">{{ counts.duplicate ?? 0 }}</div></div>
        <div class="count" :class="{ 'count--bad': (counts.failed ?? 0) > 0 }">
          <div class="count__k">失败</div><div class="count__v">{{ counts.failed ?? 0 }}</div>
        </div>
      </div>

      <ul v-if="jobItems.length" class="items scroll-y">
        <li v-for="(item, index) in jobItems" :key="index" class="items__row">
          <span class="items__name">{{ item.name ?? '—' }}</span>
          <span class="tag" :class="statusTag(item.status).cls">{{ statusTag(item.status).text }}</span>
          <span class="items__msg">{{ item.message ?? '' }}</span>
        </li>
      </ul>
    </article>

    <article class="panel">
      <div class="panel__head">
        <h2>导入历史</h2>
        <span class="chip">共 {{ historyTotal }} 条</span>
      </div>

      <table class="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>来源</th>
            <th>名称 / 标识符</th>
            <th>结果</th>
            <th>论文</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="historyLoading && history.length === 0">
            <td colspan="5" class="empty">加载中…</td>
          </tr>
          <tr v-else-if="history.length === 0">
            <td colspan="5" class="empty">还没有导入记录</td>
          </tr>
          <tr v-for="item in history" :key="item.id">
            <td class="mono">{{ timeOf(item.created_at) }}</td>
            <td>{{ item.source_type ?? '—' }}</td>
            <td class="ellipsis">{{ item.name_or_identifier ?? item.title ?? '—' }}</td>
            <td><span class="tag" :class="statusTag(item.status).cls">{{ statusTag(item.status).text }}</span></td>
            <td class="mono">{{ item.paper_id ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <footer class="pager">
        <button class="btn" type="button" :disabled="historyPage <= 1" @click="loadHistory(historyPage - 1)">
          上一页
        </button>
        <span class="pager__now">第 {{ historyPage }} 页</span>
        <button
          class="btn"
          type="button"
          :disabled="historyPage * 20 >= historyTotal"
          @click="loadHistory(historyPage + 1)"
        >
          下一页
        </button>
      </footer>
    </article>
  </section>
</template>

<style scoped>
.importer {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.importer__head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.importer__head h1 {
  margin: 0;
  font-size: var(--font-size-xl);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: var(--space-4);
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
.panel__foot {
  display: flex;
  gap: var(--space-2);
}
.dropzone {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-4);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: var(--color-bg-subtle);
  cursor: pointer;
  transition:
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}
.dropzone:hover,
.dropzone--on {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
  transform: translateY(-1px);
}
.dropzone__input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
.dropzone__mark {
  padding: 2px 10px;
  border-radius: var(--radius-pill);
  background: var(--color-brand);
  color: var(--color-text-inverse);
  font-size: var(--font-size-xs);
  font-weight: 600;
}
.dropzone__text {
  font-size: var(--font-size-md);
}
.filelist {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 180px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.filelist__item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: var(--space-2);
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  transition: background-color 160ms;
}
.filelist__item:hover {
  background: var(--color-bg-muted);
}
.filelist__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.filelist__size {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.ids {
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-sm);
  resize: vertical;
}
.ids:focus {
  outline: none;
  border-color: var(--color-brand);
}
.counts {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-2);
}
.count {
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1), box-shadow 200ms;
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
}
.count--bad .count__v {
  color: var(--color-danger);
}
.items {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 200px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.items__row {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) auto minmax(0, 1.6fr);
  align-items: center;
  gap: var(--space-2);
  padding: 6px 10px;
  border-bottom: 1px solid var(--color-border);
  font-size: var(--font-size-sm);
}
.items__name,
.items__msg {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.items__msg {
  color: var(--color-text-secondary);
}
.table {
  width: 100%;
  border-collapse: collapse;
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
.ellipsis {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty {
  color: var(--color-text-secondary);
  text-align: center;
}
.pager {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.pager__now {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.task {
  margin-left: auto;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
</style>
