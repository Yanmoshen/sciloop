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
 * 字段：项目名（必填）/ 工作目录（**按钮选择**）/ 备注。
 * 左栏「项目」的 ＋ 与首页「新建研究项目」卡片**共用这一个弹窗**（2026-09-26 统一）。
 *
 * 工作目录为什么走执行环境：网页拿不到本地路径、也调不起系统弹框（浏览器安全边界）；
 * 点按钮 → 后端 → 执行环境弹出**系统自带**的文件夹选择框 → 真实绝对路径回填。
 *
 */
import { computed, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'

import { createProject, isOwnerRequired, pickFolder, type CreatedProject } from '@/api/projects'

const props = defineProps<{ modelValue: boolean; prefill?: string }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', project: CreatedProject): void
}>()

const name = ref('')
const note = ref('')
const notice = ref('')
const busy = ref(false)

type PickResult = { ok: boolean; available: boolean; canceled: boolean; path: string; message: string }

/** 研究者用系统选择框选的绝对路径（选完就定，不提供重选） */
const workspaceDir = ref('')

/** 选文件夹：这个执行环境弹不弹得出系统框 / 正在等选 / 弹不出时给人看的说明 */
const pickAvailable = ref(true)
const pickBusy = ref(false)
const pickMessage = ref('')

const canSubmit = computed(() => name.value.trim().length > 0)

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    notice.value = ''
    busy.value = false
    workspaceDir.value = ''
    pickBusy.value = false
    pickMessage.value = ''
    pickAvailable.value = true
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

/**
 * 点「选择文件夹…」：请**执行环境**在研究者屏幕上弹出系统自带的文件夹选择框。
 *
 * 为什么不自己做界面：网页拿不到本地路径、也调不起系统弹框（浏览器安全边界）；
 * 而"资源管理器那种选择"正是研究者要的（2026-09-26）。容器执行环境没有屏幕 → 由它如实回"弹不出来"。
 */
async function chooseFolder(): Promise<void> {
  if (pickBusy.value || !pickAvailable.value) return
  pickBusy.value = true
  notice.value = ''
  pickMessage.value = ''
  try {
    const result = (await pickFolder()) as PickResult
    if (!result.ok && !result.available) {
      pickAvailable.value = false
      pickMessage.value = result.message || '当前执行环境弹不出系统选择框'
      return
    }
    if (result.ok && result.path) workspaceDir.value = result.path
    else if (result.ok) notice.value = result.message || '你在系统窗口里取消了选择（保持默认）'
    else notice.value = result.message || '没能打开系统选择框'
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  } finally {
    pickBusy.value = false
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
        <span class="field__label">工作目录</span>
        <div class="pickrow">
          <button
            class="btn"
            type="button"
            :disabled="pickBusy || !pickAvailable"
            :title="pickAvailable ? '' : pickMessage"
            @click="chooseFolder"
          >
            {{ pickBusy ? '等待你在系统窗口里选择…' : '选择文件夹…' }}
          </button>
          <span v-if="workspaceDir" class="pickrow__path">{{ workspaceDir }}</span>
        </div>
        <p v-if="workspaceDir" class="field__hint">已选择：项目就在这个目录里干活</p>
        <p v-else-if="pickMessage && !pickAvailable" class="field__hint">{{ pickMessage }}</p>
        <p v-else class="field__hint">
          未选择：将在 SciLoop 目录下的 research-workspaces/&lt;项目名&gt; 建一个
        </p>
      </div>

      <label class="field">
        <span class="field__label">备注</span>
        <textarea v-model="note" rows="3" placeholder="例如：本周先验证 tokenizer 公平性，样本 20–50 条" />
      </label>

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
.field textarea,
.field textarea {
  resize: none;
}

.field input:focus,
.field textarea:focus,
/* 方向 chip：悬停给描边 + 淡底，不用位移（避免整排跳动） */
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
