<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 六泳道看板（WP15-T2）。
 *
 * survey → plan → plan_review → experiment → writing → review
 * 每道展示：状态 / 耗时 / 成本 / 产出摘要 / 失败信息 / 决策点 / 介入节点 / attempt 轮次。
 *
 * 数据来源：store（快照接口为准 + SSE 实时刷新）；**本组件内不发起任何请求**。
 * 铁律：显著展示 stop_reason 与迭代轮次；颜色一律走 tokens.css 变量。
 */
import { computed } from 'vue'

import {
  DECISION_POINTS,
  INTERVENTION_NODES,
  STAGE_LABELS,
  STAGE_ORDER,
  type PipelineStage,
  type StageDetail,
  type StageStatus,
} from '@/api/workbench'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()

const STATUS_LABELS: Record<StageStatus, string> = {
  pending: '待执行',
  running: '执行中',
  waiting_human: '等待人工',
  done: '已完成',
  failed: '失败',
}

/** 环节 → 决策点 / 介入节点（契约 decision_points / intervention_mapping） */
const decisionPointOf = (stage: PipelineStage): string | null =>
  DECISION_POINTS.find((d) => d.stage === stage)?.id ?? null
const interventionNodeOf = (stage: PipelineStage): string | null =>
  INTERVENTION_NODES.find((n) => n.id !== 'N1' && dStage(n.id) === stage)?.id ?? null

function dStage(nodeId: string): PipelineStage | null {
  if (nodeId === 'N2') return 'plan_review'
  if (nodeId === 'N3') return 'experiment'
  if (nodeId === 'N4') return 'writing'
  return null
}

const lanes = computed(() =>
  STAGE_ORDER.map((stage) => {
    const detail = store.stages.find((s) => s.stage === stage) ?? null
    return {
      stage,
      label: STAGE_LABELS[stage],
      detail,
      status: (detail?.status ?? 'pending') as StageStatus,
      registered: store.status?.stage_registry?.registered?.includes(stage) ?? true,
      decisionPoint: decisionPointOf(stage),
      node: interventionNodeOf(stage),
    }
  }),
)

const knownStages = computed(() => new Set(store.stages.map((s) => s.stage)))
const absentStages = computed(() =>
  STAGE_ORDER.filter((s) => !knownStages.value.has(s) && store.stages.length > 0),
)

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${ms} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} min ${Math.round(seconds % 60)} s`
}

function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `$${Number(value).toFixed(4)}`
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString()
}

/** 产出摘要：只展示 output_json 里真实存在的字段，缺失即明示，不编造 */
function summarize(detail: StageDetail | null): Array<{ label: string; value: string }> {
  const output = detail?.output_json
  if (!output) return []
  const rows: Array<{ label: string; value: string }> = []

  const waiting = (output as { _waiting?: { node?: string; reason?: string } })._waiting
  if (waiting) {
    rows.push({ label: '等待人工', value: `节点 ${waiting.node ?? '—'}` })
    if (waiting.reason) rows.push({ label: '原因', value: waiting.reason })
  }

  const failure = (output as { _failure_report?: Record<string, unknown> })._failure_report
  if (failure) {
    rows.push({ label: '失败等级', value: String(failure.level ?? '—') })
    rows.push({ label: '失败环节', value: String(failure.failed_stage ?? '—') })
    if (failure.suggested_human_actions) {
      const actions = failure.suggested_human_actions as string[]
      rows.push({ label: '建议处置', value: actions.join(' / ') })
    }
  }

  for (const [key, value] of Object.entries(output)) {
    if (key.startsWith('_')) continue
    if (value === null || value === undefined) {
      rows.push({ label: key, value: 'null（接口未给出，未编造）' })
      continue
    }
    if (Array.isArray(value)) {
      rows.push({ label: key, value: `${value.length} 项` })
      continue
    }
    if (typeof value === 'object') {
      const keys = Object.keys(value as Record<string, unknown>)
      rows.push({ label: key, value: `{${keys.slice(0, 6).join(', ')}${keys.length > 6 ? ', …' : ''}}` })
      continue
    }
    rows.push({ label: key, value: String(value) })
  }
  return rows.slice(0, 8)
}

const registryNote = computed(() => {
  const registry = store.status?.stage_registry
  if (!registry) return ''
  if (!registry.missing || registry.missing.length === 0) return ''
  return `未注册环节：${registry.missing.join('、')}（引擎按「环节实现未注册」如实报错，等待 WP11 experiment / WP14 writing·review 交付）`
})
</script>

<template>
  <div class="board">
    <!-- 停止原因 + 迭代轮次：必须显著 -->
    <div class="banner" :class="{ 'banner--stop': store.stopReason, 'banner--warn': store.isWaitingHuman }">
      <div class="banner-main">
        <span class="banner-label">停止原因</span>
        <strong class="banner-value">
          {{ store.stopReason ? store.stopReason : '未停止（无 stop_reason）' }}
        </strong>
        <span v-if="store.stopReason" class="banner-explain">
          {{
            store.stopReason === 'score_threshold'
              ? '达到评分阈值'
              : store.stopReason === 'marginal_stagnation'
                ? '边际增益停滞'
                : store.stopReason === 'max_iterations'
                  ? '达到最大迭代轮次'
                  : '人工中止'
          }}
        </span>
      </div>
      <div class="banner-side">
        <span>迭代轮次 <strong>{{ store.iteration }}</strong></span>
        <span>当前环节 <strong>{{ store.status?.current_stage?.stage ?? store.status?.next_stage ?? '—' }}</strong></span>
        <span>模式 <strong>{{ store.status?.mode ?? '—' }}</strong></span>
        <span v-if="store.status?.run">run #{{ store.status.run.id }} · {{ store.status.run.status }}</span>
      </div>
    </div>

    <p v-if="store.status?.stop_conditions" class="thresholds">
      停止条件（来源 {{ store.status.stop_conditions.score_threshold_source }}）：
      max_iterations={{ store.status.stop_conditions.max_iterations }} ·
      score_threshold={{ store.status.stop_conditions.score_threshold }} ·
      marginal_gain={{ store.status.stop_conditions.marginal_gain_threshold }}
      <span v-if="store.status.stop_conditions.is_demo"> · 演示快照阈值</span>
    </p>

    <!-- 六泳道 -->
    <div class="lanes">
      <article
        v-for="lane in lanes"
        :key="lane.stage"
        class="lane"
        :class="`lane--${lane.status}`"
      >
        <header class="lane-head">
          <div class="lane-title">
            <span class="lane-index">{{ STAGE_ORDER.indexOf(lane.stage) + 1 }}</span>
            <strong>{{ lane.label }}</strong>
            <code class="lane-key">{{ lane.stage }}</code>
          </div>
          <span class="lane-status" :class="`lane-status--${lane.status}`">
            {{ STATUS_LABELS[lane.status] }}
          </span>
        </header>

        <div class="lane-meta">
          <span
            >attempt <strong>{{ lane.detail?.attempt ?? 0 }}</strong></span
          >
          <span>耗时 <strong>{{ formatDuration(lane.detail?.duration_ms) }}</strong></span>
          <span>成本 <strong>{{ formatCost(lane.detail?.cost_usd) }}</strong></span>
        </div>

        <div class="lane-tags">
          <span v-if="lane.decisionPoint" class="tag tag--brand">决策点 {{ lane.decisionPoint }}</span>
          <span v-if="lane.node" class="tag tag--info">介入节点 {{ lane.node }}</span>
          <span v-if="!lane.registered" class="tag tag--warn">环节未注册</span>
          <span v-if="lane.detail?.has_output" class="tag tag--ok">有产出</span>
          <span v-else class="tag">无产出</span>
        </div>

        <p v-if="lane.detail?.error" class="lane-error">失败：{{ lane.detail.error }}</p>

        <dl v-if="summarize(lane.detail).length" class="lane-summary">
          <template v-for="row in summarize(lane.detail)" :key="row.label">
            <dt>{{ row.label }}</dt>
            <dd>{{ row.value }}</dd>
          </template>
        </dl>
        <p v-else class="lane-empty">
          {{ lane.status === 'pending' ? '尚未执行（无产出）' : '接口未返回产出内容' }}
        </p>

        <footer class="lane-foot">
          <span>{{ formatTime(lane.detail?.started_at) }} → {{ formatTime(lane.detail?.finished_at) }}</span>
          <span v-if="lane.detail?.stage_output_id">output #{{ lane.detail.stage_output_id }}</span>
        </footer>
      </article>
    </div>

    <p v-if="absentStages.length" class="hint">
      以下环节本次运行暂无记录：{{ absentStages.join('、') }}
    </p>
    <p v-if="registryNote" class="hint hint--warn">{{ registryNote }}</p>

    <!-- 实时日志（SSE 事件流） -->
    <section class="log">
      <header class="log-head">
        <strong>实时事件流</strong>
        <span class="log-state" :class="`log-state--${store.connection}`">
          {{ store.connection === 'open' ? 'SSE 已连接' : store.connection === 'reconnecting' ? 'SSE 重连中' : 'SSE 未连接' }}
        </span>
        <span v-if="store.lastSyncedAt" class="log-hint">快照同步 {{ new Date(store.lastSyncedAt).toLocaleTimeString() }}</span>
      </header>
      <ul v-if="store.eventLog.length" class="log-list scroll-y">
        <li v-for="entry in store.eventLog.slice(0, 30)" :key="entry.seq">
          <span class="log-time">{{ new Date(entry.at).toLocaleTimeString() }}</span>
          <code class="log-name">{{ entry.name }}</code>
          <span class="log-payload">{{ JSON.stringify(entry.payload) }}</span>
        </li>
      </ul>
      <p v-else class="lane-empty">暂无事件（未订阅或尚无事件产生）</p>
    </section>
  </div>
</template>

<style scoped>
.board {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* ---------- 停止原因横幅 ---------- */
.banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-bg-subtle);
}

.banner--stop {
  border-color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.banner--warn {
  border-color: var(--color-brand);
  background-color: var(--color-brand-soft);
}

.banner-main {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.banner-label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.banner-value {
  font-size: var(--font-size-xl);
  color: var(--color-text-primary);
}

.banner-explain {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.banner-side {
  display: flex;
  gap: var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.banner-side strong {
  color: var(--color-text-primary);
}

.thresholds,
.hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.hint--warn {
  color: var(--color-warning);
}

/* ---------- 泳道 ---------- */
.lanes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-3);
}

.lane {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-top: 3px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}

.lane--running {
  border-top-color: var(--color-brand);
}

.lane--done {
  border-top-color: var(--color-success);
}

.lane--waiting_human {
  border-top-color: var(--color-warning);
}

.lane--failed {
  border-top-color: var(--color-danger);
}

.lane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.lane-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.lane-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: var(--radius-pill);
  background-color: var(--color-bg-muted);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.lane-key {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.lane-status {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.lane-status--running {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.lane-status--done {
  border-color: var(--color-success);
  color: var(--color-success);
}

.lane-status--waiting_human {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.lane-status--failed {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.lane-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.lane-meta strong {
  color: var(--color-text-primary);
}

.lane-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.tag {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.tag--brand {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.tag--info {
  border-color: var(--color-info);
  color: var(--color-info);
}

.tag--ok {
  border-color: var(--color-success);
  color: var(--color-success);
}

.tag--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.lane-error {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background-color: var(--color-danger-soft);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}

.lane-summary {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 2px var(--space-2);
  margin: 0;
  font-size: var(--font-size-xs);
}

.lane-summary dt {
  color: var(--color-text-secondary);
}

.lane-summary dd {
  margin: 0;
  color: var(--color-text-primary);
  overflow-wrap: anywhere;
}

.lane-empty {
  margin: 0;
  color: var(--color-text-disabled);
  font-size: var(--font-size-xs);
}

.lane-foot {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  border-top: 1px solid var(--color-border);
  padding-top: var(--space-2);
}

/* ---------- 事件流 ---------- */
.log {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.log-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
  font-size: var(--font-size-xs);
}

.log-state {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
}

.log-state--open {
  border-color: var(--color-success);
  color: var(--color-success);
}

.log-state--reconnecting {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.log-state--error {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.log-hint {
  color: var(--color-text-secondary);
}

.log-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 260px;
  overflow: auto;
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
}

.log-list li {
  display: flex;
  gap: var(--space-2);
  padding: 2px 0;
  border-bottom: 1px dashed var(--color-border);
}

.log-time {
  color: var(--color-text-secondary);
  flex: 0 0 auto;
}

.log-name {
  color: var(--color-brand);
  flex: 0 0 auto;
}

.log-payload {
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}
</style>
