<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文翻译（核心模块 ②）：从已入库论文或上传 PDF 创建翻译任务，轮询进度，
 * 产出单语 / 双语 PDF 下载与逐块对照预览。
 *
 * 引擎是后端的 PyMuPDF 块级原位译写（`pymupdf-block-v1`），不是 pdf2zh；
 * `layout_warnings` 为后端如实输出的排版告警，界面原样展示。
 */
import { computed, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  cancelTranslateJob,
  createTranslateFromFile,
  createTranslateFromPaper,
  fetchTranslateJob,
  fetchTranslatePreview,
  listTranslateJobs,
  retryTranslateJob,
  translatePdfUrl,
  type TranslateJob,
  type TranslatePreviewBlock,
} from '@/api/translate'

const route = useRoute()
const router = useRouter()

const jobs = ref<TranslateJob[]>([])
const loading = ref(false)
const busy = ref('')
const notice = ref('')
const paperId = ref<number | null>(
  route.query.paper_id ? Number(route.query.paper_id) : null,
)
const mode = ref<'translate' | 'simplify'>('translate')
const detail = ref<TranslateJob | null>(null)
const previewBlocks = ref<TranslatePreviewBlock[]>([])
const previewLoading = ref(false)
const engine = ref('')

let pollTimer: ReturnType<typeof setInterval> | null = null

/**
 * ⚠️ 终态要按**后端真实的状态名**列（2026-09-26 修）。
 *
 * 改前写的是 `['accepted','running','queued','pending']` —— 其中前三个后端**根本没有**，
 * 而真实的运行态 `parsing / rewriting / rendering / highlighting` 一个都没认。
 * 结果：`hasRunning` 在真跑的时候恒为假 → **轮询从不启动** → 状态一动不动
 * （用户报的"状态不实时更新"就是它）。
 *
 * 口径改成"**不是终态就是在跑**"，这样后端以后加新的中间态也不会漏。
 */
const TERMINAL_STATUSES = ['completed', 'error', 'failed', 'cancelled']
const isRunning = (status: unknown): boolean => !TERMINAL_STATUSES.includes(String(status))
const hasRunning = computed(() => jobs.value.some((job) => isRunning(job.status)))

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function ensurePolling(): void {
  if (pollTimer !== null || !hasRunning.value) return
  pollTimer = setInterval(() => {
    void (async () => {
      await loadJobs()
      if (!hasRunning.value) stopPolling()
      if (detail.value) {
        const fresh = jobs.value.find((job) => job.task_id === detail.value?.task_id)
        if (fresh) detail.value = fresh
      }
    })()
  }, 2500)
}

function ownerHint(error: unknown): string {
  const status = (error as { status?: number })?.status
  if (status === 403) {
    return '只读浏览模式下没法创建翻译任务：到「设置」里切换成研究者身份后重试。'
  }
  const message = error instanceof Error ? error.message : String(error)
  // 论文库里没有这篇的本地原文（很常见：题录是抓来的，没有落盘 PDF）→ 光说"没有原文"没用，
  // 得给出可执行的出路（用户口径：界面不写解释性小字，但**错误必须有下一步**）。
  if (status === 409 && message.includes('原文')) {
    return `${message}  出路：用下面的「上传 PDF 翻译」直接传这篇的 PDF；或先把原文 PDF 导进论文库再点翻译。`
  }
  return message
}

async function loadJobs(): Promise<void> {
  loading.value = true
  try {
    const result = await listTranslateJobs(30)
    jobs.value = result.items ?? []
    engine.value = result.engine ?? ''
  } catch {
    /* 拉取失败保留上一份列表 */
  } finally {
    loading.value = false
  }
}

async function createFromPaper(): Promise<void> {
  if (!paperId.value || busy.value) return
  busy.value = 'create'
  notice.value = ''
  try {
    const accepted = await createTranslateFromPaper(paperId.value, { mode: mode.value })
    await loadJobs()
    ensurePolling()
    if (accepted.task_id) await openDetail(accepted.task_id)
    await router.replace({ query: {} })
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

async function createFromFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || busy.value) return
  busy.value = 'upload'
  notice.value = ''
  try {
    const accepted = await createTranslateFromFile(file, { mode: mode.value })
    await loadJobs()
    ensurePolling()
    if (accepted.task_id) await openDetail(accepted.task_id)
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

async function openDetail(taskId: string): Promise<void> {
  previewBlocks.value = []
  detail.value = jobs.value.find((job) => job.task_id === taskId) ?? null
  await refreshDetail(taskId)
  void loadPreview(taskId)
}

async function refreshDetail(taskId: string): Promise<void> {
  try {
    detail.value = await fetchTranslateJob(taskId)
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  }
}

async function loadPreview(taskId: string): Promise<void> {
  previewLoading.value = true
  try {
    const preview = await fetchTranslatePreview(taskId)
    previewBlocks.value = preview.blocks ?? []
  } catch {
    previewBlocks.value = []
  } finally {
    previewLoading.value = false
  }
}

async function action(kind: 'cancel' | 'retry', job: TranslateJob): Promise<void> {
  if (busy.value) return
  busy.value = kind
  notice.value = ''
  try {
    if (kind === 'cancel') await cancelTranslateJob(job.task_id)
    else await retryTranslateJob(job.task_id)
    await loadJobs()
    ensurePolling()
    if (detail.value?.task_id === job.task_id) await refreshDetail(job.task_id)
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

/** 状态文案（用户口径 2026-09-26：已完成 / 正在翻译 / 未完成）。 */
function statusTag(status: string): { text: string; cls: string; running: boolean } {
  switch (status) {
    case 'completed':
      return { text: '已完成', cls: 'tag--ok', running: false }
    case 'error':
    case 'failed':
      return { text: '未完成', cls: 'tag--fail', running: false }
    case 'cancelled':
      return { text: '已取消', cls: 'tag--mute', running: false }
    default:
      // 其余全是中间态（pending / parsing / rewriting / rendering / highlighting …）
      return { text: '正在翻译', cls: 'tag--warn', running: true }
  }
}

function timeOf(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', { hour12: false })
}

function download(taskId: string, format: 'mono' | 'dual'): void {
  window.open(translatePdfUrl(taskId, format), '_blank', 'noopener')
}

const detailWarnings = computed(() => detail.value?.layout_warnings ?? [])

/**
 * 带着 `?paper_id=` 进来（从阅读页点「翻译该论文」）→ **直接开跑**，不让研究者再点一次。
 *
 * 用户口径 2026-09-26：「跳转到论文翻译界面后，应该直接新建一个翻译任务，
 * 直接翻译刚刚那篇论文，不需要我手动再去选择」。
 *
 * 两条保护：
 *  · 只在**这次进入**触发一次（`autoStarted`），页面内不会重复建；
 *  · 同一条论文**已经有任务在跑**时不重复建（重复建 = 白烧一次整篇翻译）。
 * `createFromPaper()` 成功后会清掉 query，所以刷新页面也不会再建一条。
 */
const autoStarted = ref(false)

async function autoStartFromQuery(): Promise<void> {
  if (autoStarted.value || !paperId.value) return
  autoStarted.value = true
  const running = jobs.value.find(
    (job) => job.paper_id === paperId.value && isRunning(job.status),
  )
  if (running) {
    notice.value = '这篇的翻译已经在跑了，进度见下面的列表。'
    await openDetail(running.task_id)
    void router.replace({ query: {} })
    return
  }
  await createFromPaper()
}

void loadJobs().then(() => {
  ensurePolling()
  void autoStartFromQuery()
})
onUnmounted(stopPolling)
</script>

<template>
  <section class="translator">
    <header class="translator__head">
      <h1>论文翻译</h1>
      <span v-if="engine" class="chip">{{ engine }}</span>
      <span class="badge" :class="hasRunning ? 'badge--warn' : 'badge--ok'">
        {{ hasRunning ? '有任务进行中' : '空闲' }}
      </span>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <article class="panel">
      <div class="panel__head">
        <h2>创建翻译任务</h2>
      </div>
      <div class="creator">
        <label class="field">
          <span class="field__label">论文 ID</span>
          <input v-model.number="paperId" class="field__input" type="number" min="1" placeholder="例如 547" />
        </label>
        <label class="field">
          <span class="field__label">模式</span>
          <select v-model="mode" class="field__input">
            <option value="translate">全文翻译</option>
            <option value="simplify">通俗简化</option>
          </select>
        </label>
        <button class="btn btn--primary" type="button" :disabled="!paperId || busy === 'create'" @click="createFromPaper">
          {{ busy === 'create' ? '提交中…' : '用库内原文翻译' }}
        </button>
        <label class="btn btn--file" :class="{ 'btn--busy': busy === 'upload' }">
          {{ busy === 'upload' ? '上传中…' : '上传 PDF 翻译' }}
          <input type="file" accept="application/pdf,.pdf" @change="createFromFile" />
        </label>
      </div>
    </article>

    <article class="panel">
      <div class="panel__head">
        <h2>翻译任务</h2>
        <span class="chip">共 {{ jobs.length }} 条</span>
        <button class="btn btn--ghost" type="button" @click="loadJobs">刷新</button>
      </div>

      <table class="table">
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>进度</th>
            <th>模式</th>
            <th>来源</th>
            <th>时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading && jobs.length === 0">
            <td colspan="7" class="empty">加载中…</td>
          </tr>
          <tr v-else-if="jobs.length === 0">
            <td colspan="7" class="empty">还没有翻译任务</td>
          </tr>
          <tr v-for="job in jobs" :key="job.task_id">
            <td class="mono">{{ job.task_id }}</td>
            <td>
              <span class="tag" :class="statusTag(String(job.status)).cls">
                <span v-if="statusTag(String(job.status)).running" class="spin" aria-hidden="true" />
                {{ statusTag(String(job.status)).text }}
              </span>
            </td>
            <td class="progress-cell">
              <span class="progress"><span class="progress__bar" :style="{ width: `${Math.min(100, job.percent ?? 0)}%` }" /></span>
              <span class="mono pct">{{ job.percent ?? 0 }}%</span>
            </td>
            <td>{{ job.mode === 'simplify' ? '通俗简化' : '全文翻译' }}</td>
            <td class="ellipsis">{{ job.source?.filename ?? (job.paper_id ? `论文 #${job.paper_id}` : '—') }}</td>
            <td class="mono">{{ timeOf(job.created_at) }}</td>
            <td class="ops">
              <button class="link-btn" type="button" @click="openDetail(job.task_id)">详情</button>
              <button
                v-if="job.formats?.mono?.available"
                class="link-btn"
                type="button"
                @click="download(job.task_id, 'mono')"
              >
                单语 PDF
              </button>
              <button
                v-if="job.formats?.dual?.available"
                class="link-btn"
                type="button"
                @click="download(job.task_id, 'dual')"
              >
                双语 PDF
              </button>
              <button
                v-if="isRunning(job.status)"
                class="link-btn link-btn--mute"
                type="button"
                @click="action('cancel', job)"
              >
                取消
              </button>
              <button
                v-else-if="job.status === 'failed'"
                class="link-btn"
                type="button"
                @click="action('retry', job)"
              >
                重试
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </article>

    <Teleport to="body">
      <div v-if="detail" class="scrim" @click.self="detail = null">
        <div class="dialog" role="dialog" aria-modal="true" aria-label="翻译任务详情">
          <header class="dialog__head">
            <h2>翻译任务 {{ detail.task_id }}</h2>
            <span class="tag" :class="statusTag(String(detail.status)).cls">
              <span v-if="statusTag(String(detail.status)).running" class="spin" aria-hidden="true" />
              {{ statusTag(String(detail.status)).text }}
            </span>
            <span class="spacer" />
            <button class="icon-btn" type="button" title="关闭" aria-label="关闭" @click="detail = null">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
          </header>

          <div class="dialog__meta">
            <span class="chip">{{ detail.mode === 'simplify' ? '通俗简化' : '全文翻译' }}</span>
            <span class="chip">进度 {{ detail.percent ?? 0 }}%</span>
            <span v-if="detail.stage" class="chip">{{ detail.stage }}</span>
            <span v-if="detail.source?.pages" class="chip">{{ detail.source.pages }} 页</span>
          </div>

          <p v-if="detail.message" class="dialog__line">{{ detail.message }}</p>
          <p v-if="detail.error" class="dialog__line dialog__line--err">{{ detail.error }}</p>

          <ul v-if="detailWarnings.length" class="warnings scroll-y">
            <li v-for="(warning, index) in detailWarnings" :key="index">{{ warning }}</li>
          </ul>

          <div class="panel__head">
            <h3>逐块对照</h3>
            <span class="chip">{{ previewBlocks.length }} 块</span>
          </div>
          <p v-if="previewLoading" class="empty">加载中…</p>
          <ul v-else-if="previewBlocks.length" class="pairs scroll-y">
            <li v-for="(block, index) in previewBlocks" :key="index" class="pair">
              <div class="pair__col pair__col--src">{{ block.source_text ?? '—' }}</div>
              <div class="pair__col pair__col--dst">{{ block.target_text ?? '—' }}</div>
            </li>
          </ul>
          <p v-else class="empty">本次任务没有可预览的对照文件</p>

          <footer class="dialog__foot">
            <button
              v-if="detail.formats?.mono?.available"
              class="btn btn--primary"
              type="button"
              @click="download(detail.task_id, 'mono')"
            >
              下载单语 PDF
            </button>
            <button
              v-if="detail.formats?.dual?.available"
              class="btn"
              type="button"
              @click="download(detail.task_id, 'dual')"
            >
              下载双语 PDF
            </button>
            <button class="btn" type="button" @click="detail = null">关闭</button>
          </footer>
        </div>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.translator {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.translator__head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.translator__head h1 {
  margin: 0;
  font-size: var(--font-size-xl);
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
.panel__head h2,
.panel__head h3 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.creator {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-3);
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 160px;
}
.field__label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.field__input {
  height: 34px;
  padding: 0 12px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
}
.field__input:focus {
  outline: none;
  border-color: var(--color-brand);
}
.btn--file {
  position: relative;
  display: inline-flex;
  align-items: center;
  cursor: pointer;
}
.btn--file input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
.btn--busy {
  opacity: 0.6;
  pointer-events: none;
}
.btn--ghost {
  margin-left: auto;
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
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty {
  color: var(--color-text-secondary);
  text-align: center;
}
.progress-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 140px;
}
.progress {
  flex: 1;
  height: 6px;
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  overflow: hidden;
}
.progress__bar {
  display: block;
  height: 100%;
  border-radius: var(--radius-pill);
  background: var(--color-brand);
  transition: width 400ms cubic-bezier(0.4, 0, 0.2, 1);
}
.pct {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.ops {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
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
  width: min(880px, 100%);
  max-height: 86vh;
  overflow-y: auto;
  padding: var(--space-5);
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.dialog__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.dialog__head h2 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.spacer {
  flex: 1;
}
.dialog__meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.dialog__line {
  margin: 0;
  font-size: var(--font-size-sm);
}
.dialog__line--err {
  color: var(--color-danger);
}
.warnings {
  margin: 0;
  padding: var(--space-3) var(--space-3) var(--space-3) var(--space-5);
  max-height: 140px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.pairs {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.pair {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--font-size-sm);
  transition: border-color 160ms;
}
.pair:hover {
  border-color: var(--color-brand);
}
.pair__col {
  white-space: pre-wrap;
  word-break: break-word;
}
.pair__col--src {
  color: var(--color-text-secondary);
}
.dialog__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

/* 「正在翻译」旁边的转圈（用户口径 2026-09-26：正在翻译要有个转圈的图案）。
   纯 CSS：一圈 currentColor 的环 + 顶上开口，转起来就是转圈，不引任何图标资源。 */
.spin {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-right: 4px;
  vertical-align: -1px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: translate-spin 0.8s linear infinite;
}

@keyframes translate-spin {
  to {
    transform: rotate(360deg);
  }
}

/* 偏好「减少动态」时不要闪个不停，放慢即可（还要能看出它在跑） */
@media (prefers-reduced-motion: reduce) {
  .spin {
    animation-duration: 2.4s;
  }
}
</style>
