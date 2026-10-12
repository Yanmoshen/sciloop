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
import { pendingAssistantText, toBlocks, type Block, type BlockLookups } from './blocks'
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
  /** 当前展示回合状态；终态后不再显示流式光标或迟到增量。 */
  turnStatus?: string | null
}>()

const lookups: BlockLookups = {
  toolCallOf: (callId) => props.toolCallOf(callId),
  toolOutputOf: (callId) => props.toolOutputOf(callId),
  approvalOf: (approvalId) => props.approvalOf(approvalId),
}

const blocks = computed(() => toBlocks(props.items, lookups))

type ToolBlock = Extract<Block, { kind: 'tool' }>
type RenderBlock = Block | {
  kind: 'tool_group'
  key: string
  tools: ToolBlock[]
}

/**
 * Codex 把连续的执行活动收成一个默认收起的组。
 * 这里只合并相邻工具 Item，保留原有事件顺序；没有工具 Item 时不会产生活动组。
 */
const renderBlocks = computed<RenderBlock[]>(() => {
  const result: RenderBlock[] = []
  let toolGroup: ToolBlock[] = []

  const flushTools = (): void => {
    if (!toolGroup.length) return
    result.push({
      kind: 'tool_group',
      key: `tool-group:${toolGroup[0].key}`,
      tools: toolGroup,
    })
    toolGroup = []
  }

  for (const block of blocks.value) {
    if (block.kind === 'tool') {
      toolGroup.push(block)
    } else {
      flushTools()
      result.push(block)
    }
  }
  flushTools()
  return result
})

/** 只在「还没有落地成 Item」时展示流式文本；已停止则一律不展示。 */
const liveText = computed(() => {
  if (props.stopped || ['completed', 'failed', 'interrupted'].includes(props.turnStatus ?? '')) return ''
  const pending = pendingAssistantText(blocks.value, props.streamText)
  return pending.trim() ? pending : ''
})

/**
 * 流式 reasoning 是供应商的内部上下文，不能直接展示。
 * 面向用户的短摘要只从 `item.payload.summary` 进入 `toBlocks`；当前协议
 * 没有单独的摘要增量通道，因此这里等待结构化摘要再渲染。
 */
const liveReasoning = computed(() => '')
</script>

<template>
  <div class="stream">
    <template v-for="block in renderBlocks" :key="block.key">
      <details v-if="block.kind === 'tool_group'" class="tool-group">
        <summary class="tool-group__summary">
          <span>工具活动</span>
          <span class="tool-group__count">{{ block.tools.length }} 项</span>
        </summary>
        <div class="tool-group__items">
          <MessageItem
            v-for="tool in block.tools"
            :key="tool.key"
            :block="tool"
            :stopped="stopped"
          />
        </div>
      </details>

      <MessageItem v-else :block="block" :stopped="stopped">
        <template #approval="{ approval }">
          <slot name="approval" :approval="approval" />
        </template>
      </MessageItem>
    </template>

    <details v-if="liveReasoning" class="reasoning">
      <summary>思考摘要</summary>
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

.tool-group {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  overflow: hidden;
}

.tool-group__summary {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  padding: var(--space-2) var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.tool-group__count {
  color: var(--color-text-disabled);
}

.tool-group__items {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: 0 var(--space-2) var(--space-2);
}

.reasoning__text {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
}
</style>
