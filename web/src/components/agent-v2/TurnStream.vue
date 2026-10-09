<script setup lang="ts">
/**
 * Turn 事件流渲染：文本与推理增量、工具调用、审批、计划、子 Agent 结果与错误。
 *
 * 渲染顺序严格跟事件序号（Item.sequence），不做任何重排。
 * 实现上先把 Item 归约成**带类型的渲染块**（`Block`），模板里只做 `block.kind` 分支——
 * 这样既避免了模板里的类型断言，也让「什么事件渲染成什么」一目了然。
 */
import { computed } from 'vue'

import ApprovalCard from './ApprovalCard.vue'
import PlanPanel from './PlanPanel.vue'
import ToolCallCard from './ToolCallCard.vue'
import type {
  ApprovalDecision,
  ApprovalView,
  ItemView,
  PlanView,
  ToolCallView,
} from '../../agent-v2/protocol'

const props = defineProps<{
  items: ItemView[]
  streamText: string
  streamReasoning: string
  toolCallOf: (callId: string | null) => ToolCallView | null
  toolOutputOf: (callId: string) => string
  approvalOf: (approvalId: string) => ApprovalView | null
  threadCwd?: string | null
  submittingApproval?: boolean
}>()

const emit = defineEmits<{
  (event: 'resolve', payload: { approval: ApprovalView; decision: ApprovalDecision }): void
}>()

type Block =
  | { kind: 'user'; key: string; text: string }
  | { kind: 'reasoning'; key: string; text: string }
  | { kind: 'assistant'; key: string; text: string }
  | { kind: 'tool'; key: string; call: ToolCallView; output: string }
  | { kind: 'approval'; key: string; approval: ApprovalView }
  | { kind: 'plan'; key: string; plan: PlanView }
  | { kind: 'subagent'; key: string; status: string; threadId: string; summary: string }
  | { kind: 'error'; key: string; code: string; message: string }

function planOf(item: ItemView): PlanView {
  const steps = Array.isArray(item.payload.steps) ? item.payload.steps : []
  return {
    item_id: item.item_id,
    title: String(item.payload.title ?? '任务计划'),
    steps: steps as PlanView['steps'],
    note: (item.payload.note as string | null) ?? null,
    created_at: item.created_at,
  }
}

const blocks = computed<Block[]>(() => {
  const out: Block[] = []
  for (const item of props.items) {
    const key = item.item_id
    switch (item.type) {
      case 'user_input':
        out.push({ kind: 'user', key, text: String(item.payload.text ?? '') })
        break
      case 'reasoning':
        out.push({ kind: 'reasoning', key, text: String(item.payload.text ?? '') })
        break
      case 'assistant_text':
        out.push({ kind: 'assistant', key, text: String(item.payload.text ?? '') })
        break
      case 'tool_call': {
        const call = props.toolCallOf(item.call_id)
        if (call) out.push({ kind: 'tool', key, call, output: props.toolOutputOf(call.call_id) })
        break
      }
      case 'approval': {
        const approval = props.approvalOf(String(item.payload.approval_id ?? ''))
        if (approval) out.push({ kind: 'approval', key, approval })
        break
      }
      case 'plan':
        out.push({ kind: 'plan', key, plan: planOf(item) })
        break
      case 'subagent_result':
        out.push({
          kind: 'subagent',
          key,
          status: String(item.payload.status ?? 'completed'),
          threadId: String(item.subagent_thread_id ?? ''),
          summary: String(item.payload.summary ?? ''),
        })
        break
      case 'error':
        out.push({
          kind: 'error',
          key,
          code: String(item.payload.code ?? 'error'),
          message: String(item.payload.message ?? ''),
        })
        break
      default:
        // tool_result 与 compaction 不单独成块：前者已在工具卡片里展示，
        // 后者由右侧的压缩面板负责，避免同一事实出现两次。
        break
    }
  }
  return out
})

const hasReasoningBlock = computed(() => blocks.value.some((block) => block.kind === 'reasoning'))

const landedAssistantText = computed(() =>
  blocks.value
    .filter((block): block is Extract<Block, { kind: 'assistant' }> => block.kind === 'assistant')
    .map((block) => block.text)
    .join(''),
)

/** 正在流式中的文本（尚未落成 Item）挂在末尾；一旦 Item 落地就换成权威内容。 */
const liveText = computed(() => {
  const live = props.streamText
  if (!live.trim()) return ''
  return landedAssistantText.value.includes(live) ? '' : live
})

function resolveApproval(approval: ApprovalView, decision: ApprovalDecision): void {
  emit('resolve', { approval, decision })
}
</script>

<template>
  <div class="stream">
    <template v-for="block in blocks" :key="block.key">
      <div v-if="block.kind === 'user'" class="bubble bubble--user">
        <span class="bubble__role">研究者</span>
        <p class="bubble__text">{{ block.text }}</p>
      </div>

      <details v-else-if="block.kind === 'reasoning'" class="reasoning">
        <summary>推理过程（{{ block.text.length }} 字）</summary>
        <p class="reasoning__text">{{ block.text }}</p>
      </details>

      <div v-else-if="block.kind === 'assistant'" class="bubble bubble--agent">
        <span class="bubble__role">助手</span>
        <p class="bubble__text">{{ block.text }}</p>
      </div>

      <ToolCallCard v-else-if="block.kind === 'tool'" :call="block.call" :output="block.output" />

      <ApprovalCard
        v-else-if="block.kind === 'approval'"
        :approval="block.approval"
        :thread-cwd="threadCwd"
        :submitting="submittingApproval"
        @resolve="(decision) => resolveApproval(block.approval, decision)"
      />

      <PlanPanel v-else-if="block.kind === 'plan'" :plan="block.plan" />

      <article
        v-else-if="block.kind === 'subagent'"
        class="subagent"
        :class="`subagent--${block.status}`"
      >
        <header class="subagent__head">
          <span class="subagent__title">子 Agent 结果</span>
          <span class="subagent__status">{{ block.status }}</span>
          <span class="subagent__from" :title="block.threadId">来自 {{ block.threadId.slice(0, 10) }}</span>
        </header>
        <p class="subagent__summary">{{ block.summary }}</p>
      </article>

      <div v-else-if="block.kind === 'error'" class="failure">
        <span class="failure__code">{{ block.code }}</span>
        <span class="failure__message">{{ block.message }}</span>
      </div>
    </template>

    <details v-if="streamReasoning && !hasReasoningBlock" class="reasoning reasoning--live">
      <summary>推理中…</summary>
      <p class="reasoning__text">{{ streamReasoning }}</p>
    </details>

    <div v-if="liveText" class="bubble bubble--agent bubble--live">
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
  font-family: var(--font-family-mono);
}

.subagent__summary {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.failure {
  display: flex;
  gap: var(--space-2);
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-sm);
  background: var(--color-danger-soft);
  padding: var(--space-2) var(--space-3);
  font-size: var(--font-size-xs);
}

.failure__code {
  font-family: var(--font-family-mono);
  color: var(--color-danger);
}
</style>
