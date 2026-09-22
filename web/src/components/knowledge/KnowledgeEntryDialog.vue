<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 知识库条目的新建 / 编辑弹窗。条目就是"一个带扩展名的文件"，所以这里改的是：
 * 文件名（含扩展名）、分类、所在文件夹、标签、归属项目、以及**文本类文件的内容**。
 *
 * 二进制文件（pdf / docx / 图片等）不提供内容编辑——它们的字节来自上传，
 * 这里只登记元数据（如实说明，不假装能在线改内容）。
 */
import { computed, ref, watch } from 'vue'

import {
  BUCKET_LABELS,
  createEntry,
  emptyDraft,
  formatOf,
  folderPath,
  hasContent,
  updateEntry,
  type KnowledgeBucket,
  type KnowledgeDraft,
  type KnowledgeEntry,
} from '@/api/knowledge'

const props = defineProps<{
  modelValue: boolean
  entry: KnowledgeEntry | null
  defaultBucket: KnowledgeBucket
  tagPool: string[]
  folders: string[]
  projects: Array<{ id: number; name: string }>
  currentFolder: string[]
  canWrite: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'saved', entry: KnowledgeEntry): void
}>()

const BUCKET_OPTIONS: ReadonlyArray<{ value: KnowledgeBucket; label: string }> = (
  ['literature', 'idea', 'experiment', 'paper', 'memory'] as KnowledgeBucket[]
).map((value) => ({ value, label: BUCKET_LABELS[value] }))

const draft = ref<KnowledgeDraft>(emptyDraft(props.defaultBucket, props.currentFolder))
const busy = ref(false)
const errorNotice = ref('')

const isCreate = computed(() => props.entry === null)
const canSubmit = computed(() => draft.value.name.trim().length > 0 && !busy.value && props.canWrite)

/** 文本类文件才给内容编辑框（md / 代码 / 纯文本 / csv 都是文本） */
const canEditContent = computed(() =>
  ['markdown', 'code', 'text', 'sheet'].includes(formatOf(draft.value.name, true).renderer),
)

const fileName = computed(() => (draft.value.name.trim() ? draft.value.name.trim() : ''))

function hydrate(): void {
  if (props.entry) {
    const { id: _id, imported_at: _imported, modified_at: _modified, moved_at: _moved, trashed_at: _trashed, ...rest } =
      props.entry
    draft.value = { ...rest, folder: [...rest.folder], tags: [...rest.tags] }
  } else {
    draft.value = emptyDraft(props.defaultBucket, props.currentFolder)
  }
  busy.value = false
  errorNotice.value = ''
}

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  busy.value = true
  errorNotice.value = ''
  const payload: KnowledgeDraft = {
    ...draft.value,
    name: draft.value.name.trim(),
    tags: draft.value.tags.map((tag) => tag.trim()).filter(Boolean),
    size: canEditContent.value ? draft.value.content.length : draft.value.size,
  }
  try {
    const saved = props.entry ? await updateEntry(props.entry.id, payload) : await createEntry(payload)
    if (!saved) {
      errorNotice.value = '该条目已不在知识库里（可能已被删除）'
      return
    }
    emit('saved', saved)
    close()
  } catch (error) {
    console.error('[knowledge] 保存失败', error)
    errorNotice.value = '保存失败，请重试'
  } finally {
    busy.value = false
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) hydrate()
  },
)
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="kbd-overlay" @click.self="close">
      <section class="kbd" role="dialog" aria-modal="true" aria-label="知识库条目">
        <header class="kbd__head">
          <h2 class="kbd__title">{{ isCreate ? '新建条目' : '编辑条目' }}</h2>
          <button class="kbd__close" type="button" aria-label="关闭" @click="close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
        </header>

        <div class="kbd__body scroll-y">
          <label class="kbd__row">
            <span class="kbd__label">文件名</span>
            <el-input v-model="draft.name" size="small" class="kbd__control" placeholder="含扩展名，如 方法草稿.md" />
          </label>

          <label class="kbd__row">
            <span class="kbd__label">分类</span>
            <el-select v-model="draft.bucket" size="small" class="kbd__control">
              <el-option v-for="option in BUCKET_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
            </el-select>
          </label>

          <label class="kbd__row">
            <span class="kbd__label">所在文件夹</span>
            <el-select v-model="draft.folder" size="small" class="kbd__control" clearable placeholder="知识库（根目录）">
              <el-option label="知识库（根目录）" :value="[]" />
              <el-option v-for="path in folders" :key="path" :label="path" :value="path.split('/')" />
            </el-select>
          </label>

          <label class="kbd__row">
            <span class="kbd__label">标签</span>
            <el-select
              v-model="draft.tags"
              size="small"
              class="kbd__control"
              multiple
              filterable
              allow-create
              default-first-option
              placeholder="回车确认，可直接新建"
            >
              <el-option v-for="tag in tagPool" :key="tag" :label="tag" :value="tag" />
            </el-select>
          </label>

          <label class="kbd__row">
            <span class="kbd__label">归属项目</span>
            <el-select v-model="draft.project_id" size="small" class="kbd__control" clearable placeholder="不归属任何项目">
              <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
            </el-select>
          </label>

          <div v-if="canEditContent" class="kbd__block">
            <span class="kbd__label">内容</span>
            <el-input
              v-model="draft.content"
              type="textarea"
              :autosize="{ minRows: 8, maxRows: 22 }"
              :placeholder="fileName ? '支持 Markdown（.md）' : '先填文件名'"
            />
          </div>
          <p v-else class="kbd__binary">
            {{ formatOf(draft.name, hasContent({ content: draft.content } as KnowledgeEntry)).label }} 的字节来自上传，
            这里只登记信息，内容请用本机软件改后重新上传。
          </p>

          <p v-if="draft.source_label" class="kbd__source">
            来源 {{ draft.source_label }}
            <template v-if="draft.folder.length"> · 位置 知识库 / {{ folderPath(draft.folder) }}</template>
          </p>

          <p v-if="errorNotice" class="kbd__error">{{ errorNotice }}</p>
        </div>

        <footer class="kbd__foot">
          <button class="kbd__btn" type="button" @click="close">取消</button>
          <button class="kbd__btn kbd__btn--primary" type="button" :disabled="!canSubmit" @click="submit">
            {{ busy ? '保存中…' : '保存' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.kbd-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-5);
  background: rgba(0, 0, 0, 0.35); /* ui-polish-allow: 遮罩色与主题解耦 */
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .kbd-overlay {
  background: rgba(0, 0, 0, 0.55); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.kbd {
  display: flex;
  flex-direction: column;
  width: min(640px, 100%);
  max-height: min(780px, 100%);
  overflow: hidden;
  background: var(--color-card-bg);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.kbd__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.kbd__title {
  margin: 0;
  flex: 1;
  font-size: var(--font-size-lg);
}

.kbd__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition:
    color var(--motion-dur-fast) var(--motion-ease),
    border-color var(--motion-dur-fast) var(--motion-ease);
}

.kbd__close:hover {
  border-color: var(--color-border);
  color: var(--color-text-primary);
}

.kbd__close:active {
  transform: scale(var(--motion-press));
}

.kbd__close:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbd__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.kbd__row {
  display: grid;
  grid-template-columns: 84px minmax(0, 1fr);
  align-items: center;
  gap: var(--space-3);
}

.kbd__block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.kbd__label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbd__control {
  min-width: 0;
}

.kbd__binary,
.kbd__source {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  line-height: var(--line-height-base);
}

.kbd__error {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  background-color: var(--color-danger-soft);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}

.kbd__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

.kbd__btn {
  height: 30px;
  padding: 0 var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease);
}

.kbd__btn:hover {
  border-color: var(--color-border-strong);
  color: var(--color-text-primary);
}

.kbd__btn:active {
  transform: scale(var(--motion-press));
}

.kbd__btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbd__btn--primary {
  border-color: var(--color-brand);
  background-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.kbd__btn--primary:hover {
  border-color: var(--color-brand-hover);
  background-color: var(--color-brand-hover);
  color: var(--color-text-inverse);
}

.kbd__btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (prefers-reduced-motion: reduce) {
  .kbd,
  .kbd__close,
  .kbd__btn {
    transition: none;
    animation: none;
  }
}
</style>
