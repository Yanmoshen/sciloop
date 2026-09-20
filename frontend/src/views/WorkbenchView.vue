<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 流水线工作台（WP15-T1，整文件覆盖 WP01 占位实现）。
 *
 * 布局：
 * - 顶部：当前项目（只读展示）+ 运行控制（启动/暂停/继续/停止/切换模式）
 *   + 停止原因显著展示 + 实时连接状态
 * - 成本双线常驻（护栏 8.0 / 演示配额 3.0）
 * - 主体按 Tab 分区：看板 / 决策日志 / 实验与 Passport / 盲评校准 / 草稿与三件套
 *
 * 铁律：组件内零 fetch（全部经 store action）；零硬编码色值（只用 tokens.css 变量）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { liveStatus } from '@/utils/messages'
import BlindReviewPanel from '@/components/BlindReviewPanel.vue'
import CostGuardrailBar from '@/components/CostGuardrailBar.vue'
import DecisionLogPanel from '@/components/DecisionLogPanel.vue'
import DraftViewer from '@/components/DraftViewer.vue'
import InterventionPanel from '@/components/InterventionPanel.vue'
import PassportViewer from '@/components/PassportViewer.vue'
import PipelineBoard from '@/components/PipelineBoard.vue'
import RiskPolicyPanel from '@/components/RiskPolicyPanel.vue'
import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { STOP_REASON_LABELS, type PipelineMode } from '@/api/workbench'
import { useSessionStore } from '@/stores/session'
import { useWorkbenchStore } from '@/stores/workbench'

const route = useRoute()
const router = useRouter()
const store = useWorkbenchStore()
const session = useSessionStore()

const activeTab = ref('board')

const canWrite = computed(() => session.isOwner || store.ownerWritesAllowed)
const writeBlockReason = computed(() =>
  canWrite.value ? null : 'public_demo 面禁止写操作（需 Owner 令牌；后端会独立校验并返回 403 owner_token_required）',
)
/**
 * 权限拒绝态：只读面下写入口已**前置禁用**；若本次请求确实收到 403，
 * 再明确说明「已被拒绝」。依据 store 记录的**真实** HTTP 状态码判断，
 * 不靠对错误文案做字符串匹配。
 */
const permissionDenied = computed(() => !canWrite.value || store.errorStatus === 403)
const permissionTitle = computed(() =>
  store.errorStatus === 403
    ? '权限受限：该写操作已被服务端拒绝（403 owner_token_required）'
    : '只读演示面：运行控制与人工介入入口已前置禁用（非请求被拒）',
)
const permissionNote = computed(() => {
  const reason = session.health ? session.accessLabel : '访问面未获取'
  return [
    writeBlockReason.value,
    `当前访问面：${reason}。启动/暂停/继续/停止/切换模式/人工介入与 Passport 重跑均已前置禁用，避免出现「点了没反应」的入口。`,
  ]
    .filter(Boolean)
    .join(' ')
})

const projectStatus = computed(() => store.status?.project_status ?? '—')
const stopReasonText = computed(() =>
  store.stopReason ? `${store.stopReason}（${STOP_REASON_LABELS[store.stopReason]}）` : '未停止',
)

/**
 * 降级说明：由服务端/接口如实给出的非阻塞缺口拼成。
 * - `projectsNote`：Project 列表走了回退接口；
 * - `costNote`：`GET /costs/summary` 不可用，已回退 `status.cost` 的双线阈值。
 * 两者都不是「整页失败」，因此以顶部提示条而不是 error 呈现。
 */

const connectionText = computed(() => liveStatus(store.connection))

const projectOptions = computed(() =>
  store.projects.map((item) => ({
    value: item.id,
    label: `${item.name}${item.is_demo ? '（示例）' : ''} · #${item.id} · ${item.status ?? '—'}`,
  })),
)

/**
 * 路由参数 → Project id。
 *
 * - `/workbench/33` → 33（深链以路由为准）
 * - `/workbench/demo`（左侧导航占位）→ null，退回 session/项目列表首条
 */
function routeProjectId(): number | null {
  const raw = route.params.projectId
  const value = Number(Array.isArray(raw) ? raw[0] : raw)
  return Number.isFinite(value) && value > 0 ? value : null
}

/** bootstrap 完成后 session 的变更才视为用户意图，避免初始化期默认项目覆盖深链 */
let bootstrapped = false

/** 单点落地：session / store / URL 三方同源，杜绝“URL 是 A、数据是 B” */
function applyProject(id: number | null): void {
  if (id === null) return
  session.selectProject(id)
  store.setProject(id)
  void store.refreshAll().then(() => store.startStream())
}

/** Project 切换器：先对齐 URL，由路由 watch 触发拉取，刷新后仍可恢复同一 Project */
function selectProject(id: number | null): void {
  if (id === null) return
  session.selectProject(id)
  if (routeProjectId() === id) {
    applyProject(id)
    return
  }
  void router
    .push({ name: 'workbench', params: { projectId: String(id) } })
    .catch(() => undefined)
}

async function bootstrap(): Promise<void> {
  await store.refreshProjects()
  applyProject(routeProjectId() ?? session.currentProjectId ?? store.projects[0]?.id ?? null)
  bootstrapped = true
}

onMounted(() => {
  void bootstrap()
})

onBeforeUnmount(() => {
  store.stopStream()
})

watch(
  () => route.params.projectId,
  () => {
    applyProject(routeProjectId() ?? session.currentProjectId)
  },
)

watch(
  () => session.currentProjectId,
  (value) => {
    if (!bootstrapped || value === null || value === store.projectId) return
    // 顶栏（WP01）切换 Project 时跟随，并同步 URL 以保持刷新可恢复
    selectProject(value)
  },
)

function switchMode(): void {
  const next: PipelineMode = store.status?.mode === 'auto' ? 'manual' : 'auto'
  void store.switchMode(next)
}

function gotoPassport(runId: number | null): void {
  if (runId !== null) store.passportRunId = runId
  activeTab.value = 'passport'
}
</script>

<template>
  <section class="workbench">
    <!-- 顶部控制区 -->
    <header class="topbar">
      <div class="topbar-row">
        <span class="label">当前项目</span>
        <span class="project-name" :title="session.currentProject?.name ?? '未选择项目'">
          {{ session.currentProject?.name ?? '未选择项目' }}
        </span>
        <span class="chip" :class="`chip--status-${projectStatus}`">{{ projectStatus }}</span>
        <span class="chip">模式 {{ store.status?.mode ?? '—' }}</span>
        <span class="chip">迭代 {{ store.iteration }}</span>
        <span class="chip" :class="`chip--sse-${store.connection}`">{{ connectionText }}</span>
      </div>

      <div class="topbar-row">
        <el-button size="small" :disabled="!canWrite" :loading="store.busy === 'start'" @click="store.startRun()">
          启动
        </el-button>
        <el-button size="small" :disabled="!canWrite" :loading="store.busy === 'pause'" @click="store.pauseRun()">
          暂停
        </el-button>
        <el-button size="small" :disabled="!canWrite" :loading="store.busy === 'resume'" @click="store.resumeRun()">
          继续（断点续跑）
        </el-button>
        <el-button size="small" :disabled="!canWrite" :loading="store.busy === 'stop'" @click="store.stopRun('manual')">
          停止
        </el-button>
        <el-button size="small" :disabled="!canWrite" :loading="store.busy === 'switch-mode'" @click="switchMode">
          切换为 {{ store.status?.mode === 'auto' ? 'manual' : 'auto' }}
        </el-button>
        <el-button size="small" :loading="store.loading" @click="store.refreshAll()">刷新快照</el-button>

        <!-- 风险策略指示器 -->
        <span class="risk-indicator" :class="`risk-indicator--${store.policyActionCounts.need_human > 0 ? 'human' : 'auto'}`">
          风险策略：auto_execute {{ store.policyActionCounts.auto_execute }} · need_human
          {{ store.policyActionCounts.need_human }} · circuit_break {{ store.policyActionCounts.circuit_break }}
        </span>
        <span v-if="permissionDenied" class="muted muted--warn">{{ writeBlockReason }}</span>
      </div>

      <div class="topbar-row">
        <span class="stop-banner" :class="{ 'stop-banner--active': store.stopReason, 'stop-banner--human': store.isWaitingHuman }">
          stop_reason：<strong>{{ stopReasonText }}</strong>
          <span v-if="store.status?.run">· run #{{ store.status.run.id }} · {{ store.status.run.status }}</span>
          <span v-if="store.isWaitingHuman">· 等待人工介入（{{ store.pendingDecision?.decision_point ?? '—' }}）</span>
        </span>
        <span v-if="store.lastSyncedAt" class="muted">
          快照同步 {{ new Date(store.lastSyncedAt).toLocaleTimeString() }}（以接口状态为准）
        </span>
        <span v-if="store.projectsNote" class="muted muted--warn">{{ store.projectsNote }}</span>
      </div>
    </header>

    <!-- 六类状态：loading / error / permission denied / retry（empty 见下方 el-empty） -->
    <ViewStatePanel
      :loading="store.loading"
      loading-text="正在同步流水线快照（status / stages / 决策 / 介入 / 成本 / 校准）…"
      :error="store.errorMessage || null"
      :error-code="store.errorCode"
      error-title="流水线数据同步失败"
      :permission-denied="permissionDenied"
      :permission-note="permissionNote"
      :permission-title="permissionTitle"
      retryable
      retry-label="重新同步快照"
      :busy="store.loading"
      @retry="store.refreshAll()"
    />

    <el-alert v-if="store.noticeMessage" type="success" :closable="true" show-icon @close="store.clearNotice()">
      <template #title>{{ store.noticeMessage }}</template>
    </el-alert>

    <el-alert v-if="store.circuitBreak" type="error" :closable="false" show-icon>
      <template #title>已熔断：{{ store.circuitBreak.reason }}</template>
      <template #default>
        <a :href="store.circuitBreak.report_url">《失败分析报告》{{ store.circuitBreak.report_url }}</a>
      </template>
    </el-alert>

    <!-- 成本双线常驻 -->
    <CostGuardrailBar />

    <el-empty
      v-if="store.projects.length === 0 && store.projectId === null && !store.loading"
      description="尚无 Project 可运行"
    />

    <!-- 主体分区 -->
    <el-tabs v-model="activeTab" class="tabs">
      <el-tab-pane label="看板" name="board">
        <PipelineBoard />
      </el-tab-pane>
      <el-tab-pane label="决策日志" name="decisions">
        <DecisionLogPanel />
      </el-tab-pane>
      <el-tab-pane label="风险策略与介入" name="risk">
        <div class="split">
          <RiskPolicyPanel />
          <InterventionPanel />
        </div>
      </el-tab-pane>
      <el-tab-pane label="实验与 Passport" name="passport">
        <PassportViewer />
      </el-tab-pane>
      <el-tab-pane label="盲评校准" name="blind">
        <BlindReviewPanel />
      </el-tab-pane>
      <el-tab-pane label="草稿与三件套" name="draft">
        <DraftViewer @goto-passport="gotoPassport" />
      </el-tab-pane>
    </el-tabs>
  </section>
</template>

<style scoped>
.workbench {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.topbar {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}

.topbar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.project-select {
  width: 280px;
}

.project-name {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
}

.muted {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.muted--warn {
  color: var(--color-warning);
}

.chip {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.chip--owner {
  border-color: var(--color-brand);
  color: var(--color-brand);
  background-color: var(--color-brand-soft);
}

.chip--demo {
  border-color: var(--color-warning);
  color: var(--color-demo-badge-text);
  background-color: var(--color-demo-badge-bg);
}

.chip--status-WAIT_HUMAN {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.chip--status-RUNNING {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.chip--status-CIRCUIT_BREAK,
.chip--status-ABORTED {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.chip--status-DONE {
  border-color: var(--color-success);
  color: var(--color-success);
}

.chip--sse-open {
  border-color: var(--color-success);
  color: var(--color-success);
}

.chip--sse-reconnecting,
.chip--sse-connecting {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.chip--sse-error {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.risk-indicator {
  margin-left: auto;
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.risk-indicator--human {
  border-color: var(--color-warning);
  color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.stop-banner {
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.stop-banner strong {
  color: var(--color-text-primary);
}

.stop-banner--active {
  border-color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.stop-banner--human {
  border-color: var(--color-brand);
  background-color: var(--color-brand-soft);
}

.tabs {
  padding: var(--space-2) var(--space-3) var(--space-4);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.split {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
</style>
