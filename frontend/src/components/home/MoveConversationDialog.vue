<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 把一条对话「移入项目」（左栏对话行悬停出的动作）。
 *
 * 目标列表 = 全部未归档项目 + 「未分组」；当前所在位置标出且不可重复选。
 * 真实写操作：`PATCH /conversations/{id}`（Owner）。403 如实说明，不做本地假装成功。
 */
import { computed, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'

import { moveConversation } from '@/api/conversations'
import type { ProjectBrief } from '@/stores/session'

const props = defineProps<{
  modelValue: boolean
  conversationId: string | null
  currentProjectId: number | null
  projects: ProjectBrief[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'moved'): void
}>()

/** `null` = 未分组；数字 = 目标项目 id */
const target = ref<number | null>(null)
const busy = ref(false)
const notice = ref('')

/** 候选 = 未分组 + 全部未归档项目 */
const options = computed<Array<{ value: number | null; label: string }>>(() => [
  { value: null, label: '未分组' },
  ...props.projects.map((project) => ({ value: project.id, label: project.name })),
])

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    target.value = props.currentProjectId
    notice.value = ''
    busy.value = false
  },
)

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  const id = props.conversationId
  if (!id || busy.value) return
  busy.value = true
  notice.value = ''
  try {
    await moveConversation(id, target.value)
    emit('moved')
    close()
  } catch (error) {
    const status = (error as { status?: number } | undefined)?.status
    notice.value =
      status === 403
        ? writeDenied('移动对话')
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
      <div class="dialog" role="dialog" aria-modal="true" aria-label="移入项目">
        <h2 class="dialog__title">移入项目</h2>
        <ul class="options">
          <li v-for="option in options" :key="String(option.value)">
            <button
              class="option"
              type="button"
              :class="{ 'option--on': option.value === target }"
              @click="target = option.value"
            >
              <span>{{ option.label }}</span>
              <svg v-if="option.value === target" width="13" height="13" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path
                  d="M2.5 6.5 5 9l4.5-6"
                  stroke="currentColor"
                  stroke-width="1.7"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </button>
          </li>
        </ul>
        <p v-if="notice" class="notice">{{ notice }}</p>
        <div class="foot">
          <button class="btn" type="button" @click="close">取消</button>
          <button
            class="btn btn--primary"
            type="button"
            :disabled="busy || target === currentProjectId"
            @click="submit"
          >
            {{ busy ? '移动中…' : '移动' }}
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
  animation: pop 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes pop {
  from {
    opacity: 0;
    transform: translateY(6px) scale(0.985);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.dialog__title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.options {
  max-height: 320px;
  overflow-y: auto;
  margin: 0;
  padding: 4px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border: 1px solid var(--h-line);
  border-radius: 12px;
}

.option {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  text-align: left;
  cursor: pointer;
}

.option:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

.option--on {
  background: var(--h-active);
  color: var(--h-primary);
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
