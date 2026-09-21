<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 新建项目弹窗：左栏「项目」右侧的 ＋ 用这个——**只手填项目名**。
 *
 * 与 ProjectCreateDialog 的分工：那个是首页「新建研究项目」卡片的完整表单（备注 / 研究方向），
 * 这个是左栏里的轻量入口，只收一个名字（口径：项目 = 对话的容器，细节留到工作台里补）。
 * 真实写操作：`POST /projects`（Owner）。403 如实说明需要 OWNER_TOKEN，不做本地假装成功。
 */
import { ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'

import { createProject, type CreatedProject } from '@/api/projects'

const props = defineProps<{ modelValue: boolean }>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', project: CreatedProject): void
}>()

const name = ref('')
const busy = ref(false)
const notice = ref('')

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    name.value = ''
    notice.value = ''
    busy.value = false
  },
)

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  if (busy.value) return
  const next = name.value.trim()
  if (!next) {
    notice.value = '请输入项目名'
    return
  }
  busy.value = true
  notice.value = ''
  try {
    const project = await createProject({ name: next, note: '', fields: [] })
    emit('created', project)
    close()
  } catch (error) {
    const status = (error as { status?: number } | undefined)?.status
    notice.value =
      status === 403
        ? writeDenied('新建项目')
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
      <div class="dialog" role="dialog" aria-modal="true" aria-label="新建项目">
        <h2 class="dialog__title">新建项目</h2>
        <label class="field">
          <span>项目名</span>
          <input
            v-model="name"
            type="text"
            maxlength="200"
            placeholder="例如：长上下文问答评测"
            @keyup.enter="submit"
          />
        </label>
        <p v-if="notice" class="notice">{{ notice }}</p>
        <div class="foot">
          <button class="btn" type="button" @click="close">取消</button>
          <button class="btn btn--primary" type="button" :disabled="busy" @click="submit">
            {{ busy ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
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
