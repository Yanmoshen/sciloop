<script setup lang="ts">
/**
 * Agent v2 工作台（Codex 风格）：会话 / 回合 / 工具 / 审批 / 计划 / 压缩 / 记忆 / 子 Agent。
 *
 * 本页**不自带路由**：全局路由由协调 Agent 集成（见 `web/src/agent-v2/README.md`）。
 * 页面只负责编排：状态来自 store，规则在 reducer，组件只管画。
 *
 * 两条产品口径：
 * - 研究意图先弹确认卡，确认前不显示任何执行进度；
 * - 停止按钮按下后，三点/计时/「生成中」立刻收束。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import AgentTreePanel from '../../components/agent-v2/AgentTreePanel.vue'
import CompactionPanel from '../../components/agent-v2/CompactionPanel.vue'
import ConnectionBanner from '../../components/agent-v2/ConnectionBanner.vue'
import ConversationShell from '../../components/agent-v2/ConversationShell.vue'
import MemoryPanel from '../../components/agent-v2/MemoryPanel.vue'
import PlanPanel from '../../components/agent-v2/PlanPanel.vue'
import ThreadRail from '../../components/agent-v2/ThreadRail.vue'
import { looksLikeResearch, type ResearchIntent } from '../../components/agent-v2/blocks'
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
const scenarios = ref<string[]>([])
const pendingResearch = ref<ResearchIntent | null>(null)

const wsUrl = computed(() => {
  if (props.wsUrl) return props.wsUrl
  if (typeof window === 'undefined') return '/api/v2/agent/ws'
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${scheme}//${window.location.host}/api/v2/agent/ws`
})

const currentThreadId = computed(() => store.state.currentThreadId)
const activeTurn = computed(() => store.active)
const currentPlan = computed(() => store.plan ?? null)
const selectedTurn = computed(() => {
  if (activeTurn.value) return activeTurn.value
  const list = store.turns
  return list.length ? list[list.length - 1] : null
})
// 对话正文属于整个线程；控制区仍只针对当前活动/最近一轮。
// 之前这里只取 selectedTurn，连续对话后页面会把历史消息全部隐藏。
const streamItems = computed(() =>
  store.turns.flatMap((turn) => store.itemsOfTurn(turn.turn_id)),
)
const liveText = computed(() => (selectedTurn.value ? store.assistantText(selectedTurn.value.turn_id) : ''))
const liveReasoning = computed(() =>
  selectedTurn.value ? store.reasoningText(selectedTurn.value.turn_id) : '',
)
/** 已停止：界面立刻不再显示三点/生成中（等中断事件只是确认，不改节奏）。 */
const stopped = computed(() => store.stopRequested && selectedTurn.value?.turn_id === store.stopping)

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
  window.addEventListener('beforeunload', store.persistNow)
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', store.persistNow)
  store.persistNow()
  store.disconnect()
})

async function onCreate(payload: { name: string; scenario?: string }): Promise<void> {
  await store.createThread(payload.name, { scenario: payload.scenario })
  await store.listThreads()
}

/** 提交：研究意图先确认；普通问候直接进入对话。 */
async function onSubmit(text: string): Promise<void> {
  if (!currentThreadId.value) return
  if (looksLikeResearch(text)) {
    pendingResearch.value = { question: text, sources: ['arXiv', 'Semantic Scholar', 'OpenAlex'] }
    return
  }
  await store.startTurn(currentThreadId.value, text)
}

async function onConfirmResearch(intent: ResearchIntent): Promise<void> {
  const threadId = currentThreadId.value
  if (!threadId) return
  const parts = [intent.question]
  if (intent.scope) parts.push(`范围与约束：${intent.scope}`)
  if (intent.sources?.length) parts.push(`优先检索源：${intent.sources.join('、')}`)
  pendingResearch.value = null
  await store.startTurn(threadId, parts.join('\n'))
}

function onCancelResearch(): void {
  pendingResearch.value = null
}

async function onResolve(payload: { approval: ApprovalView; decision: ApprovalDecision }): Promise<void> {
  await store.resolveApproval(
    payload.approval.thread_id,
    payload.approval.turn_id,
    payload.approval.approval_id,
    payload.decision,
  )
}

async function onMemoryLoad(payload: {
  scope: 'user' | 'project' | 'conversation'
  scopeId: string
  includeDeleted: boolean
}): Promise<void> {
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
      <h1 class="workbench__title">Agent 工作台</h1>
      <ConnectionBanner
        :connection="store.connection"
        :snapshot-restored="store.snapshotRestored"
        :gap-pending="store.gapFrom !== null"
        :notices="store.notices"
        @reconnect="store.connect({ url: wsUrl, token: props.ownerToken || undefined })"
        @refresh="currentThreadId && store.resumeThread(currentThreadId)"
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
        <ConversationShell
          :thread="store.currentThread"
          :items="streamItems"
          :active-turn="activeTurn"
          :stream-text="liveText"
          :stream-reasoning="liveReasoning"
          :approvals="store.approvals"
          :tool-call-of="store.toolCall"
          :tool-output-of="store.toolOutput"
          :approval-of="approvalOf"
          :busy="store.busy"
          :stopped="stopped"
          :read-only="store.readOnly"
          :pending-research="pendingResearch"
          @submit="onSubmit"
          @steer="(text) => currentThreadId && activeTurn && store.steerTurn(currentThreadId, activeTurn.turn_id, text)"
          @continue="(text) => currentThreadId && activeTurn && store.continueTurn(currentThreadId, activeTurn.turn_id, text)"
          @interrupt="currentThreadId && activeTurn && store.stopTurn(currentThreadId, activeTurn.turn_id)"
          @recover="currentThreadId && activeTurn && store.recoverTurn(currentThreadId, activeTurn.turn_id)"
          @retry="currentThreadId && store.startTurn(currentThreadId, liveText || '重试上一轮')"
          @resolve="onResolve"
          @confirm-research="onConfirmResearch"
          @cancel-research="onCancelResearch"
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
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
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
