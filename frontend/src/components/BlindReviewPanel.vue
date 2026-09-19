<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 盲评校准面板（WP15-T6）。
 *
 * 匿名性红线（contracts.blind_review_rules）：
 * - **不泄漏评审来源与原始顺序**：界面只展示「生成模型与评审模型不同」这一**事实**，
 *   绝不渲染 generator_model_ref / reviewer_model_ref 原文，也不展示原始 method_index
 * - 候选只以匿名别名（A/B/C/D）+ 服务端 shuffle 后的位次展示；shuffle_seed 作为可复现审计信息展示
 * - 一致率必须带 sample_size，小样本固定显示「不构成统计显著结论」（不允许宣称显著）
 *
 * 数据来源：GET /pipelines/{project_id}/review-calibration（WP12，已挂载）
 * 人工标签录入：POST /pipelines/{project_id}/human-labels（Owner，≥3 条起算）
 */
import { computed, ref } from 'vue'

import type { HumanLabelEntry } from '@/api/workbench'
import { useSessionStore } from '@/stores/session'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()
const session = useSessionStore()

type DimensionKey = 'novelty' | 'feasibility' | 'rigor' | 'cost_reasonableness' | 'risk_control'

const DIMENSIONS: Array<{ key: DimensionKey; label: string }> = [
  { key: 'novelty', label: 'novelty 新颖性' },
  { key: 'feasibility', label: 'feasibility 可行性' },
  { key: 'rigor', label: 'rigor 严谨性' },
  { key: 'cost_reasonableness', label: 'cost_reasonableness 成本合理性' },
  { key: 'risk_control', label: 'risk_control 风险可控性' },
]

const report = computed(() => store.calibration)
const canWrite = computed(() => session.isOwner || store.ownerWritesAllowed)

/** 隔离事实：只输出布尔与一句结论，不渲染模型 ref 原文 */
const isolation = computed(() => {
  const value = report.value
  if (!value || !value.generator_model_ref || !value.reviewer_model_ref) return null
  const isolated = value.generator_model_ref !== value.reviewer_model_ref
  return {
    isolated,
    statement: isolated
      ? '生成模型与评审模型不同（隔离成立）；两者标识均不进入评审输入，界面亦不展示原文。'
      : '生成模型与评审模型相同：按红线 plan_review 环节必须直接失败，禁止静默回退同一模型。',
  }
})

/** 匿名候选表：别名 + shuffle 位次 + 五维 + 合计（不显示原始下标） */
const candidates = computed(() => {
  const value = report.value
  if (!value) return []
  const positionByAlias = new Map<string, number>()
  for (const order of value.candidate_order ?? []) positionByAlias.set(order.alias, order.position)
  return (value.model_scores ?? [])
    .map((score) => ({
      alias: score.alias,
      position: positionByAlias.get(score.alias) ?? null,
      selected: score.selected,
      total: score.total,
      comments: score.comments ?? [],
      scores: DIMENSIONS.map((dimension) => ({
        key: dimension.key,
        label: dimension.label,
        value: score[dimension.key] ?? null,
      })),
    }))
    .sort((a, b) => (a.position ?? 99) - (b.position ?? 99))
})

const selectedAlias = computed(() => candidates.value.find((candidate) => candidate.selected)?.alias ?? null)

const concerns = computed(() => candidates.value.flatMap((candidate) => candidate.comments.map((text) => ({ alias: candidate.alias, text }))))

const disclosure = computed(() => report.value?.disclosure ?? null)
const smallSample = computed(() => disclosure.value?.is_small_sample !== false)

const metricLabel = computed(() => {
  const metric = report.value?.agreement_metric
  if (metric === 'cohen_kappa') return 'Cohen κ（分类一致率）'
  if (metric === 'mae') return 'MAE（连续分平均绝对误差）'
  return '—'
})

/* ---------------- 人工标签录入 ---------------- */
const draftLabel = ref<{
  alias: string
  decision: 'approve' | 'revise' | 'reject'
  scores: Partial<Record<DimensionKey, number>>
}>({ alias: '', decision: 'approve', scores: {} })
const labelNote = ref('')
const pendingLabels = ref<HumanLabelEntry[]>([])

function addLabel(): void {
  if (!draftLabel.value.alias) return
  const scores: Partial<Record<DimensionKey, number>> = {}
  for (const dimension of DIMENSIONS) {
    const value = draftLabel.value.scores[dimension.key]
    if (typeof value === 'number' && !Number.isNaN(value)) {
      scores[dimension.key] = value
    }
 }
  pendingLabels.value = [
    ...pendingLabels.value,
    {
      candidate_alias: draftLabel.value.alias,
      decision: draftLabel.value.decision,
      scores: Object.keys(scores).length ? scores : undefined,
      note: labelNote.value || null,
    },
  ]
  draftLabel.value = { alias: '', decision: 'approve', scores: {} }
  labelNote.value = ''
}

async function submitLabels(): Promise<void> {
  const count = pendingLabels.value.length
  if (count === 0) return
  const ok = await store.submitLabels(pendingLabels.value)
  if (ok) pendingLabels.value = []
}

function removeLabel(index: number): void {
  pendingLabels.value = pendingLabels.value.filter((_, i) => i !== index)
}

function valueOf(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : String(value)
}
</script>

<template>
  <div class="panel">
    <!-- 匿名性声明 -->
    <section class="anon">
      <div class="anon-row">
        <strong>匿名化</strong>
        <span>
          版本 {{ report?.anonymization_version ?? '—' }} · shuffle_seed
          <code>{{ report?.shuffle_seed ?? '—' }}</code>（固定种子，顺序可复现）
        </span>
        <span class="anon-flag">原始下标映射与模型标识仅服务端保存，不进入评审输入，界面不展示</span>
      </div>
      <div v-if="isolation" class="anon-row" :class="isolation.isolated ? 'anon-row--ok' : 'anon-row--bad'">
        <strong>隔离校验</strong>
        <span>{{ isolation.statement }}</span>
      </div>
      <div v-else class="anon-row">
        <strong>隔离校验</strong>
        <span>接口未返回隔离信息（仅展示「两者是否不同」的事实，不展示标识原文）</span>
      </div>
    </section>

    <!-- 未就绪 / 空 -->
    <el-alert v-if="report && report.status !== 'calibrated'" type="info" :closable="false" show-icon>
      <template #title>
        盲评尚未完成：状态 {{ report.status }} · 样本量 {{ report.sample_size }}（人工标签 ≥
        {{ report.human_label_min ?? 3 }} 条起算）
      </template>
      <template #default>
        <p class="alert-text">{{ report.note ?? '（接口未给出说明）' }}</p>
      </template>
    </el-alert>

    <!-- 匿名候选与五维打分 -->
    <section v-if="candidates.length" class="card">
      <header class="card-head">
        <strong>匿名候选（服务端 shuffle 顺序）</strong>
      </header>
      <table class="table">
        <thead>
          <tr>
            <th>位次</th>
            <th>别名</th>
            <th v-for="dimension in DIMENSIONS" :key="String(dimension.key)">{{ dimension.label }}</th>
            <th>total</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="candidate in candidates" :key="candidate.alias" :class="{ 'row--selected': candidate.selected }">
            <td>{{ candidate.position === null ? '—' : candidate.position + 1 }}</td>
            <td class="alias">{{ candidate.alias }}</td>
            <td v-for="score in candidate.scores" :key="score.key">{{ valueOf(score.value) }}</td>
            <td class="total">{{ valueOf(candidate.total) }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- concerns -->
    <section v-if="concerns.length" class="card">
      <header class="card-head"><strong>评审 concerns</strong></header>
      <ul class="concerns">
        <li v-for="(item, index) in concerns" :key="index">
          <span class="alias">{{ item.alias }}</span>
          <span>{{ item.text }}</span>
        </li>
      </ul>
    </section>

    <!-- 校准区 -->
    <section class="card">
      <header class="card-head">
        <strong>人工标签一致率</strong>
      </header>

      <div class="calib">
        <div class="calib-item">
          <span class="calib-label">一致率</span>
          <strong class="calib-value">
            {{ report?.agreement_value === null || report?.agreement_value === undefined ? '—' : report.agreement_value }}
          </strong>
        </div>
        <div class="calib-item">
          <span class="calib-label">样本量 sample_size</span>
          <strong class="calib-value">{{ report?.sample_size ?? 0 }}</strong>
        </div>
        <div class="calib-item">
          <span class="calib-label">置信区间</span>
          <strong class="calib-value">
            {{
              disclosure
                ? disclosure.is_small_sample
                  ? '不适用（小样本）'
                  : '见接口 confidence_interval'
                : '—'
            }}
          </strong>
        </div>
        <div class="calib-item">
          <span class="calib-label">小样本阈值</span>
          <strong class="calib-value">{{ report?.small_sample_threshold ?? '—' }}</strong>
        </div>
      </div>

      <p class="disclosure" :class="{ 'disclosure--warn': smallSample }">
        {{
          disclosure?.statement ??
            `样本量 ${report?.sample_size ?? 0}：一致率仅供参考，不等同于评审正确率；小样本不得宣称统计显著。`
        }}
      </p>
    </section>

    <!-- 人工标签录入 -->
    <section class="card">
      <header class="card-head">
        <strong>人工标签录入</strong>
      </header>

      <div class="entry">
        <el-select v-model="draftLabel.alias" size="small" placeholder="候选别名" class="alias-select">
          <el-option v-for="candidate in candidates" :key="candidate.alias" :label="candidate.alias" :value="candidate.alias" />
        </el-select>
        <el-radio-group v-model="draftLabel.decision" size="small">
          <el-radio-button value="approve">approve</el-radio-button>
          <el-radio-button value="revise">revise</el-radio-button>
          <el-radio-button value="reject">reject</el-radio-button>
        </el-radio-group>
        <el-input v-model="labelNote" size="small" placeholder="备注" class="note" />
      </div>

      <div class="dims">
        <label v-for="dimension in DIMENSIONS" :key="dimension.key" class="dim">
          <span>{{ dimension.label }}</span>
          <el-input-number
            v-model="draftLabel.scores[dimension.key]"
            size="small"
            :min="0"
            :max="20"
            controls-position="right"
          />
        </label>
      </div>

      <div class="entry-actions">
        <el-button size="small" :disabled="!draftLabel.alias" @click="addLabel">加入待提交</el-button>
        <el-button
          type="primary"
          size="small"
          :disabled="!canWrite || pendingLabels.length === 0"
          :loading="store.busy === 'human-labels'"
          @click="submitLabels"
        >
          提交 {{ pendingLabels.length }} 条标签
        </el-button>
        <span v-if="!canWrite" class="hint hint--warn">public_demo 面禁止写操作（需 Owner 令牌）</span>
      </div>

      <ul v-if="pendingLabels.length" class="pending">
        <li v-for="(label, index) in pendingLabels" :key="index">
          <span class="alias">{{ label.candidate_alias }}</span>
          <span>{{ label.decision }}</span>
          <span v-if="label.scores" class="mono">{{ JSON.stringify(label.scores) }}</span>
          <el-button size="small" text @click="removeLabel(index)">移除</el-button>
        </li>
      </ul>

      <el-table v-if="report?.human_labels?.length" :data="report.human_labels" size="small" border>
        <el-table-column label="候选" width="80">
          <template #default="{ row }">{{ row.candidate_alias ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="判定" width="100">
          <template #default="{ row }">{{ row.decision }}</template>
        </el-table-column>
        <el-table-column label="五维" min-width="220">
          <template #default="{ row }">
            <code v-if="row.scores">{{ JSON.stringify(row.scores) }}</code>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="合计" width="80">
          <template #default="{ row }">{{ row.total ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="备注" min-width="160">
          <template #default="{ row }">{{ row.note ?? '—' }}</template>
        </el-table-column>
      </el-table>
    </section>

    <!-- 逐候选配对（可追溯，不含模型标识） -->
    <section v-if="report?.pairs?.length" class="card">
      <header class="card-head"><strong>模型 × 人工 逐候选配对</strong></header>
      <table class="table">
        <thead>
          <tr>
            <th>别名</th>
            <th>模型总分</th>
            <th>人工总分</th>
            <th>模型选中</th>
            <th>人工判定</th>
            <th>是否一致</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="pair in report.pairs" :key="pair.alias">
            <td class="alias">{{ pair.alias }}</td>
            <td>{{ valueOf(pair.model_total) }}</td>
            <td>{{ valueOf(pair.human_total) }}</td>
            <td>{{ pair.model_selected ? '是' : '否' }}</td>
            <td>{{ pair.human_decision }}</td>
            <td :class="pair.agree ? 'agree' : 'disagree'">{{ pair.agree ? '一致' : '不一致' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.anon {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.anon-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.anon-row--ok {
  color: var(--color-success);
}

.anon-row--bad {
  color: var(--color-danger);
}

.anon-flag {
  color: var(--color-text-secondary);
}

.card {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
  font-size: var(--font-size-sm);
}



.alert-text {
  margin: 0;
  font-size: var(--font-size-xs);
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.table th,
.table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  text-align: left;
}

.table th {
  color: var(--color-text-secondary);
  font-weight: 400;
}

.row--selected {
  background-color: var(--color-success-soft);
}

.alias {
  font-family: var(--font-family-mono);
  color: var(--color-brand);
}

.total {
  font-weight: 700;
}

.concerns {
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--font-size-xs);
}

.calib {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
}

.calib-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-subtle);
}

.calib-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.calib-value {
  font-size: var(--font-size-lg);
  color: var(--color-text-primary);
}


.disclosure {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-muted);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.disclosure--warn {
  background-color: var(--color-warning-soft);
  color: var(--color-warning);
}

.entry,
.entry-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.alias-select {
  width: 120px;
}

.note {
  width: 200px;
}

.dims {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-2);
  margin: var(--space-2) 0;
}

.dim {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.pending {
  list-style: none;
  margin: var(--space-2) 0 0;
  padding: 0;
  font-size: var(--font-size-xs);
}

.pending li {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 2px 0;
  border-bottom: 1px dashed var(--color-border);
}


.agree {
  color: var(--color-success);
}

.disagree {
  color: var(--color-danger);
}

.mono {
  font-family: var(--font-family-mono);
}
</style>
