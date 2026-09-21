<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 项目重命名弹窗（**总览壳层专用样式**，用 `.sl-home` 的 --h-* 令牌）。
 *
 * 真实写操作：`PATCH /projects/{id}`（Owner）。403 时如实说明需要 OWNER_TOKEN，
 * 不做本地假装成功。模块壳层（ShellLayout）里另有一份浅色学术蓝版本，两者互不影响。
 */
import { ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'

import { renameProject } from '@/api/projects'
import { useSessionStore } from '@/stores/session'

const props = defineProps<{
  modelValue: boolean
  projectId: number | null
  projectName: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'renamed'): void
}>()

const session = useSessionStore()
const name = ref('')
const busy = ref(false)
const notice = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    name.value = props.projectName
    notice.value = ''
    busy.value = false
  },
)

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  const id = props.projectId
  if (id === null || busy.value) return
  const next = name.value.trim()
  if (!next) {
    notice.value = '项目名不能为空'
    return
  }
  busy.value = true
  notice.value = ''
  try {
    await renameProject(id, next)
    await session.loadProjects()
    emit('renamed')
    close()
  } catch (error) {
    const status = (error as { status?: number } | undefined)?.status
    notice.value =
      status === 403
        ? writeDenied('重命名项目')
        : error instanceof Error
          ? error.message
          : String(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="overlay sl-home" @click.self="close">
      <div class="dialog" role="dialog" aria-modal="true" aria-label="重命名项目">
        <h2 class="dialog__title">重命名项目</h2>
        <label class="field">
          <span>项目名</span>
          <input v-model="name" type="text" maxlength="200" @keyup.enter="submit" />
        </label>
        <p v-if="notice" class="notice">{{ notice }}</p>
        <div class="foot">
          <button class="btn" type="button" @click="close">取消</button>
          <button class="btn btn--primary" type="button" :disabled="busy" @click="submit">
            {{ busy ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 居中弹框：四周半透明遮罩 + 背景模糊（与项目详情弹框同一口径） */
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
  width: min(440px, 100%);
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

.dialog__title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: var(--font-size-sm);
  color: var(--h-fg-subtle);
}

.field input {
  padding: 10px 12px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  border-radius: 12px;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  outline: none;
}

.field input:focus {
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

.foot {
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
