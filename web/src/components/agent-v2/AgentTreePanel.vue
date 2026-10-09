<script setup lang="ts">
/**
 * 子 Agent 树：创建 / 运行 / 等待 / 失败 / 中断 / 完成的完整状态与结果来源。
 *
 * 结果一律显示「来自哪个子线程」，避免把子 Agent 的产物误当成父 Agent 的输出。
 */
import { computed } from 'vue'

import type { ChildView } from '../../agent-v2/protocol'

const props = defineProps<{
  parentThreadId: string
  children: ChildView[]
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'refresh'): void
  (event: 'wait', childId: string): void
  (event: 'waitAll'): void
  (event: 'interrupt', childId: string): void
}>()

const STATUS_LABEL: Record<string, string> = {
  created: '已创建',
  idle: '空闲',
  running: '运行中',
  waiting: '等待中',
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断',
  timeout: '等待超时',
}

function tone(status: string): string {
  if (status === 'completed') return 'ok'
  if (status === 'failed') return 'danger'
  if (status === 'interrupted' || status === 'timeout') return 'warn'
  if (status === 'running' || status === 'waiting') return 'busy'
  return 'idle'
}

const anyRunning = computed(() =>
  props.children.some((child) => ['running', 'waiting', 'created', 'idle'].includes(child.status)),
)

function depth(child: ChildView): number {
  return Math.max(0, (child.path?.length ?? 1) - 2)
}
</script>

<template>
  <section class="tree">
    <header class="tree__head">
      <span class="tree__title">子 Agent</span>
      <span class="tree__count" :title="`父线程 ${parentThreadId}`">{{ children.length }} 个</span>
      <button type="button" class="link" @click="emit('refresh')">刷新</button>
      <button
        type="button"
        class="link"
        :disabled="busy || !anyRunning"
        @click="emit('waitAll')"
      >
        等待全部收敛
      </button>
    </header>

    <ul v-if="children.length" class="tree__list">
      <li
        v-for="child in children"
        :key="child.thread_id"
        class="node"
        :class="`node--${tone(child.status)}`"
        :style="{ marginLeft: `${depth(child) * 12}px` }"
      >
        <div class="node__head">
          <span class="node__name">{{ child.name }}</span>
          <span class="tag" :class="`tag--${tone(child.status)}`">
            {{ STATUS_LABEL[child.status] ?? child.status }}
          </span>
          <span class="node__id mono" :title="child.thread_id">{{ child.thread_id.slice(0, 10) }}</span>
        </div>
        <p v-if="child.summary" class="node__summary">
          <span class="node__from">来自 {{ child.name }}</span>
          {{ child.summary }}
        </p>
        <p v-if="child.error" class="node__error">
          {{ child.error.code }}：{{ child.error.message }}
        </p>
        <div class="node__actions">
          <button
            type="button"
            class="link"
            :disabled="busy || !['running', 'waiting', 'created', 'idle'].includes(child.status)"
            @click="emit('wait', child.thread_id)"
          >
            等待此子 Agent
          </button>
          <button
            type="button"
            class="link link--danger"
            :disabled="busy || !['running', 'waiting', 'created', 'idle'].includes(child.status)"
            @click="emit('interrupt', child.thread_id)"
          >
            中断
          </button>
        </div>
      </li>
    </ul>
    <p v-else class="tree__empty">本会话还没有派生子 Agent</p>
  </section>
</template>

<style scoped>
.tree {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.tree__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.tree__title {
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.tree__count {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
  margin-left: auto;
}

.tree__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.node {
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.node--ok {
  border-left-color: var(--color-success);
}

.node--danger {
  border-left-color: var(--color-danger);
}

.node--warn {
  border-left-color: var(--color-warning);
}

.node--busy {
  border-left-color: var(--color-brand);
}

.node__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.node__name {
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.node__id {
  margin-left: auto;
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.node__summary {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.node__from {
  color: var(--color-text-secondary);
  margin-right: var(--space-1);
}

.node__error {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-danger);
}

.node__actions {
  display: flex;
  gap: var(--space-3);
}

.tag {
  font-size: var(--font-size-2xs);
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
}

.tag--ok {
  color: var(--color-success);
  background: var(--color-success-soft);
}

.tag--danger {
  color: var(--color-danger);
  background: var(--color-danger-soft);
}

.tag--warn {
  color: var(--color-warning);
  background: var(--color-warning-soft);
}

.tag--busy {
  color: var(--color-brand);
  background: var(--color-brand-soft);
}

.link {
  border: 0;
  padding: 0;
  background: none;
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}

.link:hover,
.link:focus-visible {
  text-decoration: underline;
}

.link--danger {
  color: var(--color-danger);
}

.link:disabled {
  color: var(--color-text-disabled);
  cursor: default;
  text-decoration: none;
}

.tree__empty {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.mono {
  font-family: var(--font-family-mono);
}
</style>
