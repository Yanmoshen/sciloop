<script setup lang="ts">
/**
 * 单条消息 / 事件块（WP-05 的 `MessageItem`）。
 *
 * 只负责画「一块」，不含任何状态推断；类型由 `blocks.ts` 归约得到。
 * 文案口径：只显示真实结论、必要失败原因与下一步；**不显示**内部 ID、错误码、
 * 模型思考、令牌或重试细节（验收 §7.3）。
 */
import PlanPanel from './PlanPanel.vue'
import ToolCallCard from './ToolCallCard.vue'
import type { Block } from './blocks'

defineProps<{
  block: Block
}>()

const SUBAGENT_LABEL: Record<string, string> = {
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断',
}

/** 把内部错误码翻译成用户能理解的一句话（界面不出现 code）。 */
const FAILURE_HINT: Record<string, string> = {
  approval_denied: '研究者拒绝了这次操作',
  execution_failed: '执行没有成功',
  timeout: '执行超时',
  cancelled: '操作已取消',
  invalid_arguments: '参数不合法',
  unknown_tool: '这个工具当前不可用',
}

function failureText(code: string, message: string): string {
  return FAILURE_HINT[code] ?? message
}

function subagentLabel(status: string): string {
  return SUBAGENT_LABEL[status] ?? status
}
</script>

<template>
  <div v-if="block.kind === 'user'" class="bubble bubble--user">
    <span class="bubble__role">研究者</span>
    <p class="bubble__text">{{ block.text }}</p>
  </div>

  <details v-else-if="block.kind === 'reasoning'" class="reasoning">
    <summary>推理过程</summary>
    <p class="reasoning__text">{{ block.text }}</p>
  </details>

  <div v-else-if="block.kind === 'assistant'" class="bubble bubble--agent">
    <span class="bubble__role">助手</span>
    <p class="bubble__text">{{ block.text }}</p>
  </div>

  <ToolCallCard v-else-if="block.kind === 'tool'" :call="block.call" :output="block.output" />

  <slot v-else-if="block.kind === 'approval'" name="approval" :approval="block.approval" />

  <PlanPanel v-else-if="block.kind === 'plan'" :plan="block.plan" />

  <article
    v-else-if="block.kind === 'subagent'"
    class="subagent"
    :class="`subagent--${block.status}`"
  >
    <header class="subagent__head">
      <span class="subagent__title">子 Agent 结果</span>
      <span class="subagent__status">{{ subagentLabel(block.status) }}</span>
      <span class="subagent__from">来自子任务</span>
    </header>
    <p class="subagent__summary">{{ block.summary }}</p>
  </article>

  <div v-else-if="block.kind === 'error'" class="failure">
    <span class="failure__message">{{ failureText(block.code, block.message) }}</span>
  </div>
</template>

<style scoped>
.bubble {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  background: var(--color-bg-elevated);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.bubble--user {
  background: var(--color-bg-subtle);
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
  max-height: 240px;
  overflow: auto;
}

.subagent {
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  background: var(--color-bg-subtle);
}

.subagent--completed {
  border-left-color: var(--color-success);
}

.subagent--failed {
  border-left-color: var(--color-danger);
}

.subagent--interrupted {
  border-left-color: var(--color-warning);
}

.subagent__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.subagent__title {
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.subagent__from {
  margin-left: auto;
}

.subagent__summary {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.failure {
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-sm);
  background: var(--color-danger-soft);
  padding: var(--space-2) var(--space-3);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}
</style>
