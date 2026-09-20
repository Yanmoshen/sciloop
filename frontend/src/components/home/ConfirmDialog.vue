<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 通用确认弹窗（左栏「归档」这类需要一次确认的动作）。
 *
 * 口径：**一次确认**即可，不做二次输入；主按钮是品牌色，取消在左。
 * 承载它的宿主负责真正的写操作与失败提示（403 等如实回传，不假装成功）。
 */
defineProps<{
  modelValue: boolean
  title: string
  message: string
  confirmText?: string
  busy?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'confirm'): void
}>()

function close(): void {
  emit('update:modelValue', false)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="overlay sl-home" @click.self="close">
      <div class="dialog" role="dialog" aria-modal="true" :aria-label="title">
        <h2 class="dialog__title">{{ title }}</h2>
        <p class="dialog__message">{{ message }}</p>
        <div class="foot">
          <button class="btn" type="button" @click="close">取消</button>
          <button class="btn btn--primary" type="button" :disabled="busy" @click="emit('confirm')">
            {{ busy ? '处理中…' : (confirmText ?? '确认') }}
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
  width: min(420px, 100%);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
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

.dialog__message {
  margin: 0;
  color: var(--h-fg-muted);
  line-height: 1.6;
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
