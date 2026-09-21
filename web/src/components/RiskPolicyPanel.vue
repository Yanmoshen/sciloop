<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 风险策略面板（WP15-T3）。
 *
 * - 三态动作可视化：auto_execute / need_human / circuit_break（真库命中分布 + 规则说明）
 * - 三分数值可解释：risk(0-100) / confidence(0-1) / reversibility(0-1) 逐项给出阈值与判定依据
 * - need_human 时提供 批准 / 修改 / 拒绝 与请求原因说明（写操作需 Owner）
 *
 * 规则层拥有最终决定权，LLM 只能给建议分；阈值展示一律带来源。
 */
import { computed, ref } from 'vue'
import { writeDenied } from '@/utils/messages'

import {
  POLICY_ACTION_LABELS,
  type DecisionRecord,
  type InterventionAction,
  type PolicyAction,
} from '@/api/workbench'
import { useSessionStore } from '@/stores/session'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()
const session = useSessionStore()

const note = ref('')
const pendingAction = ref<InterventionAction>('approve')

const ACTIONS: PolicyAction[] = ['auto_execute', 'need_human', 'circuit_break']

const ACTION_RULES: Record<PolicyAction, string> = {
  auto_execute: '硬护栏全部通过 且 risk ≤ 阈值 且 confidence ≥ 阈值 且 reversibility ≥ 阈值',
  need_human: '硬护栏通过，但未满足自动执行条件 → 转人工确认',
  circuit_break: '硬护栏任一失败 / 重试次数耗尽 → 立即熔断并生成失败分析报告',
}

const ACTION_COST: Record<PolicyAction, string> = {
  auto_execute: '成本低、可逆',
  need_human: '需人工把关',
  circuit_break: '止损，不继续消耗',
}

const thresholds = computed(() => store.decisions?.thresholds ?? null)
const limits = computed(() => store.decisions?.guardrail_limits ?? null)
const policy = computed(() => store.status?.risk_policy ?? null)
const counts = computed(() => store.policyActionCounts)
const total = computed(() => store.decisions?.items.length ?? 0)

/** 当前真正需要人处置的决策（优先 status.pending_decision，其次最后一条 need_human） */
const pending = computed<DecisionRecord | null>(() => {
  const items = store.decisions?.items ?? []
  const waiting = [...items].reverse().find((item) => item.policy_action === 'need_human')
  return waiting ?? null
})

/** 最近一条需要人处置的决策三分（用于三数值可解释展示） */
const latest = computed<DecisionRecord | null>(() => {
  if (pending.value) return pending.value
  const items = store.decisions?.items ?? []
  return items.length > 0 ? (items[items.length - 1] ?? null) : null
})

const canWrite = computed(() => session.isOwner || store.ownerWritesAllowed)
const writeBlockReason = computed(() =>
  canWrite.value ? '' : writeDenied('调整风险策略'),
)

const approvedRecently = computed(() =>
  store.interventions.items.filter((item) => item.action === 'approve').length,
)

function decide(item: DecisionRecord | null, action: InterventionAction): void {
  if (!item) return
  void store.resolvePendingDecision(item.id, action, { note: note.value, resume: true })
}

function intervene(node: 'N3' | 'N2' | 'N4', action: InterventionAction): void {
  void store.interveneNode(node, action, {}, { note: note.value, autoResume: true })
}

function scoreLevel(value: number | null | undefined, kind: 'risk' | 'confidence' | 'reversibility'): string {
  const th = thresholds.value
  if (value === null || value === undefined || !th) return 'unknown'
  if (kind === 'risk') {
    if (value <= th.risk_auto_max) return 'ok'
    return value >= 70 ? 'bad' : 'warn'
  }
  if (kind === 'confidence') {
    if (value >= th.confidence_auto_min) return 'ok'
    return value >= (th.confidence_break_min_reserved ?? 0.5) ? 'warn' : 'bad'
  }
  if (value >= th.reversibility_auto_min) return 'ok'
  return value >= 0.4 ? 'warn' : 'bad'
}

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(digits)
}
</script>

<template>
  <div class="panel">
    <!-- 策略挂载状态（不可用时如实披露，不假装有判定） -->
    <p v-if="policy" class="policy-source">
      风险策略：{{ policy.mounted ? '已挂载' : '未挂载' }} · policy_version
      <code>{{ policy.policy_version ?? '—' }}</code> · 来源 <code>{{ policy.source }}</code>
    </p>
    <p v-else class="policy-source">风险策略状态未知（status 接口未返回 risk_policy）</p>

    <!-- 三态动作可视化 -->
    <section class="actions">
      <article
        v-for="action in ACTIONS"
        :key="action"
        class="action"
        :class="[`action--${action}`, { 'action--active': latest?.policy_action === action }]"
      >
        <header>
          <code class="action-name">{{ action }}</code>
          <span class="action-label">{{ POLICY_ACTION_LABELS[action] }}</span>
          <span class="action-count">{{ counts[action] }} 次</span>
        </header>
        <p class="action-rule">{{ ACTION_RULES[action] }}</p>
        <p class="action-cost">{{ ACTION_COST[action] }}</p>
        <div class="action-bar">
          <span class="action-bar-fill" :style="{ width: `${total > 0 ? (counts[action] / total) * 100 : 0}%` }" />
        </div>
      </article>
    </section>

    <p class="rules">
      硬护栏先判：任一失败 → circuit_break（阈值不可覆盖）；重试次数耗尽 → circuit_break。
      <span v-if="limits">
        裁判顺序 {{ limits.judgement_order.join(' → ') }} · 成本硬线 ${{ limits.cost_hard_limit_usd }} ·
        配额 ${{ limits.cost_demo_quota_usd }}（只告警） · 模板白名单 {{ limits.template_whitelist.length }} 项
      </span>
    </p>

    <!-- 三分数值可解释 -->
    <section class="scores-card">
      <header class="scores-head">
        <strong>三分数值可解释</strong>
        <span v-if="latest" class="scores-subject">
          决策 #{{ latest.id }}（{{ latest.decision_point }} · {{ latest.stage ?? '—' }} ·
          {{ latest.policy_action }}）
        </span>
        <span v-else class="scores-subject">暂无决策记录</span>
      </header>

      <div v-if="latest" class="score-list">
        <div class="score-row" :class="`score-row--${scoreLevel(latest.risk_score, 'risk')}`">
          <span class="score-name">risk_score</span>
          <span class="score-num">{{ num(latest.risk_score) }}<small>/100</small></span>
          <span class="score-th">
            自动执行上限 ≤ {{ thresholds?.risk_auto_max ?? '—' }}；
            公式 0.30·cost_exposure + 0.25·blast_radius + 0.20·evidence_gap + 0.15·state_change + 0.10·external_dependency
          </span>
        </div>
        <div class="score-row" :class="`score-row--${scoreLevel(latest.confidence_score, 'confidence')}`">
          <span class="score-name">confidence_score</span>
          <span class="score-num">{{ num(latest.confidence_score) }}<small>/1</small></span>
          <span class="score-th">
            自动执行下限 ≥ {{ thresholds?.confidence_auto_min ?? '—' }}；
            公式 0.40·evidence_coverage + 0.25·review_agreement + 0.20·input_completeness + 0.15·historical_success_rate
          </span>
        </div>
        <div class="score-row" :class="`score-row--${scoreLevel(latest.reversibility_score, 'reversibility')}`">
          <span class="score-name">reversibility_score</span>
          <span class="score-num">{{ num(latest.reversibility_score) }}<small>/1</small></span>
          <span class="score-th">自动执行下限 ≥ {{ thresholds?.reversibility_auto_min ?? '—' }}（动作可否安全回退）</span>
        </div>
      </div>

      <p class="score-why">{{ latest ? latest.rationale : '接口未返回决策记录，无法给出判定依据。' }}</p>
      <p v-if="thresholds" class="score-src">
        阈值来源 {{ thresholds.source }} · policy_version {{ thresholds.policy_version }} ·
        LLM 不可覆盖 {{ thresholds.overridable_by_llm }}
        <span v-if="thresholds.confidence_break_min_note"> · {{ thresholds.confidence_break_min_note }}</span>
      </p>
    </section>

    <!-- need_human 处置入口 -->
    <section class="resolve" :class="{ 'resolve--waiting': store.isWaitingHuman }">
      <header class="resolve-head">
        <strong>人工确认请求</strong>
        <span v-if="pending" class="resolve-subject">
          决策 #{{ pending.id }} · {{ pending.decision_point }} · 节点 {{ pending.node ?? '—' }}
        </span>
        <span v-else class="resolve-subject">当前没有 need_human 决策</span>
        <span v-if="store.status?.pending_decision" class="resolve-subject">
          status.pending_decision：{{ store.status.pending_decision.stage }} ·
          动作域 {{ store.status.pending_decision.options.join('/') }}
        </span>
      </header>

      <p class="resolve-reason">
        <template v-if="pending">
          请求原因：{{ pending.rationale }}
        </template>
        <template v-else-if="store.status?.pending_decision">
          请求原因：{{ store.status.pending_decision.error ?? '等待人工处置（接口未给出 error 字段）' }}
        </template>
        <template v-else>无待处置项；已批准 {{ approvedRecently }} 次（interventions 记录）</template>
      </p>

      <div class="resolve-form">
        <el-input v-model="note" size="small" placeholder="人工备注（写入 interventions.note）" class="note-input" />
        <el-radio-group v-model="pendingAction" size="small">
          <el-radio-button value="approve">批准</el-radio-button>
          <el-radio-button value="modify">修改</el-radio-button>
          <el-radio-button value="reject">拒绝</el-radio-button>
          <el-radio-button value="rerun">重跑</el-radio-button>
          <el-radio-button value="downgrade">降级</el-radio-button>
          <el-radio-button value="abort">中止</el-radio-button>
        </el-radio-group>
        <el-button
          type="primary"
          size="small"
          :disabled="!pending || !canWrite"
          :loading="store.busy === 'approve'"
          @click="decide(pending, pendingAction)"
        >
          提交并续跑
        </el-button>
      </div>

      <p v-if="!canWrite" class="resolve-block">{{ writeBlockReason }}</p>

      <div v-if="pending" class="resolve-quick">
        <span>快捷介入（按节点）：</span>
        <el-button size="small" :disabled="!canWrite" @click="intervene('N3', 'approve')">
          N3 批准实验配置
        </el-button>
        <el-button size="small" :disabled="!canWrite" @click="intervene('N3', 'downgrade')">
          N3 降级重跑
        </el-button>
        <el-button size="small" :disabled="!canWrite" @click="intervene('N2', 'reject')">
          N2 否决方案
        </el-button>
        <el-button size="small" :disabled="!canWrite" @click="intervene('N4', 'approve')">
          N4 确认大纲
        </el-button>
      </div>
    </section>

    <!-- 最近风险策略事件（SSE 实时） -->
    <section class="events">
      <strong>最近 risk_policy 事件（SSE）</strong>
      <ul v-if="store.riskEvents.length">
        <li v-for="(event, index) in store.riskEvents.slice(0, 8)" :key="index">
          <code>{{ new Date(event.at).toLocaleTimeString() }}</code>
          <span class="ev-action" :class="`ev-action--${event.action}`">{{ event.action }}</span>
          risk={{ num(event.risk) }} · confidence={{ num(event.confidence) }} · reversibility={{ num(event.reversibility) }}
          <span v-if="event.decision_point"> · {{ event.decision_point }}</span>
          <span v-if="event.decision_id"> · 决策 #{{ event.decision_id }}</span>
        </li>
      </ul>
      <p v-else class="events-empty">暂无事件（未订阅或本次会话尚无风险策略事件）</p>
    </section>

    <p v-if="store.circuitBreak" class="circuit">
      熔断事件：{{ store.circuitBreak.reason }} · 失败报告
      <a :href="store.circuitBreak.report_url">{{ store.circuitBreak.report_url }}</a>
    </p>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.policy-source,
.rules,
.score-src,
.resolve-hint,
.resolve-block {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.degrade {
  color: var(--color-warning);
}

.actions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-3);
}

.action {
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-left: 4px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
}

.action--active {
  box-shadow: var(--shadow-popover);
}

.action--auto_execute {
  border-left-color: var(--color-success);
}

.action--need_human {
  border-left-color: var(--color-warning);
}

.action--circuit_break {
  border-left-color: var(--color-danger);
}

.action--auto_execute.action--active {
  background-color: var(--color-success-soft);
}

.action--need_human.action--active {
  background-color: var(--color-warning-soft);
}

.action--circuit_break.action--active {
  background-color: var(--color-danger-soft);
}

.action header {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.action-name {
  font-weight: 700;
  color: var(--color-text-primary);
}

.action-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.action-count {
  margin-left: auto;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.action-rule,
.action-cost {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.action-bar {
  height: 6px;
  margin-top: var(--space-2);
  border-radius: var(--radius-pill);
  background-color: var(--color-bg-muted);
  overflow: hidden;
}

.action-bar-fill {
  display: block;
  height: 100%;
  background-color: var(--color-brand);
}

.scores-card {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.scores-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
  font-size: var(--font-size-sm);
}

.scores-subject {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.score-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.score-row {
  display: grid;
  grid-template-columns: 130px 90px 1fr;
  align-items: baseline;
  gap: var(--space-3);
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
}

.score-row--ok {
  border-left: 4px solid var(--color-success);
}

.score-row--warn {
  border-left: 4px solid var(--color-warning);
}

.score-row--bad {
  border-left: 4px solid var(--color-danger);
}

.score-row--unknown {
  border-left: 4px solid var(--color-border-strong);
}

.score-name {
  font-family: var(--font-family-mono);
  color: var(--color-text-secondary);
}

.score-num {
  font-size: var(--font-size-lg);
  color: var(--color-text-primary);
}

.score-num small {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.score-th {
  color: var(--color-text-secondary);
}

.score-why {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-muted);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.resolve {
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-bg-subtle);
}

.resolve--waiting {
  border-color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.resolve-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
  font-size: var(--font-size-sm);
}

.resolve-subject {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.resolve-reason {
  margin: var(--space-2) 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  overflow-wrap: anywhere;
}

.resolve-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.note-input {
  width: 260px;
}

.resolve-block {
  color: var(--color-warning);
}

.resolve-quick {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.events {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  font-size: var(--font-size-xs);
}

.events ul {
  list-style: none;
  margin: var(--space-2) 0 0;
  padding: 0;
  font-family: var(--font-family-mono);
}

.events li {
  padding: 2px 0;
  border-bottom: 1px dashed var(--color-border);
  color: var(--color-text-secondary);
}

.ev-action--auto_execute {
  color: var(--color-success);
}

.ev-action--need_human {
  color: var(--color-warning);
}

.ev-action--circuit_break {
  color: var(--color-danger);
}

.events-empty {
  margin: var(--space-2) 0 0;
  color: var(--color-text-disabled);
}

.circuit {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background-color: var(--color-danger-soft);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}
</style>
