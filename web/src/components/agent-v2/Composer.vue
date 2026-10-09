<script setup lang="ts">
/** 输入框：进入新一轮对话；处于等待输入状态时改为「继续」。 */
import { computed, ref } from 'vue'

const props = defineProps<{
  disabled?: boolean
  busy?: boolean
  mode?: 'new' | 'steer' | 'continue'
  placeholder?: string
}>()

const emit = defineEmits<{ (event: 'submit', text: string): void }>()

const text = ref('')
const canSubmit = computed(() => text.value.trim().length > 0 && !props.disabled && !props.busy)

const label = computed(() => {
  if (props.mode === 'steer') return '追加'
  if (props.mode === 'continue') return '继续'
  return '发送'
})

function submit(): void {
  if (!canSubmit.value) return
  emit('submit', text.value.trim())
  text.value = ''
}
</script>

<template>
  <form class="composer" @submit.prevent="submit">
    <textarea
      v-model="text"
      class="composer__input"
      rows="2"
      :disabled="disabled"
      :placeholder="placeholder ?? '描述要完成的任务…'"
      @keydown.enter.exact.prevent="submit"
    />
    <div class="composer__foot">
      <span class="composer__hint" title="Enter 发送，Shift+Enter 换行">Enter 发送</span>
      <button type="submit" class="composer__send" :disabled="!canSubmit">{{ label }}</button>
    </div>
  </form>
</template>

<style scoped>
.composer {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: var(--color-bg-elevated);
  box-shadow: var(--shadow-card);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.composer:focus-within {
  border-color: var(--color-brand);
}

.composer__input {
  border: 0;
  outline: none;
  resize: vertical;
  background: none;
  color: var(--color-text-primary);
  font-family: var(--font-family-base);
  font-size: var(--font-size-md);
  line-height: var(--line-height-base);
  min-height: 48px;
}

.composer__foot {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.composer__hint {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.composer__send {
  margin-left: auto;
  border: 1px solid var(--color-brand);
  border-radius: var(--radius-md);
  background: var(--color-brand);
  color: var(--color-text-inverse);
  font-size: var(--font-size-sm);
  padding: var(--space-1) var(--space-4);
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.composer__send:hover:not(:disabled) {
  background: var(--color-brand-hover);
}

.composer__send:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.composer__send:disabled {
  background: var(--color-bg-muted);
  border-color: var(--color-border);
  color: var(--color-text-disabled);
  cursor: not-allowed;
}
</style>
