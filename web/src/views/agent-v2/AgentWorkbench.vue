<script setup lang="ts">
/**
 * Agent v2 工作台（Codex 风格）：线程 / Turn / 工具 / 审批 / 计划 / 压缩 / 记忆 / 子 Agent。
 *
 * 本页**不自带路由**：全局路由由协调 Agent 集成（见 `web/src/agent-v2/README.md` 的挂载补丁）。
 * 页面通过 props 接收服务端地址与身份参数，默认指向同源 `/api/v2/agent/ws`。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import AgentTreePanel from '../../components/agent-v2/AgentTreePanel.vue'
import ApprovalCard from '../../components/agent-v2/ApprovalCard.vue'
import CompactionPanel from '../../components/agent-v2/CompactionPanel.vue'
import Composer from '../../components/agent-v2/Composer.vue'
import ConnectionBanner from '../../components/agent-v2/ConnectionBanner.vue'
import MemoryPanel from '../../components/agent-v2/MemoryPanel.vue'
import PlanPanel from '../../components/agent-v2/PlanPanel.vue'
import ThreadRail from '../../components/agent-v2/ThreadRail.vue'
import TurnControls from '../../components/agent-v2/TurnControls.vue'
import TurnStream from '../../components/agent-v2/TurnStream.vue'
import { METHOD, type ApprovalDecision, type ApprovalView } from '../../agent-v2/protocol'
import { useAgentV2Store } from '../../stores/agent-v2'

const props = withDefaults(
  defineProps<{
    wsUrl?: string
    ownerToken?: string
    userId?: string
    projectId?: string
  }>(),
  { wsUrl: '', ownerToken: '', userId: 'default', projectId: 'default' },
)

const store = useAgentV2Store()
const submittingApproval = ref(false)
const draft = ref('')
const scenarios = ref<string[]>([])

const wsUrl = computed(() => {
  if (props.wsUrl) return props.wsUrl
  if (typeof window === 'undefined') return '/api/v2/agent/ws'
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}/api/v2/agent/ws`
})

const currentThreadId = computed(() => store.state.currentThreadId)
const active = computed(() => store.active)
const composerMode = computed<'new' | 'steer' | 'continue'>(() => {
  const status = active.value?.status
  if (status === 'waiting_input') return 'continue'
  if (status === 'running') return 'steer'
  return 'new'
})

const currentPlan = computed(() => store.plan ?? null)
const selectedTurn = computed(() => {
  const turn = active.value
  if (turn) return turn
  const list = store.turns
  return list.length ? list[list.length - 1] : null
})
const streamItems = computed(() => (selectedTurn.value ? store.itemsOfTurn(selectedTurn.value.turn_id) : []))
const liveText = computed(() =>
  selectedTurn.value ? store.assistantText(selectedTurn.value.turn_id) : '',
)
const liveReasoning = computed(() =>
  selectedTurn.value ? store.reasoningText(selectedTurn.value.turn_id) : '',
)

function approvalOf(approvalId: string): ApprovalView | null {
  return store.state.approvals[approvalId] ?? null
}

onMounted(async () => {
  store.connect({ url: wsUrl.value, token: props.ownerToken || undefined })
  await store.listThreads()
  const described = await store.client?.request(METHOD.describe, {})
  const result = described?.ok ? (described.result ?? {}) : {}
  scenarios.value = (result.scenarios as string[]) ?? []
  const first = store.threads[0]
  if (first) await store.selectThread(first.thread_id)
})

onBeforeUnmount(() => store.disconnect())

async function onCreate(payload: { name: string; scenario?: string }): Promise<void> {
  await store.createThread(payload.name, { scenario: payload.scenario })
  await store.listThreads()
}

async function onSubmit(text: string): Promise<void> {
  const threadId = currentThreadId.value
  if (!threadId) return
  const mode = composerMode.value
  if (mode === 'new') {
    await store.startTurn(threadId, text)
    return
  }
  const turn = active.value
  if (!turn) return
  if (mode === 'continue') await store.continueTurn(threadId, turn.turn_id, text)
  else await store.steerTurn(threadId, turn.turn_id, text)
}

async function onResolve(payload: { approval: ApprovalView; decision: ApprovalDecision }): Promise<void> {
  submittingApproval.value = true
  try {
    await store.resolveApproval(
      payload.approval.thread_id,
      payload.approval.turn_id,
      payload.approval.approval_id,
      payload.decision,
    )
  } finally {
    submittingApproval.value = false
  }
}

async function onMemoryLoad(payload: { scope: 'user' | 'project' | 'conversation'; scopeId: string; includeDeleted: boolean }): Promise<void> {
  await store.listMemories(payload.scope, payload.scopeId, payload.includeDeleted)
}

async function onMemorySave(payload: {
  scope: 'user' | 'project' | 'conversation'
  scopeId: string
  text: string
  memoryId?: string
}): Promise<void> {
  await store.updateMemory(payload.scope, payload.scopeId, payload.text, { memoryId: payload.memoryId })
}
</script>

<template>
  <div class="workbench">
    <header class="workbench__header">
      <div class="workbench__title">
        <h1>Agent 工作台</h1>
        <span class="workbench__sub" :title="currentThreadId ?? ''">{{ store.currentThread?.name ?? '未选择会话' }}</span>
      </div>
      <ConnectionBanner
        :connection="store.connection"
        :cursor="store.cursor"
        :gap-from="store.gapFrom"
        :backfilled="store.backfilled"
        :notices="store.notices"
        @reconnect="store.connect({ url: wsUrl, token: props.ownerToken || undefined })"
        @backfill="currentThreadId && store.backfill(currentThreadId)"
      />
    </header>

    <div class="workbench__body">
      <ThreadRail
        :threads="store.threads"
        :current-thread-id="currentThreadId"
        :busy="store.busy"
        :scenarios="scenarios"
        @select="store.selectThread($event)"
        @create="onCreate"
        @archive="store.deleteThread($event)"
      />

      <main class="workbench__main">
        <TurnControls
          :turn="selectedTurn"
          :busy="store.busy"
          :draft="draft"
          @steer="(text) => store.steerTurn(currentThreadId as string, selectedTurn?.turn_id as string, text)"
          @continue="(text) => store.continueTurn(currentThreadId as string, selectedTurn?.turn_id as string, text)"
          @interrupt="store.interruptTurn(currentThreadId as string, selectedTurn?.turn_id as string)"
          @recover="store.recoverTurn(currentThreadId as string, selectedTurn?.turn_id as string)"
          @retry="store.startTurn(currentThreadId as string, liveText || '重试上一轮')"
        />

        <ApprovalCard
          v-for="approval in store.approvals"
          :key="approval.approval_id"
          :approval="approval"
          :thread-cwd="store.currentThread?.cwd ?? null"
          :submitting="submittingApproval"
          @resolve="(decision) => onResolve({ approval, decision })"
        />

        <TurnStream
          :items="streamItems"
          :stream-text="liveText"
          :stream-reasoning="liveReasoning"
          :tool-call-of="store.toolCall"
          :tool-output-of="store.toolOutput"
          :approval-of="approvalOf"
          :thread-cwd="store.currentThread?.cwd ?? null"
          :submitting-approval="submittingApproval"
          @resolve="onResolve"
        />

        <Composer
          :mode="composerMode"
          :busy="store.busy"
          :disabled="!currentThreadId"
          @submit="onSubmit"
        />
      </main>

      <aside class="workbench__side">
        <PlanPanel v-if="currentPlan" :plan="currentPlan" />
        <CompactionPanel
          :runs="store.compactionRuns"
          :summaries="store.summaries"
          :busy="store.busy"
          @compact="currentThreadId && store.compact(currentThreadId)"
          @refresh="currentThreadId && store.refreshSummaries(currentThreadId)"
          @edit="(payload) => currentThreadId && store.editSummary(currentThreadId, payload.summaryId, payload.text)"
          @restore="(summaryId) => currentThreadId && store.restoreSummary(currentThreadId, summaryId)"
        />
        <AgentTreePanel
          :parent-thread-id="currentThreadId ?? ''"
          :children="store.children"
          :busy="store.busy"
          @refresh="currentThreadId && store.refreshChildren(currentThreadId)"
          @wait="(childId) => currentThreadId && store.waitChildren(currentThreadId, [childId], 5)"
          @wait-all="
            currentThreadId &&
              store.waitChildren(
                currentThreadId,
                store.children.map((child) => child.thread_id),
                5,
              )
          "
          @interrupt="(childId) => currentThreadId && store.interruptChild(currentThreadId, childId)"
        />
        <MemoryPanel
          :records-by-scope="store.state.memories"
          :thread-id="currentThreadId"
          :user-id="props.userId"
          :project-id="props.projectId"
          :busy="store.busy"
          @load="onMemoryLoad"
          @save="onMemorySave"
          @remove="(payload) => store.deleteMemory(payload.memoryId, payload.scopeId)"
        />
      </aside>
    </div>
  </div>
</template>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-4);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  min-height: 100%;
}

.workbench__header {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.workbench__title {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.workbench__title h1 {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.workbench__sub {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.workbench__body {
  display: grid;
  grid-template-columns: 240px minmax(0, 1fr) 360px;
  gap: var(--space-4);
  align-items: start;
}

.workbench__main {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
}

.workbench__side {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
}

@media (max-width: 1280px) {
  .workbench__body {
    grid-template-columns: 220px minmax(0, 1fr);
  }

  .workbench__side {
    grid-column: 1 / -1;
  }
}
</style>
