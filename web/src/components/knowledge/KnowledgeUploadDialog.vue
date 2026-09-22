<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 本地上传文件进知识库（拖拽区与论文导入弹窗同一套写法：原生 input + 拖拽，不引 el-upload）。
 *
 * ⚠️ 没有后端，所以**不会把文件传走**。但能做两件真事：
 * ① 文本类文件（md / 代码 / csv / txt）**真的读进内容**——上传后立刻能在阅读器里看；
 * ② 图片**读成 data URL**，立刻能预览。
 * 其余二进制格式（pdf / docx / xlsx）只登记元数据（名称 / 体积 / 类型），接后端后走真文件。
 */
import { computed, ref, watch } from 'vue'

import {
  BUCKET_LABELS,
  createEntry,
  formatOf,
  formatSize,
  hasContent,
  type KnowledgeBucket,
  type KnowledgeDraft,
  type KnowledgeEntry,
} from '@/api/knowledge'
import { useSessionStore } from '@/stores/session'

const props = defineProps<{
  modelValue: boolean
  tagPool: string[]
  folders: string[]
  projects: Array<{ id: number; name: string }>
  currentFolder: string[]
  canWrite: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'added', entries: KnowledgeEntry[]): void
}>()

const session = useSessionStore()

/** 单文件上限：留足余量，超限如实报出文件名而不是静默丢弃 */
const MAX_BYTES = 200 * 1024 * 1024

const BUCKET_OPTIONS: ReadonlyArray<{ value: KnowledgeBucket; label: string }> = (
  ['literature', 'idea', 'experiment', 'paper', 'memory'] as KnowledgeBucket[]
).map((value) => ({ value, label: BUCKET_LABELS[value] }))

interface Picked {
  file: File
  content: string
  dataUrl: string | null
}

const picked = ref<Picked[]>([])
const dragging = ref(false)
const rejected = ref<string[]>([])
const tags = ref<string[]>([])
const bucket = ref<KnowledgeBucket>('literature')
const folder = ref<string[]>([])
const projectId = ref<number | null>(null)
const saving = ref(false)
const errorNotice = ref('')

const totalSize = computed(() => picked.value.reduce((sum, item) => sum + item.file.size, 0))
const canSubmit = computed(() => picked.value.length > 0 && !saving.value && props.canWrite)

/** 只为"上传完立刻能看"服务：小文本读内容、小图片读 data URL；大文件一律只登记 */
async function readForPreview(file: File): Promise<Picked> {
  const renderer = formatOf(file.name, true).renderer
  const small = file.size <= 2 * 1024 * 1024
  if (small && ['markdown', 'code', 'text', 'sheet'].includes(renderer)) {
    try {
      return { file, content: await file.text(), dataUrl: null }
    } catch {
      return { file, content: '', dataUrl: null }
    }
  }
  if (small && renderer === 'image') {
    try {
      const buffer = await file.arrayBuffer()
      const base64 = btoa(String.fromCharCode(...new Uint8Array(buffer)))
      return { file, content: '', dataUrl: `data:${file.type || 'image/png'};base64,${base64}` }
    } catch {
      return { file, content: '', dataUrl: null }
    }
  }
  return { file, content: '', dataUrl: null }
}

async function addFiles(list: FileList | null): Promise<void> {
  if (!list) return
  const tooBig: string[] = []
  const next: Picked[] = []
  for (const file of Array.from(list)) {
    if (file.size > MAX_BYTES) {
      tooBig.push(file.name)
      continue
    }
    if (picked.value.some((item) => item.file.name === file.name && item.file.size === file.size)) continue
    next.push(await readForPreview(file))
  }
  picked.value = [...picked.value, ...next]
  rejected.value = tooBig
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  void addFiles(event.dataTransfer?.files ?? null)
}

function removeFile(index: number): void {
  picked.value.splice(index, 1)
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  saving.value = true
  errorNotice.value = ''
  const created: KnowledgeEntry[] = []
  try {
    for (const item of picked.value) {
      const draft: KnowledgeDraft = {
        name: item.file.name,
        bucket: bucket.value,
        content: item.content,
        size: item.content ? item.content.length : item.file.size,
        folder: [...folder.value],
        tags: tags.value,
        project_id: projectId.value,
        source_label: '本地上传',
        source_route: null,
        file_url: item.dataUrl,
      }
      created.push(await createEntry(draft))
    }
    emit('added', created)
    close()
  } catch (error) {
    console.error('[knowledge] 上传失败', error)
    errorNotice.value = '加入知识库失败，请重试'
  } finally {
    saving.value = false
  }
}

function close(): void {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    picked.value = []
    rejected.value = []
    tags.value = []
    bucket.value = 'literature'
    folder.value = [...props.currentFolder]
    projectId.value = session.currentProjectId
    dragging.value = false
    saving.value = false
    errorNotice.value = ''
  },
)
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="kbu-overlay" @click.self="close">
      <section class="kbu" role="dialog" aria-modal="true" aria-label="上传文件到知识库">
        <header class="kbu__head">
          <h2 class="kbu__title">上传文件</h2>
          <button class="kbu__close" type="button" aria-label="关闭" @click="close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
        </header>

        <div class="kbu__body scroll-y">
          <label
            class="kbu__drop"
            :class="{ 'kbu__drop--on': dragging }"
            @dragover.prevent="dragging = true"
            @dragleave.prevent="dragging = false"
            @drop.prevent="onDrop"
          >
            <input class="kbu__drop-input" type="file" multiple @change="addFiles(($event.target as HTMLInputElement).files)" />
            <span class="kbu__drop-mark">任意格式</span>
            <span class="kbu__drop-text">拖拽文件到这里，或点击选择</span>
          </label>

          <p v-if="rejected.length" class="kbu__error">超过 200 MB，未加入：{{ rejected.join('、') }}</p>

          <div v-if="picked.length" class="kbu__summary">
            <span>{{ picked.length }} 个文件</span>
            <span>共 {{ formatSize(totalSize) }}</span>
          </div>

          <ul v-if="picked.length" class="kbu__list scroll-y">
            <li v-for="(item, index) in picked" :key="`${item.file.name}-${index}`" class="kbu__item">
              <span class="kbu__item-name">{{ item.file.name }}</span>
              <span class="kbu__item-tag">{{ formatOf(item.file.name, hasContent({ content: item.content } as KnowledgeEntry)).label }}</span>
              <span class="kbu__item-size">{{ formatSize(item.file.size) }}</span>
              <button class="kbu__item-remove" type="button" title="移除" @click="removeFile(index)">移除</button>
            </li>
          </ul>

          <div class="kbu__form">
            <div class="kbu__form-row">
              <span class="kbu__label">分类</span>
              <el-select v-model="bucket" size="small" class="kbu__control">
                <el-option v-for="option in BUCKET_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
              </el-select>
            </div>
            <div class="kbu__form-row">
              <span class="kbu__label">文件夹</span>
              <el-select v-model="folder" size="small" class="kbu__control" clearable placeholder="知识库（根目录）">
                <el-option label="知识库（根目录）" :value="[]" />
                <el-option v-for="path in folders" :key="path" :label="path" :value="path.split('/')" />
              </el-select>
            </div>
            <div class="kbu__form-row">
              <span class="kbu__label">标签</span>
              <el-select
                v-model="tags"
                size="small"
                class="kbu__control"
                multiple
                filterable
                allow-create
                default-first-option
                placeholder="回车确认，可直接新建"
              >
                <el-option v-for="tag in tagPool" :key="tag" :label="tag" :value="tag" />
              </el-select>
            </div>
            <div class="kbu__form-row">
              <span class="kbu__label">归属项目</span>
              <el-select v-model="projectId" size="small" class="kbu__control" clearable placeholder="不归属任何项目">
                <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
              </el-select>
            </div>
          </div>

          <p v-if="errorNotice" class="kbu__error">{{ errorNotice }}</p>
        </div>

        <footer class="kbu__foot">
          <button class="kbu__btn" type="button" @click="close">取消</button>
          <button class="kbu__btn kbu__btn--primary" type="button" :disabled="!canSubmit" @click="submit">
            {{ saving ? '加入中…' : `加入知识库${picked.length ? `（${picked.length}）` : ''}` }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.kbu-overlay {
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

:global(:root[data-theme='dark']) .kbu-overlay {
  background: rgba(0, 0, 0, 0.55); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.kbu {
  display: flex;
  flex-direction: column;
  width: min(620px, 100%);
  max-height: min(760px, 100%);
  overflow: hidden;
  background: var(--color-card-bg);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.kbu__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.kbu__title {
  margin: 0;
  flex: 1;
  font-size: var(--font-size-lg);
}

.kbu__close {
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

.kbu__close:hover {
  border-color: var(--color-border);
  color: var(--color-text-primary);
}

.kbu__close:active {
  transform: scale(var(--motion-press));
}

.kbu__close:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbu__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.kbu__drop {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  min-height: 168px;
  padding: var(--space-5) var(--space-4);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: var(--color-bg-subtle);
  cursor: pointer;
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    background-color var(--motion-dur-fast) var(--motion-ease),
    transform var(--motion-dur-fast) var(--motion-ease-out);
}

.kbu__drop:hover,
.kbu__drop--on {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
  transform: translateY(-1px);
}

.kbu__drop:focus-within {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbu__drop-input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.kbu__drop-mark {
  padding: 2px 10px;
  border-radius: var(--radius-pill);
  background: var(--color-brand);
  color: var(--color-text-inverse);
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.kbu__drop-text {
  font-size: var(--font-size-md);
}

.kbu__error {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  background-color: var(--color-danger-soft);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}

.kbu__summary {
  display: flex;
  gap: var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbu__list {
  margin: 0;
  padding: 0;
  list-style: none;
  max-height: 160px;
  overflow-y: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.kbu__item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
}

.kbu__item:last-child {
  border-bottom: none;
}

.kbu__item-name {
  flex: 1;
  min-width: 0;
  font-size: var(--font-size-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kbu__item-tag {
  flex: none;
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbu__item-size {
  flex: none;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-family: var(--font-family-mono);
}

.kbu__item-remove {
  flex: none;
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-xs);
  cursor: pointer;
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease);
}

.kbu__item-remove:hover {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.kbu__item-remove:active {
  transform: scale(var(--motion-press));
}

.kbu__item-remove:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbu__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.kbu__form-row {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  align-items: center;
  gap: var(--space-2);
}

.kbu__label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbu__control {
  min-width: 0;
}

.kbu__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

.kbu__btn {
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

.kbu__btn:hover {
  border-color: var(--color-border-strong);
  color: var(--color-text-primary);
}

.kbu__btn:active {
  transform: scale(var(--motion-press));
}

.kbu__btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbu__btn--primary {
  border-color: var(--color-brand);
  background-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.kbu__btn--primary:hover {
  border-color: var(--color-brand-hover);
  background-color: var(--color-brand-hover);
  color: var(--color-text-inverse);
}

.kbu__btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (prefers-reduced-motion: reduce) {
  .kbu,
  .kbu__close,
  .kbu__drop,
  .kbu__item-remove,
  .kbu__btn {
    transition: none;
    animation: none;
  }
}
</style>
