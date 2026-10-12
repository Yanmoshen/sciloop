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
 * 字段：项目名（必填）/ 工作目录（原生目录选择器）/ 备注。
 * 左栏「项目」的 ＋ 与首页「新建研究项目」卡片**共用这一个弹窗**（2026-09-26 统一）。
 *
 * 工作目录选择打开本机原生目录对话框，只回传绝对路径，不读取或上传目录内文件。
 *
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'

import {
  cancelPickFolder,
  createProject,
  isOwnerRequired,
  type CreatedProject,
  pickFolder,
  pickFolderStatus,
} from '@/api/projects'

const props = defineProps<{ modelValue: boolean; prefill?: string }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', project: CreatedProject): void
}>()

const name = ref('')
const note = ref('')
const notice = ref('')
const busy = ref(false)

/** 手动填写或本机原生目录选择器返回的真实宿主路径 */
const workspaceDir = ref('')
const pickPending = ref(false)
const pickStarting = ref(false)
const pickRequestId = ref('')
let pickTimer: number | undefined
const canSubmit = computed(() => name.value.trim().length > 0)

function stopPoll(): void {
  if (pickTimer !== undefined) window.clearTimeout(pickTimer)
  pickTimer = undefined
}

function schedulePoll(): void {
  stopPoll()
  pickTimer = window.setTimeout(() => void pollFolder(), 350)
}

async function pollFolder(): Promise<void> {
  pickTimer = undefined
  if (!pickRequestId.value) return
  try {
    const state = await pickFolderStatus(pickRequestId.value)
    if (state.status === 'pending') {
      schedulePoll()
      return
    }
    pickRequestId.value = ''
    pickPending.value = false
    if (state.status === 'selected' && state.path) {
      workspaceDir.value = state.path
      notice.value = ''
    } else if (state.status !== 'canceled') {
      notice.value = state.message || '选择文件夹失败，请重试'
    }
  } catch (error) {
    pickRequestId.value = ''
    pickPending.value = false
    notice.value = error instanceof Error ? error.message : '选择文件夹失败，请重试'
  }
}

async function discardPick(): Promise<void> {
  stopPoll()
  const requestId = pickRequestId.value
  pickRequestId.value = ''
  pickPending.value = false
  if (requestId) await cancelPickFolder(requestId).catch(() => undefined)
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) {
      void discardPick()
      return
    }
    notice.value = ''
    name.value = ''
    note.value = ''
    busy.value = false
    workspaceDir.value = ''
    pickPending.value = false
    pickStarting.value = false
    pickRequestId.value = ''
    const seed = (props.prefill ?? '').trim()
    if (seed) {
      note.value = seed
      if (!name.value) name.value = seed.length > 40 ? `${seed.slice(0, 38)}…` : seed
    }
  },
)

onBeforeUnmount(() => {
  void discardPick()
})

function close(): void {
  emit('update:modelValue', false)
}

async function chooseFolder(): Promise<void> {
  if (pickPending.value || pickStarting.value) return
  pickStarting.value = true
  notice.value = ''
  try {
    const state = await pickFolder('选择项目工作目录')
    pickRequestId.value = state.request_id
    if (state.status === 'pending') {
      pickPending.value = true
      schedulePoll()
    } else if (state.status === 'selected' && state.path) {
      workspaceDir.value = state.path
    } else {
      notice.value = state.message || '选择文件夹失败，请重试'
    }
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '选择文件夹失败，请重试'
  } finally {
    pickStarting.value = false
  }
}

async function submit(): Promise<void> {
  if (!canSubmit.value || busy.value) return
  busy.value = true
  notice.value = ''
  try {
    const project = await createProject({
      name: name.value,
      note: note.value,
      workspace_dir: workspaceDir.value,
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

      <div class="field">
        <label class="field__label" for="project-workspace">工作目录</label>
        <input id="project-workspace" v-model="workspaceDir" type="text" placeholder="输入完整目录路径，或选择文件夹" />
        <div class="pickrow">
          <button
            class="btn"
            type="button"
            @click="chooseFolder"
          >
            {{ pickStarting ? '正在打开系统选择窗口…' : pickPending ? '等待选择文件夹…' : '选择文件夹…' }}
          </button>
          <span v-if="workspaceDir" class="pickrow__path">{{ workspaceDir }}</span>
        </div>
        <p v-if="workspaceDir" class="field__hint">已选择：创建项目时将使用这个真实目录路径。</p>
        <p v-else class="field__hint">
          留空时将自动创建项目文件夹；填写的新目录会在创建项目时建立。
        </p>
      </div>

      <label class="field">
        <span class="field__label">备注</span>
        <textarea v-model="note" rows="3" placeholder="例如：本周先验证 tokenizer 公平性，样本 20–50 条" />
      </label>

      <p v-if="notice" class="notice">{{ notice }}</p>

      <footer class="dialog__foot">
        <button class="btn" type="button" @click="close">取消</button>
        <button class="btn btn--primary" type="button" :disabled="!canSubmit || busy || pickPending || pickStarting" @click="submit">
          {{ busy ? '创建中…' : '创建项目' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
/* 选文件夹：按钮 + 已选路径 + 未选提示（2026-09-26） */
.pickrow {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.pickrow__path {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
  color: var(--h-fg-muted);
  overflow-wrap: anywhere;
}

.field__hint {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--h-fg-muted);
}

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

.field__label {
  font-size: var(--font-size-sm);
  color: var(--h-fg-subtle);
}

.field input,
.field textarea {
  /* 2026-09-26：这段规则体被上一轮"删死样式"误删过（只剩 resize）→ 输入框退回浏览器默认样式，
     与研究者的整体风格断层 ✗。这里按 app 的扁平口径补回来（跟聊天输入框同一套令牌）。 */
  width: 100%;
  box-sizing: border-box;
  padding: 9px 12px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: var(--h-surface);
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  line-height: 1.5;
  resize: vertical;
  transition: border-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.field input::placeholder,
.field textarea::placeholder {
  color: var(--h-fg-subtle);
}

.field input:hover,
.field textarea:hover {
  border-color: var(--h-line-strong);
}

.field input:focus,
.field textarea:focus {
  outline: none;
  border-color: var(--h-fg-muted);
}

.field input:focus-visible,
.field textarea:focus-visible {
  outline: 2px solid var(--h-fg-muted);
  outline-offset: 1px;
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

/* 2026-09-26：通用 `.btn:hover{color:var(--h-fg)}` 会盖掉主按钮的字色 ✗
   → 白底 + 主题浅字 = 悬停时看不清（研究者实报）。主按钮的 hover 固定用自己的字色。 */
.btn--primary:hover {
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
  opacity: 0.92;
}

.btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
