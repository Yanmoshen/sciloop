<script setup lang="ts">
/**
 * 连接状态条（WP-05：只显示「是否已恢复」和「是否需要刷新」）。
 *
 * **不显示**内部细节：游标序号、重连次数、事件补齐条数、错误码、服务端地址。
 * 用户需要知道的只有三件事：现在能不能用、断线时在自动恢复、什么时候需要手动刷新。
 */
import { computed } from 'vue'

import type { ConnectionState, Notice } from '../../agent-v2/reducer'

const props = defineProps<{
  connection: ConnectionState
  /** 是否已用本地快照先渲染（此时提示「正在补齐最新进度」）。 */
  snapshotRestored?: boolean
  /** 事件缺口尚未补平。 */
  gapPending?: boolean
  notices: Notice[]
}>()

const emit = defineEmits<{ (event: 'reconnect'): void; (event: 'refresh'): void }>()

const label = computed(() => {
  if (props.connection.shuttingDown) return '服务正在重启'
  switch (props.connection.status) {
    case 'connected':
      return '已连接'
    case 'connecting':
      return '正在连接'
    case 'reconnecting':
      return '正在自动重连'
    case 'closed':
      return '已断开'
    default:
      return '未连接'
  }
})

const healthy = computed(() => props.connection.status === 'connected' && !props.connection.shuttingDown)

/** 一句话告诉用户该怎么办；没有需要处理的事情时不显示。 */
const hint = computed(() => {
  if (props.connection.shuttingDown) return '连接会自行恢复，稍等片刻'
  if (props.connection.status === 'reconnecting') return '正在自动恢复，已发出的内容不会重复提交'
  if (props.connection.status === 'closed') return '请重新连接'
  if (props.gapPending) return '正在补齐最新进度'
  if (props.snapshotRestored) return '已显示本地记录，正在核对最新进度'
  return ''
})

/** 只保留最近一条「需要注意」的提示，避免堆成一堵墙。 */
const worstNotice = computed(
  () => [...props.notices].reverse().find((notice) => notice.level !== 'info') ?? null,
)
</script>

<template>
  <div class="banner" :class="{ 'banner--warn': !healthy }">
    <span class="banner__state">
      <i class="dot" />
      {{ label }}
    </span>
    <span v-if="hint" class="banner__hint">{{ hint }}</span>
    <span v-if="worstNotice" class="banner__notice">{{ worstNotice.text }}</span>
    <span class="banner__actions">
      <button v-if="!healthy" type="button" class="link" @click="emit('reconnect')">重新连接</button>
      <button v-else type="button" class="link" @click="emit('refresh')">刷新最新进度</button>
    </span>
  </div>
</template>

<style scoped>
.banner {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
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

.banner__hint {
  white-space: nowrap;
}

.banner__notice {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
</style>
