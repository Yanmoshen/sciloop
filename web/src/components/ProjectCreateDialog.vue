<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 新建研究项目弹窗（**总览壳层样式**：居中 + 背景模糊，用 `.sl-home` 的 --h-* 令牌）。
 *
 * 字段：项目名（必填）/ 备注 / 研究方向（多选，可自定义并保存、可删除）。
 * 研究方向交互：默认平铺前 9 个，**第 10 个起收进下拉框**；行尾「＋」进入编辑模式，
 * 在编辑模式里新增或删除方向，编辑结果持久化在浏览器本地。
 *
 * 口径说明：自定义研究方向目前**存在浏览器 localStorage**（跨设备/清缓存会丢）；
 * 后端暂无「方向字典」接口，若需要入库需另加接口与迁移。
 */
import { computed, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'

import { createProject, isOwnerRequired, RESEARCH_FIELDS, type CreatedProject } from '@/api/projects'

const props = defineProps<{ modelValue: boolean; prefill?: string }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', project: CreatedProject): void
}>()

/** 内置研究方向（单一来源：api/projects.ts 的 RESEARCH_FIELDS） */
const BUILTIN_FIELDS: FieldOption[] = RESEARCH_FIELDS

interface FieldOption {
  value: string
  label: string
}

/** 内置研究方向由 api/projects.ts 提供（前 9 个平铺、其余进下拉） */
const VISIBLE_FIELDS = 9
const CUSTOM_FIELD_KEY = 'sciloop.custom_research_fields'

const name = ref('')
const note = ref('')
const selected = ref<string[]>(['cs.CL'])
const notice = ref('')
const busy = ref(false)

const editing = ref(false)
const newFieldName = ref('')

const customFields = ref<FieldOption[]>(readCustomFields())

function readCustomFields(): FieldOption[] {
  try {
    const raw = localStorage.getItem(CUSTOM_FIELD_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as FieldOption[]
    return Array.isArray(parsed)
      ? parsed.filter((item) => item && typeof item.value === 'string' && item.value.trim())
      : []
  } catch {
    return []
  }
}

function persistCustomFields(): void {
  try {
    localStorage.setItem(CUSTOM_FIELD_KEY, JSON.stringify(customFields.value))
  } catch {
    /* 存储不可用时忽略：仅影响自定义方向的持久化 */
  }
}

const allFields = computed<FieldOption[]>(() => [...BUILTIN_FIELDS, ...customFields.value])
/** 平铺展示的前 9 个 */
const headFields = computed(() => allFields.value.slice(0, VISIBLE_FIELDS))
/** 第 10 个起：下拉框选择 */
const tailFields = computed(() => allFields.value.slice(VISIBLE_FIELDS))
/** 已选但不在平铺区的方向（下拉或自定义来的），要单独显示出来 */
const extraSelected = computed(() =>
  selected.value
    .filter((value) => !headFields.value.some((item) => item.value === value))
    .map((value) => allFields.value.find((item) => item.value === value) ?? { value, label: value }),
)

const canSubmit = computed(() => name.value.trim().length > 0 && selected.value.length > 0)

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    notice.value = ''
    busy.value = false
    editing.value = false
    newFieldName.value = ''
    const seed = (props.prefill ?? '').trim()
    if (seed) {
      note.value = seed
      if (!name.value) name.value = seed.length > 40 ? `${seed.slice(0, 38)}…` : seed
    }
  },
)

function close(): void {
  emit('update:modelValue', false)
}

function toggleField(value: string): void {
  selected.value = selected.value.includes(value)
    ? selected.value.filter((item) => item !== value)
    : [...selected.value, value]
}

function pickFromSelect(event: Event): void {
  const el = event.target as HTMLSelectElement
  const value = el.value
  el.value = ''
  if (!value) return
  if (!selected.value.includes(value)) selected.value = [...selected.value, value]
}

function saveNewField(): void {
  const raw = newFieldName.value.trim()
  if (!raw) {
    notice.value = '研究方向名称不能为空'
    return
  }
  if (allFields.value.some((item) => item.value === raw || item.label === raw)) {
    notice.value = '该研究方向已存在'
    return
  }
  customFields.value = [...customFields.value, { value: raw, label: raw }]
  persistCustomFields()
  selected.value = [...selected.value, raw]
  newFieldName.value = ''
  notice.value = ''
}

function removeField(value: string): void {
  customFields.value = customFields.value.filter((item) => item.value !== value)
  persistCustomFields()
  selected.value = selected.value.filter((item) => item !== value)
}

async function submit(): Promise<void> {
  if (!canSubmit.value || busy.value) return
  busy.value = true
  notice.value = ''
  try {
    const project = await createProject({
      name: name.value,
      note: note.value,
      fields: selected.value,
    })
    emit('created', project)
    close()
  } catch (error) {
    notice.value = isOwnerRequired(error)
      ? writeDenied('创建项目')
      : error instanceof Error
        ? error.message
        : String(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="modelValue" class="overlay" @click.self="close">
    <div class="dialog sl-home scroll-y" role="dialog" aria-modal="true" aria-label="新建研究项目">
      <header class="dialog__head">
        <h2 class="dialog__title">新建研究项目</h2>
        <button class="close" type="button" @click="close">关闭</button>
      </header>

      <label class="field">
        <span class="field__label">项目名</span>
        <input v-model="name" type="text" placeholder="例如：长上下文问答评测方案" />
      </label>

      <label class="field">
        <span class="field__label">备注</span>
        <textarea v-model="note" rows="3" placeholder="例如：本周先验证 tokenizer 公平性，样本 20–50 条" />
      </label>

      <div class="field">
        <div class="field__row">
          <span class="field__label">研究方向（可多选）</span>
          <button class="link-btn" type="button" @click="editing = !editing">
            {{ editing ? '完成编辑' : '编辑方向' }}
          </button>
        </div>

        <!-- 前 9 个平铺 + 已选中的额外方向 -->
        <div class="chips">
          <button
            v-for="item in headFields"
            :key="item.value"
            type="button"
            class="chip"
            :class="{ 'chip--on': selected.includes(item.value) }"
            @click="toggleField(item.value)"
          >
            {{ item.label }}
          </button>
          <button
            v-for="item in extraSelected"
            :key="`extra-${item.value}`"
            type="button"
            class="chip chip--on"
            :title="`移除 ${item.label}`"
            @click="toggleField(item.value)"
          >
            {{ item.label }} ×
          </button>
        </div>

        <!-- 第 10 个起：下拉框选择；行尾「＋」进入编辑模式 -->
        <div class="picker">
          <select class="select" :disabled="tailFields.length === 0" @change="pickFromSelect">
            <option value="">更多方向</option>
            <option v-for="item in tailFields" :key="item.value" :value="item.value">
              {{ item.label }}
            </option>
          </select>
          <button
            class="icon-btn"
            type="button"
            :class="{ 'icon-btn--on': editing }"
            title="编辑研究方向"
            aria-label="编辑研究方向"
            @click="editing = !editing"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
        </div>

        <!-- 编辑模式：新增 / 删除自定义方向 -->
        <div v-if="editing" class="editor">
          <div v-if="customFields.length > 0" class="editor__list">
            <span v-for="item in customFields" :key="item.value" class="editor__item">
              {{ item.label }}
              <button type="button" :aria-label="`删除 ${item.label}`" @click="removeField(item.value)">×</button>
            </span>
          </div>
          <div class="editor__row">
            <input
              v-model="newFieldName"
              type="text"
              maxlength="40"
              placeholder="输入新的研究方向名称"
              @keyup.enter="saveNewField"
            />
            <button class="btn" type="button" @click="saveNewField">保存方向</button>
          </div>
        </div>
      </div>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <footer class="dialog__foot">
        <button class="btn" type="button" @click="close">取消</button>
        <button class="btn btn--primary" type="button" :disabled="!canSubmit || busy" @click="submit">
          {{ busy ? '创建中…' : '创建项目' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 2200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.35); /* ui-polish-allow: 遮罩色与主题解耦 */
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .overlay {
  background: rgba(0, 0, 0, 0.55); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.dialog {
  width: min(600px, 100%);
  max-height: 86vh;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  background: var(--h-surface-raised);
  color: var(--h-fg);
  border: 1px solid var(--h-line-strong);
  border-radius: 20px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35); /* ui-polish-allow: 弹窗投影色 */
  font-size: var(--font-size-md);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.dialog__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.dialog__title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.close {
  border: 0;
  background: transparent;
  color: var(--h-fg-subtle);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
}

.close:hover {
  color: var(--h-fg);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.field__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.field__label {
  font-size: var(--font-size-sm);
  color: var(--h-fg-subtle);
}

.field input,
.field textarea,
.select {
  width: 100%;
  padding: 10px 12px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  border-radius: 12px;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  outline: none;
}

.field textarea {
  resize: none;
}

.field input:focus,
.field textarea:focus,
.select:focus {
  border-color: var(--h-primary);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.chip {
  padding: 6px 12px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.chip--on {
  background: var(--h-active);
  border-color: var(--h-primary);
  color: var(--h-fg);
}

.picker {
  display: flex;
  align-items: center;
  gap: 8px;
}

.link-btn {
  border: 0;
  background: transparent;
  color: var(--h-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    filter 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.link-btn:hover {
  filter: brightness(1.2);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.link-btn:active {
  filter: brightness(0.95);
}
/* 方向 chip：悬停给描边 + 淡底，不用位移（避免整排跳动） */
.chip:hover {
  border-color: var(--h-primary);
  color: var(--h-fg);
  background: var(--h-hover);
}

.icon-btn {
  width: 32px;
  height: 32px;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: transparent;
  color: var(--h-fg-muted);
  cursor: pointer;
}

.icon-btn:hover,
.icon-btn--on {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

.editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border: 1px solid var(--h-line);
  border-radius: 12px;
  background: var(--h-surface-input);
}

.editor__list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.editor__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.editor__item button {
  border: 0;
  background: transparent;
  color: var(--h-fg-subtle);
  font: inherit;
  font-size: var(--font-size-md);
  line-height: 1;
  cursor: pointer;
}

.editor__item button:hover {
  color: var(--h-primary);
}

.editor__row {
  display: flex;
  gap: 8px;
}

.editor__row input {
  flex: 1;
  min-width: 0;
  padding: 8px 12px;
  background: var(--h-surface-raised);
  border: 1px solid var(--h-line);
  border-radius: 10px;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  outline: none;
}

.editor__row input:focus {
  border-color: var(--h-primary);
}

.notice {
  margin: 0;
  padding: 10px 12px;
  border-left: 3px solid var(--h-primary);
  border-radius: 8px;
  background: var(--h-hover);
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.dialog__foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn {
  height: 34px;
  padding: 0 16px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
  white-space: nowrap;
}

.btn:hover {
  border-color: var(--h-line-strong);
  color: var(--h-fg);
}

.btn--primary {
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
  font-weight: 600;
}

.btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
