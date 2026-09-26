<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全文阅读（核心模块 ③）：**原文 PDF 与中文译文 PDF 并排，原汁原味地读**。
 *
 * 界面口径（2026-09-26 研究者逐条确认）
 * --------------------------------------
 * - 只保留两个动作：**「翻译该论文」**（跳「论文翻译」页去跑，把这篇带过去）与
 *   **「双语对照模式」**（显示开关）；
 * - 「双语对照模式」**关** = 整屏一栏原文 PDF；**开** = 左英文原文 / 右中文译文，
 *   两栏**各自独立滚动**（刻意不做滚动同步对齐）；
 * - **不再有**「译文版本」下拉，也没有「登记中文译本 / 通俗简化 / 双语对照」按钮 ——
 *   一个文档只可能有原文与中文译本两类版本（译本类型已在 2026-09-26 收敛）；
 * - 进阅读页的唯一入口是**论文库点标题**（左栏导航的「全文阅读」入口与列表页已下线）。
 *
 * 译文怎么进阅读器（关键）
 * ------------------------
 * 登记按钮下掉后，译文靠 `autoAttachTranslation()` **自动接入**：打开阅读页时若该文档
 * 还没有中文译本，就去翻译任务列表里找**这篇论文最新一份已完成**的产物并登记。
 * 没有产物 / 只读模式 / 产物缺失 → 静默保持"还没有中文译本"，右栏照常给出下一步提示。
 * 这样「只保留两个按钮」才不会漏掉"登记"这一步。
 *
 * 批注（保留）
 * ------------
 * 选区新建 / 点击高亮查看编辑，批注**直接高亮在 PDF 页面上**（按后端给的 page + bbox 叠矩形）。
 * 只有原文栏的选区用于新建批注（译文栏的选区不是证据锚点）。
 * 「阅读就是阅读」：不放结构大纲、已理解标记、字号、逐块对照文本等辅助面板。
 *
 * 渲染由 `components/PdfPane.vue` 用 PDF.js 完成；批注读写走既有 `/reader/...` 接口。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'
import { useRoute, useRouter } from 'vue-router'

import {
  createReaderAnnotation,
  deleteReaderAnnotation,
  fetchReaderDocument,
  listReaderAnnotations,
  listReaderVersions,
  readerVersionPdfUrl,
  registerReaderVersion,
  updateReaderAnnotation,
  type ReaderAnnotation,
  type ReaderDocumentSummary,
  type ReaderVersion,
} from '@/api/reader'
import { listTranslateJobs } from '@/api/translate'
import PdfPane from '@/components/PdfPane.vue'

const route = useRoute()
const router = useRouter()

// ---- 阅读态 ----
const detail = ref<ReaderDocumentSummary | null>(null)
const versions = ref<ReaderVersion[]>([])
const sourceVersionId = ref<number | null>(null)
const targetVersionId = ref<number | null>(null)
const annotations = ref<ReaderAnnotation[]>([])

// 「双语对照模式」原先在这里是个显示开关（整屏原文 ↔ 左原文右译文）。
// 2026-09-26 用户口径：双语阅读要**全屏、只剩左右两栏** —— 那必须脱开应用外壳，
// 所以它改成了独立页面 `/papers/dual/:documentId`（见 `goDual`），本页只留原文一栏。

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

/** 中文译本：同一 kind 可保留历史（迁移 0004），取 version_no 最大的那份当"当前译文"。 */
const translationVersion = computed(() => {
  const items = versions.value.filter((item) => item.kind !== 'original')
  return items.length
    ? items.slice().sort((left, right) => right.version_no - left.version_no)[0]
    : null
})

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

/** 该论文在库内的 id —— 「翻译该论文」要把它带给翻译页。 */
const paperId = computed(() => detail.value?.paper_id ?? null)

function ownerHint(error: unknown): string {
  const status = (error as { status?: number })?.status
  if (status === 403) {
    return writeDenied('保存批注')
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
/**
 * 自动接入中文译本：该文档还没有译本时，取**这篇论文最新一份已完成**的翻译产物登记。
 *
 * 幂等：已有译本直接返回（不重复登记，也不会因重复而 409）；
 * 失败一律**静默**——没翻译过 / 只读模式 / 产物已被清掉都属正常情况，
 * 右栏会自己显示「还没有中文译本」并给出下一步，不在这里报错吓人。
 */
async function autoAttachTranslation(id: number): Promise<void> {
  const targetPaperId = paperId.value
  if (!targetPaperId) return
  try {
    const page = await listTranslateJobs(50)
    const latest = (page.items ?? [])
      .filter((job) => job.paper_id === targetPaperId && job.status === 'completed' && job.task_id)
      .sort((left, right) =>
        String(right.created_at ?? '').localeCompare(String(left.created_at ?? '')),
      )[0]
    if (!latest?.task_id) return

    // ⚠️ 幂等的判据是「**这一版还没登记过**」，**不是**「有没有译本」（2026-09-26 修）。
    //
    // 原来写的是 `if (translationVersion.value) return` —— 只要已经有过一版（哪怕是最早
    // 本地桩跑出来的那一版），就**永远不再接入新译文**。于是用户看到的现象是：
    // **翻译明明完成了，阅读页还停在旧译文上**。
    const newest = String(latest.task_id)
    const registered = new Set(
      versions.value.map((item) => String(item.task_id ?? '')).filter(Boolean),
    )
    if (registered.has(newest)) return

    const created = await registerReaderVersion(id, 'chinese', newest)
    if (created?.id) {
      versions.value = [...versions.value, created]
      // 右栏立刻切到刚接入的这一版（`translationVersion` 取 version_no 最大者）
      targetVersionId.value = created.id
    }
  } catch {
    /* 没翻译过 / 无权限 / 产物缺失 —— 保持"还没有中文译本"即可 */
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
    targetVersionId.value = translationVersion.value?.id ?? null

    await autoAttachTranslation(id)
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

/** 「翻译该论文」：带上这篇论文去翻译页（翻译页会**自动开跑**，不用再点一次）。 */
function goTranslate(): void {
  if (!paperId.value) return
  void router.push({ path: '/papers/translate', query: { paper_id: String(paperId.value) } })
}

/** 「双语对照模式」：打开**全屏双语页**（不带应用外壳的那一页）。 */
function goDual(): void {
  if (!documentId.value) return
  void router.push(`/papers/dual/${documentId.value}`)
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
    <header class="reader__head">
      <button class="btn btn--ghost" type="button" @click="router.push('/papers/feed')">
        返回论文库
      </button>
      <h1 class="reader__title">{{ detail?.title ?? `文档 #${documentId}` }}</h1>
      <span v-if="detail?.page_count" class="chip">{{ detail.page_count }} 页</span>
      <span class="spacer" />
      <button class="btn" type="button" :disabled="!paperId" @click="goTranslate">
        翻译该论文
      </button>
      <!-- 「双语对照模式」不再是本页的显示开关，而是**打开全屏双语页**
           （用户口径 2026-09-26：全屏只剩左原文/右译文，导航栏头像全去掉）。 -->
      <button class="btn" type="button" :disabled="!documentId" @click="goDual">
        双语对照模式
      </button>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <div class="pdf-grid">
      <section class="pane">
        <header class="pane__head">
          <span class="pane__title">原文</span>
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
    </div>

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

/* 单栏 = 整屏原文；开对照模式才分左右两栏（各自滚动，不做滚动同步） */
.pdf-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--space-3);
}
.pdf-grid--pair {
  grid-template-columns: 1fr 1fr;
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
  .pdf-grid--pair {
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
  animation: pop-in var(--motion-dur-fast) var(--motion-ease-out);
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
