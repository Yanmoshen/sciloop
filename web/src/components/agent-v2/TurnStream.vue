<script setup lang="ts">
/**
 * 会话消息流（WP-05：`MessageItem` 的列表容器）。
 *
 * 严格按事件序号渲染，不做重排；正在流式中的文本挂在末尾，
 * 一旦落地成 Item 就换成权威内容（避免同一段话出现两遍）。
 * 审批卡通过插槽交给外层渲染，保持"事件流"与"需要决策的卡片"职责分离。
 */
import { computed } from 'vue'

import MessageItem from './MessageItem.vue'
import { landedAssistantText, toBlocks, type BlockLookups } from './blocks'
import type { ApprovalView, ItemView, ToolCallView } from '../../agent-v2/protocol'

const props = defineProps<{
  items: ItemView[]
  streamText: string
  streamReasoning: string
  toolCallOf: (callId: string | null) => ToolCallView | null
  toolOutputOf: (callId: string) => string
  approvalOf: (approvalId: string) => ApprovalView | null
  /** 本轮已停止（或正在停止）：不再显示三点/生成中。 */
  stopped?: boolean
}>()

const lookups: BlockLookups = {
  toolCallOf: (callId) => props.toolCallOf(callId),
  toolOutputOf: (callId) => props.toolOutputOf(callId),
  approvalOf: (approvalId) => props.approvalOf(approvalId),
}

const blocks = computed(() => toBlocks(props.items, lookups))
const hasReasoning = computed(() => blocks.value.some((block) => block.kind === 'reasoning'))

/** 只在「还没有落地成 Item」时展示流式文本；已停止则一律不展示。 */
const liveText = computed(() => {
  if (props.stopped) return ''
  const live = props.streamText
  if (!live.trim()) return ''
  return landedAssistantText(blocks.value).includes(live) ? '' : live
})

const liveReasoning = computed(() => (props.stopped ? '' : props.streamReasoning))
</script>

<template>
  <div class="stream">
    <MessageItem v-for="block in blocks" :key="block.key" :block="block">
      <template #approval="{ approval }">
        <slot name="approval" :approval="approval" />
      </template>
    </MessageItem>

    <details v-if="liveReasoning && !hasReasoning" class="reasoning">
      <summary>推理中</summary>
      <p class="reasoning__text">{{ liveReasoning }}</p>
    </details>

    <div v-if="liveText" class="bubble bubble--agent">
      <span class="bubble__role">助手</span>
      <p class="bubble__text">{{ liveText }}<i class="caret" /></p>
    </div>
  </div>
</template>

<style scoped>
.stream {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.bubble {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  background: var(--color-bg-elevated);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.bubble__role {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.bubble__text {
  margin: 0;
  font-size: var(--font-size-md);
  line-height: var(--line-height-base);
  color: var(--color-text-primary);
  white-space: pre-wrap;
}

.caret {
  display: inline-block;
  width: 6px;
  height: 14px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--color-brand);
  animation: blink 1.1s step-end infinite;
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

.reasoning {
  border-left: 3px solid var(--color-border-strong);
  padding: var(--space-1) var(--space-3);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-sm);
}

.reasoning > summary {
  cursor: pointer;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.reasoning__text {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
}
</style>
