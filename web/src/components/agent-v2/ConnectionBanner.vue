<script setup lang="ts">
/**
 * 连接状态条：连接状态、重连次数、事件补齐进度与提示。
 *
 * 只显示事实与可执行动作；口径说明走 `title`，不写解释性小字。
 */
import { computed } from 'vue'

import type { ConnectionState, Notice } from '../../agent-v2/reducer'

const props = defineProps<{
  connection: ConnectionState
  cursor: number
  gapFrom: number | null
  backfilled: number
  notices: Notice[]
}>()

const emit = defineEmits<{ (event: 'reconnect'): void; (event: 'backfill'): void }>()

const label = computed(() => {
  switch (props.connection.status) {
    case 'open':
      return '已连接'
    case 'connecting':
      return '正在连接'
    case 'reconnecting':
      return '正在重连'
    case 'closed':
      return '已断开'
    default:
      return '未连接'
  }
})

const tone = computed(() => {
  if (props.connection.shuttingDown) return 'warn'
  return props.connection.status === 'open' ? 'ok' : 'warn'
})

const recentNotices = computed(() => props.notices.slice(-3).reverse())

const gapText = computed(() => {
  if (props.gapFrom === null) return ''
  return `事件缺口：等待序号 ${props.gapFrom}`
})
</script>

<template>
  <div class="banner" :class="`banner--${tone}`">
    <span class="banner__state">
      <i class="dot" />
      {{ label }}
    </span>
    <span class="banner__meta" :title="`已应用事件的最后序号（游标）为 ${cursor}`">游标 {{ cursor }}</span>
    <span v-if="connection.attempts" class="banner__meta">重连 {{ connection.attempts }} 次</span>
    <span v-if="backfilled" class="banner__meta" :title="`最近一次重连通过游标补齐了 ${backfilled} 条事件`">
      已补齐 {{ backfilled }} 条事件
    </span>
    <span v-if="gapText" class="banner__gap">{{ gapText }}</span>
    <span v-if="connection.message" class="banner__message">{{ connection.message }}</span>
    <span class="banner__actions">
      <button v-if="gapFrom !== null" type="button" class="link" @click="emit('backfill')">补齐事件</button>
      <button
        v-if="connection.status !== 'open'"
        type="button"
        class="link"
        @click="emit('reconnect')"
      >
        重新连接
      </button>
    </span>
    <ul v-if="recentNotices.length" class="banner__notices">
      <li v-for="(notice, index) in recentNotices" :key="index" :class="`level-${notice.level}`">
        {{ notice.text }}
      </li>
    </ul>
  </div>
</template>

<style scoped>
.banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.banner--ok {
  border-color: var(--color-border);
}

.banner--warn {
  border-color: var(--color-border-strong);
}

.banner__state {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: var(--radius-pill);
  background: var(--color-success);
}

.banner--warn .dot {
  background: var(--color-danger);
}

.banner__meta {
  white-space: nowrap;
}

.banner__gap {
  color: var(--color-danger);
}

.banner__message {
  color: var(--color-danger);
}

.banner__actions {
  display: inline-flex;
  gap: var(--space-3);
  margin-left: auto;
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
  color: var(--color-brand-hover);
  text-decoration: underline;
}

.banner__notices {
  flex-basis: 100%;
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.level-error {
  color: var(--color-danger);
}

.level-warning {
  color: var(--color-warning);
}
</style>
