<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全文阅读（核心模块 ③）：**原文 PDF 与译文 PDF 左右并排，原汁原味地读**。
 *
 * 需求确认（2026-09-19，用户明确）：
 * - 左栏原文 PDF / 右栏译文 PDF，两栏各自滚动
 * - 批注**直接高亮在 PDF 页面上**（按后端给的 page + bbox 叠矩形），点高亮可看/改/删
 * - 「阅读就是阅读」：**不放**结构大纲、已理解标记、字号、逐块对照文本等辅助面板
 *
 * 渲染由 `components/PdfPane.vue` 用 PDF.js 完成；批注读写走既有 `/reader/...` 接口。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  createReaderAnnotation,
  deleteReaderAnnotation,
  fetchReaderDocument,
  listReaderAnnotations,
  listReaderDocuments,
  listReaderVersions,
  registerReaderDocument,
  registerReaderVersion,
  readerVersionPdfUrl,
  updateReaderAnnotation,
  type ReaderAnnotation,
  type ReaderDocumentSummary,
  type ReaderVersion,
} from '@/api/reader'
import PdfPane from '@/components/PdfPane.vue'

const route = useRoute()
const router = useRouter()

// ---- 文档列表态 ----
const documents = ref<ReaderDocumentSummary[]>([])
const documentsTotal = ref(0)
const registerPaperId = ref<number | null>(null)

// ---- 阅读态 ----
const detail = ref<ReaderDocumentSummary | null>(null)
const versions = ref<ReaderVersion[]>([])
const sourceVersionId = ref<number | null>(null)
const targetVersionId = ref<number | null>(null)
const annotations = ref<ReaderAnnotation[]>([])

const loading = ref(false)
const busy = ref('')
const notice = ref('')

// ---- 批注气泡 ----
const bubble = ref<'create' | 'view' | null>(null)
const bubbleStyle = ref<Record<string, string>>({})
const draft = ref({ page: 1, text: '', kind: 'highlight', note: '' })
const activeAnnotation = ref<ReaderAnnotation | null>(null)
const editingNote = ref('')

const documentId = computed(() => {
  const raw = route.params.documentId
  const value = Array.isArray(raw) ? raw[0] : raw
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
})
const inReading = computed(() => documentId.value !== null)

const sourceVersion = computed(
  () => versions.value.find((item) => item.id === sourceVersionId.value) ?? null,
)
const targetVersion = computed(
  () => versions.value.find((item) => item.id === targetVersionId.value) ?? null,
)
const translationVersions = computed(() => versions.value.filter((item) => item.kind !== 'original'))

const sourcePdfUrl = computed(() =>
  documentId.value && sourceVersionId.value
    ? readerVersionPdfUrl(documentId.value, sourceVersionId.value)
    : '',
)
const targetPdfUrl = computed(() =>
  documentId.value && targetVersionId.value
    ? readerVersionPdfUrl(documentId.value, targetVersionId.value)
    : '',
)
/** 批注按版本分栏显示：左栏只看原文版本上的批注，右栏看译文版本上的 */
const sourceAnnotations = computed(() =>
  annotations.value.filter((item) => item.version_id === sourceVersionId.value),
)
const targetAnnotations = computed(() =>
  annotations.value.filter((item) => item.version_id === targetVersionId.value),
)

function versionLabel(kind: string | undefined): string {
  switch (kind) {
    case 'original':
      return '原文'
    case 'chinese':
      return '中文译本'
    case 'simple':
      return '通俗简化'
    case 'bilingual':
      return '双语对照'
    default:
      return kind ?? '—'
  }
}

function ownerHint(error: unknown): string {
  const status = (error as { status?: number })?.status
  if (status === 403) {
    return 'public_demo 只读面无法写入（服务端 403）：请在「设置」页填入 OWNER_TOKEN 后重试。'
  }
  if (status === 409) return '该批注已被其他地方修改，请重新打开后再改。'
  return error instanceof Error ? error.message : String(error)
}

/** 气泡定位：优先贴在被选中的文字/高亮下方，并夹在视口内 */
function placeBubble(rect: DOMRect): void {
  const width = 320
  const left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12)
  const top = Math.min(rect.bottom + 8, window.innerHeight - 240)
  bubbleStyle.value = { left: `${left}px`, top: `${Math.max(12, top)}px`, width: `${width}px` }
}

// ---- 数据加载 ----
async function loadDocuments(): Promise<void> {
  try {
    const result = await listReaderDocuments(1, 50)
    documents.value = result.items ?? []
    documentsTotal.value = result.total ?? 0
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  }
}

async function loadReading(id: number): Promise<void> {
  loading.value = true
  notice.value = ''
  bubble.value = null
  try {
    const [doc, versionList, annotationPage] = await Promise.all([
      fetchReaderDocument(id).catch(() => null),
      listReaderVersions(id),
      listReaderAnnotations(id),
    ])
    detail.value =
      doc ??
      ({
        id,
        title: `文档 #${id}`,
        page_count: null,
        block_count: null,
      } as ReaderDocumentSummary)
    versions.value = versionList.items ?? []
    annotations.value = annotationPage.items ?? []

    const original = versions.value.find((item) => item.kind === 'original')
    sourceVersionId.value = original?.id ?? null
    // 默认右栏放「已登记的第一个译文版本」（没有就留空，由用户点登记）
    targetVersionId.value = translationVersions.value[0]?.id ?? null
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

async function loadAnnotations(id: number): Promise<void> {
  try {
    const page = await listReaderAnnotations(id)
    annotations.value = page.items ?? []
  } catch {
    /* 保留上一份 */
  }
}

async function registerDocument(): Promise<void> {
  if (!registerPaperId.value || busy.value) return
  busy.value = 'register'
  notice.value = ''
  try {
    const doc = await registerReaderDocument(registerPaperId.value)
    await loadDocuments()
    if (doc?.id) await router.push({ path: `/papers/reader/${doc.id}` })
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

/** 登记翻译版本（把翻译产物变成可阅读的 PDF） */
async function registerTranslation(kind: 'chinese' | 'simple' | 'bilingual'): Promise<void> {
  if (!documentId.value || busy.value) return
  busy.value = `version-${kind}`
  notice.value = ''
  try {
    const created = await registerReaderVersion(documentId.value, kind)
    await loadReading(documentId.value)
    if (created?.id) targetVersionId.value = created.id
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

// ---- 批注 ----
function onSelectText(payload: { side: 'source' | 'target'; page: number; text: string; rect: DOMRect }): void {
  // 只有原文栏的选区用于新建批注（译文栏的选区不是证据锚点）
  if (payload.side !== 'source' || !sourceVersionId.value) return
  draft.value = { page: payload.page, text: payload.text, kind: 'highlight', note: '' }
  activeAnnotation.value = null
  placeBubble(payload.rect)
  bubble.value = 'create'
}

function onClickAnnotation(payload: { annotation: ReaderAnnotation; rect: DOMRect }): void {
  activeAnnotation.value = payload.annotation
  editingNote.value = payload.annotation.note ?? ''
  placeBubble(payload.rect)
  bubble.value = 'view'
}

async function saveDraft(): Promise<void> {
  if (!documentId.value || !sourceVersionId.value || !draft.value.text.trim()) return
  busy.value = 'annotation'
  notice.value = ''
  try {
    await createReaderAnnotation(documentId.value, {
      version_id: sourceVersionId.value,
      quote_text: draft.value.text.trim(),
      note: draft.value.note.trim() || null,
      kind: draft.value.kind,
      page: draft.value.page,
    })
    bubble.value = null
    await loadAnnotations(documentId.value)
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

async function saveAnnotationNote(): Promise<void> {
  const target = activeAnnotation.value
  if (!documentId.value || !target) return
  try {
    await updateReaderAnnotation(documentId.value, target.id, {
      revision: target.revision ?? 1,
      note: editingNote.value,
    })
    bubble.value = null
    await loadAnnotations(documentId.value)
  } catch (error) {
    notice.value = ownerHint(error)
  }
}

async function removeAnnotation(): Promise<void> {
  const target = activeAnnotation.value
  if (!documentId.value || !target) return
  try {
    await deleteReaderAnnotation(documentId.value, target.id)
    bubble.value = null
    await loadAnnotations(documentId.value)
  } catch (error) {
    notice.value = ownerHint(error)
  }
}

// ---- 路由驱动 ----
watch(
  documentId,
  (id) => {
    if (id === null) {
      detail.value = null
      versions.value = []
      annotations.value = []
      void loadDocuments()
      return
    }
    void loadReading(id)
  },
  { immediate: true },
)

onUnmounted(() => {
  bubble.value = null
})
</script>

<template>
  <section class="reader">
    <!-- 列表态：挑文档 / 登记文档 -->
    <template v-if="!inReading">
      <header class="reader__head">
        <h1>全文阅读</h1>
        <span class="spacer" />
        <span class="chip">共 {{ documentsTotal }} 篇</span>
      </header>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <article class="panel">
        <div class="panel__head">
          <h2>登记阅读文档</h2>
        </div>
        <div class="creator">
          <input
            v-model.number="registerPaperId"
            class="field__input"
            type="number"
            min="1"
            placeholder="论文 ID"
          />
          <button
            class="btn btn--primary"
            type="button"
            :disabled="!registerPaperId || busy === 'register'"
            @click="registerDocument"
          >
            {{ busy === 'register' ? '解析中…' : '解析原文并登记' }}
          </button>
        </div>
      </article>

      <article class="panel">
        <table class="table">
          <thead>
            <tr>
              <th>标题</th>
              <th>页数</th>
              <th>解析状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="documents.length === 0">
              <td colspan="4" class="empty">还没有阅读文档：填论文 ID 登记一篇</td>
            </tr>
            <tr v-for="doc in documents" :key="doc.id" class="row" @click="router.push(`/papers/reader/${doc.id}`)">
              <td class="ellipsis">{{ doc.title ?? `文档 #${doc.id}` }}</td>
              <td class="mono">{{ doc.page_count ?? '—' }}</td>
              <td>
                <span class="tag" :class="doc.parse_status === 'ok' ? 'tag--ok' : 'tag--warn'">
                  {{ doc.parse_status === 'ok' ? '解析完成' : (doc.parse_status ?? '未知') }}
                </span>
              </td>
              <td>
                <button class="link-btn" type="button" @click.stop="router.push(`/papers/reader/${doc.id}`)">
                  打开阅读
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </article>
    </template>

    <!-- 阅读态：原文 PDF ｜ 译文 PDF -->
    <template v-else>
      <header class="reader__head">
        <button class="btn btn--ghost" type="button" @click="router.push('/papers/reader')">返回文档列表</button>
        <h1 class="reader__title">{{ detail?.title ?? `文档 #${documentId}` }}</h1>
        <span v-if="detail?.page_count" class="chip">{{ detail.page_count }} 页</span>
        <span class="spacer" />
        <span class="side-label">译文版本</span>
        <select v-model.number="targetVersionId" class="field__input field__input--sm">
          <option :value="null">未选择</option>
          <option v-for="item in translationVersions" :key="item.id" :value="item.id">
            {{ versionLabel(item.kind) }} · v{{ item.version_no }}
          </option>
        </select>
        <div class="register-group">
          <button
            v-for="kind in (['chinese', 'simple', 'bilingual'] as const)"
            :key="kind"
            class="link-btn"
            type="button"
            :disabled="busy === `version-${kind}`"
            @click="registerTranslation(kind)"
          >
            登记{{ versionLabel(kind) }}
          </button>
        </div>
      </header>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <div class="pdf-grid">
        <section class="pane">
          <header class="pane__head">
            <span class="pane__title">原文</span>
            <span class="chip">{{ versionLabel(sourceVersion?.kind) }}</span>
            <span class="spacer" />
            <a v-if="sourcePdfUrl" class="link-btn" :href="sourcePdfUrl" target="_blank" rel="noopener">打开原始 PDF</a>
          </header>
          <PdfPane
            :src="sourcePdfUrl"
            :annotations="sourceAnnotations"
            side="source"
            @select-text="onSelectText"
            @click-annotation="onClickAnnotation"
          />
        </section>

        <section class="pane">
          <header class="pane__head">
            <span class="pane__title">译文</span>
            <span v-if="targetVersion" class="chip">{{ versionLabel(targetVersion.kind) }}</span>
            <span class="spacer" />
            <a v-if="targetPdfUrl" class="link-btn" :href="targetPdfUrl" target="_blank" rel="noopener">
              打开原始 PDF
            </a>
          </header>
          <PdfPane
            v-if="targetPdfUrl"
            :src="targetPdfUrl"
            :annotations="targetAnnotations"
            side="target"
            @click-annotation="onClickAnnotation"
          />
          <p v-else class="pane__empty">该文档还没有译文版本，请在右上角登记一个</p>
        </section>
      </div>
    </template>

    <!-- 批注气泡：选区新建 / 点击高亮查看编辑 -->
    <Teleport to="body">
      <div v-if="bubble" class="bubble" :style="bubbleStyle" @mouseup.stop>
        <template v-if="bubble === 'create'">
          <p class="bubble__quote">P{{ draft.page }} · {{ draft.text.slice(0, 120) }}</p>
          <div class="bubble__row">
            <select v-model="draft.kind" class="field__input field__input--sm">
              <option value="highlight">高亮</option>
              <option value="note">笔记</option>
              <option value="question">疑问</option>
            </select>
            <input v-model="draft.note" class="field__input" type="text" placeholder="写点备注（可选）" />
          </div>
          <div class="bubble__foot">
            <button class="btn" type="button" @click="bubble = null">取消</button>
            <button class="btn btn--primary" type="button" :disabled="busy === 'annotation'" @click="saveDraft">
              {{ busy === 'annotation' ? '保存中…' : '保存批注' }}
            </button>
          </div>
        </template>

        <template v-else-if="activeAnnotation">
          <p class="bubble__quote">P{{ activeAnnotation.page ?? '—' }} · {{ (activeAnnotation.quote_text ?? '').slice(0, 120) }}</p>
          <input v-model="editingNote" class="field__input" type="text" placeholder="备注" />
          <div class="bubble__foot">
            <button class="link-btn link-btn--mute" type="button" @click="removeAnnotation">删除</button>
            <span class="spacer" />
            <button class="btn" type="button" @click="bubble = null">关闭</button>
            <button class="btn btn--primary" type="button" @click="saveAnnotationNote">保存</button>
          </div>
        </template>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.reader {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  height: 100%;
  min-height: 0;
}
.reader__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.reader__head h1 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.reader__title {
  max-width: 520px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.spacer {
  flex: 1;
}
.side-label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.register-group {
  display: flex;
  gap: var(--space-2);
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
.panel__head h2 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.creator {
  display: flex;
  gap: var(--space-2);
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
.field__input--sm {
  height: 30px;
  font-size: var(--font-size-xs);
}
.field__input:focus {
  outline: none;
  border-color: var(--color-brand);
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
.row {
  cursor: pointer;
  transition: background-color 160ms;
}
.row:hover {
  background: var(--color-bg-subtle);
}
.ellipsis {
  max-width: 620px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty {
  color: var(--color-text-secondary);
  text-align: center;
  font-size: var(--font-size-sm);
}

/* 双栏：原文 ｜ 译文，各自滚动，填满可用高度 */
.pdf-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}
.pane {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.pane__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-card-bg);
}
.pane__title {
  font-size: var(--font-size-sm);
  font-weight: 600;
}
.pane__empty {
  margin: 0;
  padding: var(--space-6) var(--space-4);
  text-align: center;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
@media (max-width: 1100px) {
  .pdf-grid {
    grid-template-columns: 1fr;
  }
}

/* 批注气泡（Teleport 到 body，需自带令牌作用域：全局 tokens 已在 :root） */
.bubble {
  position: fixed;
  z-index: 2500;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
  animation: bubble-in 160ms cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes bubble-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
.bubble__quote {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.bubble__row {
  display: flex;
  gap: var(--space-2);
}
.bubble__row .field__input:not(.field__input--sm) {
  flex: 1;
  min-width: 0;
}
.bubble__foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
</style>
