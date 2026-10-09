<script setup lang="ts">
/**
 * Turn 控件：继续、追加、中断、重试、恢复。
 *
 * 按钮按当前 Turn 状态启用/禁用，并且**只有真正可用的动作才出现**——
 * 不给用户一个点了没反应的按钮。
 */
import { computed } from 'vue'

import type { TurnView } from '../../agent-v2/protocol'

const props = defineProps<{
  turn: TurnView | null
  busy?: boolean
  draft?: string
}>()

const emit = defineEmits<{
  (event: 'steer', text: string): void
  (event: 'continue', text: string): void
  (event: 'interrupt'): void
  (event: 'retry'): void
  (event: 'recover'): void
}>()

const status = computed(() => props.turn?.status ?? 'idle')
const canSteer = computed(() => status.value === 'running')
const canContinue = computed(() => status.value === 'waiting_input')
const canInterrupt = computed(() =>
  ['running', 'waiting_approval', 'waiting_input', 'queued'].includes(status.value),
)
const canRecover = computed(() => status.value === 'interrupted')
const canRetry = computed(() => status.value === 'failed')

const STATUS_LABEL: Record<string, string> = {
  idle: '空闲',
  queued: '排队中',
  running: '执行中',
  waiting_approval: '等待审批',
  waiting_input: '等待输入',
  interrupted: '已中断',
  failed: '失败',
  completed: '已完成',
}

function submit(mode: 'steer' | 'continue'): void {
  const text = (props.draft ?? '').trim()
  if (!text) return
  if (mode === 'steer') emit('steer', text)
  else emit('continue', text)
}
</script>

<template>
  <div class="controls">
    <span class="controls__status" :class="`s-${status}`">{{ STATUS_LABEL[status] ?? status }}</span>

    <div class="controls__buttons">
      <button v-if="canSteer" type="button" class="btn" :disabled="busy" @click="submit('steer')">
        追加输入
      </button>
      <button v-if="canContinue" type="button" class="btn btn--primary" :disabled="busy" @click="submit('continue')">
        继续
      </button>
      <button v-if="canInterrupt" type="button" class="btn btn--danger" :disabled="busy" @click="emit('interrupt')">
        中断
      </button>
      <button v-if="canRecover" type="button" class="btn" :disabled="busy" @click="emit('recover')">
        恢复
      </button>
      <button v-if="canRetry" type="button" class="btn" :disabled="busy" @click="emit('retry')">
        重试
      </button>
    </div>
  </div>
</template>

<style scoped>
.controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}

.controls__status {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.s-running {
  color: var(--color-brand);
}

.s-waiting_approval,
.s-waiting_input {
  color: var(--color-warning);
}

.s-failed {
  color: var(--color-danger);
}

.s-completed {
  color: var(--color-success);
}

.controls__buttons {
  display: flex;
  gap: var(--space-2);
  margin-left: auto;
}

.btn {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  font-size: var(--font-size-xs);
  padding: 2px var(--space-3);
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.btn:hover:not(:disabled) {
  background: var(--color-bg-muted);
}

.btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}

.btn:disabled {
  color: var(--color-text-disabled);
  cursor: progress;
}

.btn--primary {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.btn--danger {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

</style>
