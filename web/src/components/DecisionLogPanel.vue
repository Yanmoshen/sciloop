<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 决策日志面板（WP15-T4）。
 *
 * 按 D1–D6 分组，每条展示 options_considered / chosen / rationale / guardrail_checks /
 * risk_score / confidence_score / reversibility_score / policy_action / cost_usd / policy_version，
 * 并支持展开**特征明细**（权重 / 归一值 / 来源 / 缺失项）。
 *
 * 「特征明细」的真实来源：POST /decisions/{id}/evaluate-risk 的 result.features
 * （落库的 guardrail_checks 只保留护栏与缺失项，不含逐特征权重）。
 * 本组件只调 store action，不直接发请求。
 */
import { computed, ref } from 'vue'

import {
  POLICY_ACTION_LABELS,
  type DecisionRecord,
  type EvaluateRiskResponse,
  type PolicyAction,
  type RiskFeatureDetail,
} from '@/api/workbench'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()
const expanded = ref<number[]>([])

const D_NAME: Record<string, string> = {
  D1: '检索策略（survey）',
  D2: '方案选型（plan_review）',
  D3: '实验配置（experiment）',
  D4: '失败处置（任意环节）',
  D5: '迭代判据（review）',
  D6: '写作结构（writing）',
}

const ACTION_CLASS: Record<PolicyAction, string> = {
  auto_execute: 'action--auto',
  need_human: 'action--human',
  circuit_break: 'action--break',
}

const groups = computed(() =>
  Object.entries(store.decisionGroups).map(([id, items]) => ({
    id,
    name: D_NAME[id] ?? id,
    items,
    policyCounts: Object.entries(
      items.reduce<Record<string, number>>((acc, item) => {
        acc[item.policy_action] = (acc[item.policy_action] ?? 0) + 1
        return acc
      }, {}),
    ).map(([action, count]) => ({
      action,
      count,
      label: POLICY_ACTION_LABELS[action as PolicyAction] ?? action,
      cls: ACTION_CLASS[action as PolicyAction] ?? '',
    })),
  })),
)

const total = computed(() => store.decisions?.total ?? store.decisions?.items.length ?? 0)
const thresholds = computed(() => store.decisions?.thresholds ?? null)
const limits = computed(() => store.decisions?.guardrail_limits ?? null)
const policyVersion = computed(() => store.decisions?.policy_version ?? '（接口未返回 policy_version）')

function guardrails(item: DecisionRecord): Record<string, unknown> {
  return (item.guardrail_checks ?? {}) as Record<string, unknown>
}

function optionsOf(item: DecisionRecord): string[] {
  return Array.isArray(item.options_considered) ? item.options_considered : []
}

function triState(value: unknown): string {
  if (value === true) return '通过'
  if (value === false) return '失败'
  return '未评估'
}

function triClass(value: unknown): string {
  if (value === true) return 'ok'
  if (value === false) return 'bad'
  return 'unknown'
}

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(digits)
}

function actionClass(action: PolicyAction | string): string {
  if (action === 'auto_execute') return 'action--auto'
  if (action === 'need_human') return 'action--human'
  return 'action--break'
}

/** 三分数值可解释：说明判定依据（阈值来源逐条给出，不给黑箱总分） */
function explain(item: DecisionRecord): string {
  const th = thresholds.value
  if (!th) return '阈值未知（决策列表接口未返回 thresholds）'
  const parts: string[] = []
  if (item.policy_action === 'auto_execute') {
    parts.push(
      `命中规则：risk(${num(item.risk_score)}) ≤ ${th.risk_auto_max} 且 confidence(${num(item.confidence_score)}) ≥ ${th.confidence_auto_min} 且 reversibility(${num(item.reversibility_score)}) ≥ ${th.reversibility_auto_min}`,
    )
  } else if (item.policy_action === 'need_human') {
    parts.push(
      `硬护栏通过但未满足自动执行：需 risk ≤ ${th.risk_auto_max} / confidence ≥ ${th.confidence_auto_min} / reversibility ≥ ${th.reversibility_auto_min}`,
    )
  } else {
    parts.push(
      '熔断：硬护栏任一失败、或 risk ≥ 人工上限、或重试次数耗尽（阈值不可被覆盖）',
    )
  }
  parts.push(`阈值来源 ${th.source}；LLM 不可覆盖阈值（overridable_by_llm=${String(th.overridable_by_llm)}）`)
  return parts.join('；')
}

/** 某条决策最近一次重算的特征明细（按 decision_id 匹配，避免串台） */
function evaluated(item: DecisionRecord): EvaluateRiskResponse | null {
  const last = store.lastEvaluate
  return last && last.decision_id === item.id ? last : null
}

function featureRows(item: DecisionRecord, kind: 'risk' | 'confidence' | 'reversibility'): Array<{ key: string } & RiskFeatureDetail> {
  const result = evaluated(item)?.result
  const block = result?.features?.[kind] as { features?: Record<string, RiskFeatureDetail> } | undefined
  const source = block?.features
  if (!source) return []
  return Object.entries(source).map(([key, value]) => ({ key, ...value }))
}

const reusing = computed(() => Boolean(store.lastEvaluate))
</script>
<template>
  <div class="panel">
    <!-- 阈值与护栏来源：禁止只给结论不给理由 -->
    <section class="meta">
      <div class="meta-card">
        <h3>策略阈值</h3>
        <p v-if="thresholds" class="meta-line">
          risk ≤ <strong>{{ thresholds.risk_auto_max }}</strong> · confidence ≥
          <strong>{{ thresholds.confidence_auto_min }}</strong> · reversibility ≥
          <strong>{{ thresholds.reversibility_auto_min }}</strong>
        </p>
        <p v-else class="meta-line">阈值来源未知（决策列表接口未返回 thresholds）</p>
        <p class="meta-src">
          policy_version <code>{{ policyVersion }}</code>
          <span v-if="thresholds"> · {{ thresholds.source }}</span>
        </p>
        <p v-if="thresholds" class="meta-src">
          LLM 只能给建议分，不能覆盖规则结论（overridable_by_llm={{ thresholds.overridable_by_llm }}）
        </p>
      </div>

      <div class="meta-card">
        <h3>硬护栏</h3>
        <p v-if="limits" class="meta-line">
          成本硬线 <strong>${{ limits.cost_hard_limit_usd }}</strong> · 演示配额
          <strong>${{ limits.cost_demo_quota_usd }}</strong>（配额只告警不熔断）
        </p>
        <p v-if="limits" class="meta-line">
          裁判顺序 {{ limits.judgement_order.join(' → ') }} · sample_size ≤ {{ limits.sample_size_max }}
        </p>
        <p v-if="limits" class="meta-src">
          {{ limits.version }} · 阈值不可覆盖 {{ limits.thresholds_overridable }} · 模板白名单
          {{ limits.template_whitelist.length }} 项
        </p>
      </div>

      <div class="meta-card">
        <h3>记录来源</h3>
        <p class="meta-line">共 <strong>{{ total }}</strong> 条决策记录</p>
        <p class="meta-src">{{ store.decisions?.source ?? '—' }}</p>
        <p v-if="store.decisions?.note" class="meta-src">接口说明：{{ store.decisions.note }}</p>
      </div>
    </section>

    <p v-if="reusing" class="notice">
      已重算特征明细：决策 #{{ store.lastEvaluate?.decision_id }} ·
      {{ store.lastEvaluate?.changed ? '结论已变化' : '结论未变化' }}
      （原 {{ store.lastEvaluate?.original.policy_action }} → 重算
      {{ store.lastEvaluate?.recomputed.policy_action }}，persisted={{ store.lastEvaluate?.persisted }}）
    </p>

    <!-- D1–D6 分组 -->
    <el-collapse v-model="expanded" class="groups">
      <el-collapse-item v-for="group in groups" :key="group.id" :name="group.id">
        <template #title>
          <span class="group-title">
            <code class="group-id">{{ group.id }}</code>
            <span>{{ group.name }}</span>
            <span class="group-count">{{ group.items.length }} 条</span>
            <span v-for="entry in group.policyCounts" :key="entry.action" class="chip" :class="entry.cls">
              {{ entry.label }} × {{ entry.count }}
            </span>
          </span>
        </template>

        <p v-if="group.items.length === 0" class="empty">该决策点暂无记录</p>

        <article v-for="item in group.items" :key="item.id" class="record">
          <header class="record-head">
            <span class="chip" :class="actionClass(item.policy_action)">
              {{ POLICY_ACTION_LABELS[item.policy_action] ?? item.policy_action }}
            </span>
            <strong class="record-chosen">{{ item.chosen }}</strong>
            <span class="record-id">#{{ item.id }}</span>
            <span v-if="item.node" class="record-node">节点 {{ item.node }}</span>
            <span class="record-stage">{{ item.stage ?? '—' }}</span>
            <span class="record-time">{{ item.created_at ? new Date(item.created_at).toLocaleString() : '—' }}</span>
          </header>

          <div class="scores">
            <div class="score">
              <span class="score-label">risk</span>
              <strong class="score-value">{{ num(item.risk_score) }}</strong>
              <span class="score-range">/100</span>
            </div>
            <div class="score">
              <span class="score-label">confidence</span>
              <strong class="score-value">{{ num(item.confidence_score) }}</strong>
            </div>
            <div class="score">
              <span class="score-label">reversibility</span>
              <strong class="score-value">{{ num(item.reversibility_score) }}</strong>
            </div>
            <div class="score">
              <span class="score-label">cost</span>
              <strong class="score-value">${{ num(item.cost_usd, 4) }}</strong>
            </div>
            <div class="score">
              <span class="score-label">policy_version</span>
              <strong class="score-value score-value--sm">{{ item.policy_version }}</strong>
            </div>
          </div>

          <p class="explain">{{ explain(item) }}</p>

          <details class="detail">
            <summary>展开明细（选项 / 理由 / 护栏检查 / 上下文摘要）</summary>

            <div class="detail-grid">
              <div>
                <h4>options_considered</h4>
                <ul class="options">
                  <li
                    v-for="opt in optionsOf(item)"
                    :key="opt"
                    :class="{ 'options--chosen': opt === item.chosen }"
                  >
                    {{ opt }}
                  </li>
                </ul>
              </div>
              <div>
                <h4>chosen</h4>
                <p class="mono">{{ item.chosen }}</p>
                <h4>rationale</h4>
                <p class="rationale">{{ item.rationale }}</p>
              </div>
            </div>

            <h4>guardrail_checks</h4>
            <div class="guardrails">
              <span class="guardrail" :class="`guardrail--${triClass(guardrails(item).safety_ok)}`">
                safety_ok：{{ triState(guardrails(item).safety_ok) }}
              </span>
              <span class="guardrail" :class="`guardrail--${triClass(guardrails(item).time_ok)}`">
                time_ok：{{ triState(guardrails(item).time_ok) }}
              </span>
              <span class="guardrail" :class="`guardrail--${triClass(guardrails(item).cost_ok)}`">
                cost_ok：{{ triState(guardrails(item).cost_ok) }}
              </span>
              <span class="guardrail guardrail--unknown">
                短路：{{ String(guardrails(item).short_circuited ?? '未提供') }}
              </span>
              <span class="guardrail guardrail--unknown">
                重试耗尽：{{ String(guardrails(item).retry_exhausted ?? '未提供') }}
              </span>
              <span class="guardrail guardrail--unknown">
                policy_mounted：{{ String(guardrails(item).policy_mounted ?? '未提供') }}
              </span>
            </div>

            <p v-if="Array.isArray(guardrails(item).missing_features)" class="missing">
              缺失特征（按剩余权重归一，不编造数值）：
              {{ (guardrails(item).missing_features as string[]).join('、') || '无' }}
            </p>
            <p v-if="guardrails(item).guardrail_first_failure" class="missing">
              首个失败护栏：{{ guardrails(item).guardrail_first_failure }}
            </p>

            <h4>context_digest</h4>
            <p class="mono digest">{{ item.context_digest }}</p>

            <h4>原始 guardrail_checks（JSON）</h4>
            <pre class="mono json scroll-y">{{ JSON.stringify(item.guardrail_checks, null, 1) }}</pre>
          </details>

          <!-- 特征明细：来自 POST /decisions/{id}/evaluate-risk（落库记录不含逐特征权重） -->
          <div class="features">
            <el-button
              size="small"
              :loading="store.busy === 'evaluate-risk'"
              @click="store.recomputeRisk(item.id)"
            >
              重算风险三分并展开特征明细
            </el-button>
          </div>

          <template v-if="evaluated(item)">
            <div class="feature-block">
              <h4>risk 特征（权重 0.30 / 0.25 / 0.20 / 0.15 / 0.10）</h4>
              <table class="feature-table">
                <thead>
                  <tr>
                    <th>特征</th>
                    <th>raw</th>
                    <th>normalized</th>
                    <th>weight</th>
                    <th>来源</th>
                    <th>缺失</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in featureRows(item, 'risk')" :key="row.key">
                    <td>{{ row.key }}</td>
                    <td>{{ row.raw === null ? 'null' : num(row.raw) }}</td>
                    <td>{{ row.normalized === null ? 'null' : num(row.normalized) }}</td>
                    <td>{{ row.weight }}</td>
                    <td class="mono src">{{ row.source }}</td>
                    <td>{{ row.missing ? '是' : '否' }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-if="featureRows(item, 'risk').length === 0" class="empty">本次重算未返回 risk 特征明细</p>
            </div>

            <div class="feature-block">
              <h4>confidence 特征</h4>
              <table class="feature-table">
                <thead>
                  <tr>
                    <th>特征</th>
                    <th>raw</th>
                    <th>normalized</th>
                    <th>weight</th>
                    <th>来源</th>
                    <th>缺失</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in featureRows(item, 'confidence')" :key="row.key">
                    <td>{{ row.key }}</td>
                    <td>{{ row.raw === null ? 'null' : num(row.raw) }}</td>
                    <td>{{ row.normalized === null ? 'null' : num(row.normalized) }}</td>
                    <td>{{ row.weight }}</td>
                    <td class="mono src">{{ row.source }}</td>
                    <td>{{ row.missing ? '是' : '否' }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-if="featureRows(item, 'confidence').length === 0" class="empty">
                本次重算未返回 confidence 特征明细
              </p>
            </div>
          </template>
        </article>
      </el-collapse-item>
    </el-collapse>

    <el-empty v-if="total === 0" description="该项目暂无决策日志记录" />
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--space-3);
}

.meta-card {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.meta-card h3 {
  margin: 0 0 var(--space-2);
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.meta-line {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.meta-src {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.notice {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background-color: var(--color-brand-soft);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}

.group-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-sm);
}

.group-id {
  color: var(--color-brand);
  font-weight: 700;
}

.group-count {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.chip {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.action--auto {
  border-color: var(--color-success);
  color: var(--color-success);
}

.action--human {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.action--break {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.record {
  margin-bottom: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
}

.record-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.record-chosen {
  color: var(--color-text-primary);
  font-size: var(--font-size-md);
}

.record-id,
.record-node {
  font-family: var(--font-family-mono);
}

.scores {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin: var(--space-2) 0;
}

.score {
  display: flex;
  align-items: baseline;
  gap: 2px;
}

.score-label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.score-value {
  font-size: var(--font-size-lg);
  color: var(--color-text-primary);
}

.score-value--sm {
  font-size: var(--font-size-xs);
  font-family: var(--font-family-mono);
}

.score-range {
  color: var(--color-text-disabled);
  font-size: var(--font-size-xs);
}

.explain {
  margin: 0 0 var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.detail summary {
  cursor: pointer;
  font-size: var(--font-size-xs);
  color: var(--color-brand);
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.detail h4 {
  margin: var(--space-3) 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.options {
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--font-size-xs);
}

.options--chosen {
  color: var(--color-success);
  font-weight: 700;
}

.rationale,
.digest {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  overflow-wrap: anywhere;
}

.guardrails {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.guardrail {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
}

.guardrail--ok {
  border-color: var(--color-success);
  color: var(--color-success);
}

.guardrail--bad {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.guardrail--unknown {
  color: var(--color-text-secondary);
}

.missing {
  margin: var(--space-2) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-warning);
}

.json {
  max-height: 260px;
  overflow: auto;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-muted);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.features {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}


.feature-block {
  margin-top: var(--space-3);
}

.feature-block h4 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.feature-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.feature-table th,
.feature-table td {
  padding: 2px var(--space-2);
  border-bottom: 1px solid var(--color-border);
  text-align: left;
}

.feature-table th {
  color: var(--color-text-secondary);
  font-weight: 400;
}

.src {
  color: var(--color-text-secondary);
}

.empty {
  margin: 0;
  color: var(--color-text-disabled);
  font-size: var(--font-size-xs);
}
</style>
