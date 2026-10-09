<script setup lang="ts">
/** 线程列表：切换当前会话、新建会话、归档会话。 */
import { computed, ref } from 'vue'

import type { ThreadView } from '../../agent-v2/protocol'

const props = defineProps<{
  threads: ThreadView[]
  currentThreadId: string | null
  busy?: boolean
  scenarios?: string[]
}>()

const emit = defineEmits<{
  (event: 'select', threadId: string): void
  (event: 'create', payload: { name: string; scenario?: string }): void
  (event: 'archive', threadId: string): void
}>()

const name = ref('')
const scenario = ref('')

const sorted = computed(() =>
  [...props.threads].sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? '')),
)

function create(): void {
  const value = name.value.trim()
  if (!value) return
  emit('create', { name: value, scenario: scenario.value || undefined })
  name.value = ''
}
</script>

<template>
  <aside class="rail">
    <header class="rail__head">
      <span class="rail__title">会话</span>
      <span class="rail__count">{{ threads.length }}</span>
    </header>

    <form class="rail__create" @submit.prevent="create">
      <input v-model="name" class="rail__input" type="text" placeholder="新建会话名称" />
      <select v-if="scenarios && scenarios.length" v-model="scenario" class="rail__select">
        <option value="">默认场景</option>
        <option v-for="item in scenarios" :key="item" :value="item">{{ item }}</option>
      </select>
      <button type="submit" class="rail__add" :disabled="busy || !name.trim()">新建</button>
    </form>

    <ul class="rail__list">
      <li
        v-for="thread in sorted"
        :key="thread.thread_id"
        class="row"
        :class="{ 'row--active': thread.thread_id === currentThreadId, 'row--archived': thread.status === 'archived' }"
      >
        <button type="button" class="row__main" @click="emit('select', thread.thread_id)">
          <span class="row__name">{{ thread.name }}</span>
          <span class="row__meta" :title="`最后事件序号 ${thread.last_sequence}`">
            {{ thread.status === 'archived' ? '已归档' : `序号 ${thread.last_sequence}` }}
          </span>
        </button>
        <button
          type="button"
          class="row__action"
          title="归档该会话（事件流与审计记录保留）"
          :disabled="busy || thread.status === 'archived'"
          @click="emit('archive', thread.thread_id)"
        >
          归档
        </button>
      </li>
      <li v-if="!sorted.length" class="rail__empty">还没有会话</li>
    </ul>
  </aside>
</template>

<style scoped>
.rail {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  min-width: 0;
}

.rail__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.rail__title {
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.rail__count {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.rail__create {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.rail__input,
.rail__select {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font-size: var(--font-size-xs);
  padding: var(--space-1) var(--space-2);
}

.rail__input:focus-visible,
.rail__select:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}

.rail__add {
  border: 1px solid var(--color-brand);
  border-radius: var(--radius-sm);
  background: var(--color-brand);
  color: var(--color-text-inverse);
  font-size: var(--font-size-xs);
  padding: var(--space-1) var(--space-2);
  cursor: pointer;
}

.rail__add:disabled {
  background: var(--color-bg-muted);
  border-color: var(--color-border);
  color: var(--color-text-disabled);
  cursor: not-allowed;
}

.rail__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 320px;
  overflow: auto;
}

.row {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  border-radius: var(--radius-sm);
}

.row:hover {
  background: var(--color-bg-muted);
}

.row--active {
  background: var(--color-brand-soft);
}

.row--archived .row__name {
  color: var(--color-text-disabled);
}

.row__main {
  flex: 1;
  min-width: 0;
  border: 0;
  background: none;
  text-align: left;
  padding: var(--space-1) var(--space-2);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.row__main:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: -2px;
}

.row__name {
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.row__meta {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.row__action {
  border: 0;
  background: none;
  color: var(--color-text-secondary);
  font-size: var(--font-size-2xs);
  padding: var(--space-1);
  cursor: pointer;
  opacity: 0;
}

.row:hover .row__action,
.row__action:focus-visible {
  opacity: 1;
}

.row__action:hover:not(:disabled) {
  color: var(--color-danger);
}

.row__action:disabled {
  cursor: default;
  color: var(--color-text-disabled);
}

.rail__empty {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
</style>
