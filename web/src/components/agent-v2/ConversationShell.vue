<script setup lang="ts">
/**
 * 会话外壳（WP-05 的 `ConversationShell`）：标题、消息流、Turn 状态与输入框。
 *
 * 只做排版与事件转发，不持有业务状态——状态在 store，规则在 reducer。
 * 停止后的收束由 `stopped` 决定：为真时立刻不显示三点/生成中/计时。
 */
import ApprovalCard from './ApprovalCard.vue'
import Composer from './Composer.vue'
import ResearchConfirmCard from './ResearchConfirmCard.vue'
import TurnControls from './TurnControls.vue'
import TurnStream from './TurnStream.vue'
import type { ResearchIntent } from './blocks'
import type {
  ApprovalDecision,
  ApprovalView,
  ItemView,
  ThreadView,
  ToolCallView,
  TurnView,
} from '../../agent-v2/protocol'

const props = defineProps<{
  thread: ThreadView | null
  items: ItemView[]
  activeTurn: TurnView | null
  streamText: string
  streamReasoning: string
  approvals: ApprovalView[]
  toolCallOf: (callId: string | null) => ToolCallView | null
  toolOutputOf: (callId: string) => string
  approvalOf: (approvalId: string) => ApprovalView | null
  busy?: boolean
  stopped?: boolean
  readOnly?: boolean
  /** 待确认的研究意图（存在时只显示确认卡，不显示任何执行进度）。 */
  pendingResearch?: ResearchIntent | null
}>()

const emit = defineEmits<{
  (event: 'submit', text: string): void
  (event: 'steer', text: string): void
  (event: 'continue', text: string): void
  (event: 'interrupt'): void
  (event: 'recover'): void
  (event: 'retry'): void
  (event: 'resolve', payload: { approval: ApprovalView; decision: ApprovalDecision }): void
  (event: 'confirm-research', intent: ResearchIntent): void
  (event: 'cancel-research'): void
}>()

const composerMode = (): 'new' | 'steer' | 'continue' => {
  if (props.activeTurn?.status === 'waiting_input') return 'continue'
  if (props.activeTurn?.status === 'running') return 'steer'
  return 'new'
}
</script>

<template>
  <section class="shell">
    <header class="shell__head">
      <h2 class="shell__title">{{ thread?.name ?? '未选择会话' }}</h2>
      <span v-if="readOnly" class="shell__readonly">只读浏览</span>
    </header>

    <div v-if="!thread" class="shell__empty">从左侧选择一个会话，或新建一个。</div>

    <template v-else>
      <ResearchConfirmCard
        v-if="pendingResearch"
        :intent="pendingResearch"
        :busy="busy"
        @confirm="(intent) => emit('confirm-research', intent)"
        @cancel="emit('cancel-research')"
      />

      <TurnStream
        :items="items"
        :stream-text="streamText"
        :stream-reasoning="streamReasoning"
        :tool-call-of="toolCallOf"
        :tool-output-of="toolOutputOf"
        :approval-of="approvalOf"
        :stopped="stopped"
      >
        <template #approval="{ approval }">
          <ApprovalCard
            :approval="approval"
            :thread-cwd="thread.cwd"
            :submitting="busy"
            @resolve="(decision: ApprovalDecision) => emit('resolve', { approval, decision })"
          />
        </template>
      </TurnStream>

      <TurnControls
        :turn="activeTurn"
        :busy="busy"
        :stopped="stopped"
        @steer="(text: string) => emit('steer', text)"
        @continue="(text: string) => emit('continue', text)"
        @interrupt="emit('interrupt')"
        @recover="emit('recover')"
        @retry="emit('retry')"
      />

      <Composer
        :mode="composerMode()"
        :busy="busy"
        :disabled="readOnly"
        @submit="(text: string) => emit('submit', text)"
      />
    </template>
  </section>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
}

.shell__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.shell__title {
  margin: 0;
  font-size: var(--font-size-lg);
  color: var(--color-text-primary);
}

.shell__readonly {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  padding: 1px var(--space-2);
}

.shell__empty {
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-5);
  text-align: center;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}
</style>
