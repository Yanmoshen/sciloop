<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Passport 查看与复现（WP15-T5）。
 *
 * 数据来源：GET /experiments/runs/{run_id}/passport（WP11）。
 * 未挂载时显示「等待 WP11 交付」空状态，**不伪造任何 Passport 字段**。
 *
 * - 展示不可变凭证全字段（数据集版本与 SHA-256 / 模型与供应商 / Prompt 版本与 SHA /
 *   生成参数 / 代码 commit / 依赖锁 SHA / 指标 / 成本 / is_replay / 产物清单 / status）
 * - replay：public_demo 面唯一允许的写操作（有速率限制）
 * - rerun：仅 Owner；非 Owner 时按钮禁用并说明，后端亦会 403
 * - 回放/重跑后展示与父 Passport 的差异报告（指标差 / 成本差 / 耗时差 / verdict）
 */
import { computed } from 'vue'

import { useSessionStore } from '@/stores/session'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()
const session = useSessionStore()

const canWrite = computed(() => session.isOwner || store.ownerWritesAllowed)
const passport = computed(() => (store.passport?.available ? store.passport.data : null))
const unavailable = computed(() => (store.passport && !store.passport.available ? store.passport : null))

function short(value: string | null | undefined, size = 16): string {
  if (!value) return '—'
  return value.length > size ? `${value.slice(0, size)}…` : value
}

function json(value: unknown): string {
  if (value === null || value === undefined) return '—'
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

const fields = computed(() => {
  const passportValue = passport.value
  if (!passportValue) return []
  return [
    { label: 'passport_id', value: String(passportValue.id), mono: true },
    { label: 'status', value: String(passportValue.status), warn: passportValue.status !== 'complete' },
    { label: 'is_replay', value: String(passportValue.is_replay ?? false), warn: passportValue.is_replay === true },
    { label: 'parent_passport_id', value: passportValue.parent_passport_id ? `#${passportValue.parent_passport_id}` : '—（父凭证）', mono: true },
    { label: 'dataset_name', value: passportValue.dataset_name ?? '—' },
    { label: 'dataset_version', value: passportValue.dataset_version ?? '—' },
    { label: 'dataset_sha256', value: short(passportValue.dataset_sha256, 24), mono: true },
    { label: 'sample_manifest', value: json(passportValue.sample_manifest) },
    { label: 'provider', value: passportValue.provider ?? '—' },
    { label: 'model_id', value: passportValue.model_id ?? '—' },
    { label: 'prompt_version', value: passportValue.prompt_version ?? '—' },
    { label: 'prompt_sha256', value: short(passportValue.prompt_sha256, 24), mono: true },
    { label: 'generation_params', value: json(passportValue.generation_params) },
    { label: 'template_id', value: passportValue.template_id ?? '—' },
    { label: 'template_config', value: json(passportValue.template_config) },
    { label: 'code_commit_sha', value: short(passportValue.code_commit_sha, 24), mono: true },
    { label: 'dependency_lock_sha256', value: short(passportValue.dependency_lock_sha256, 24), mono: true },
    { label: 'metrics', value: json(passportValue.metrics) },
    { label: 'cost_usd', value: passportValue.cost_usd === null || passportValue.cost_usd === undefined ? '—' : `$${Number(passportValue.cost_usd).toFixed(4)}` },
    { label: 'artifact_manifest', value: json(passportValue.artifact_manifest) },
    { label: 'started_at', value: passportValue.started_at ?? '—' },
    { label: 'finished_at', value: passportValue.finished_at ?? '—' },
  ]
})

const diff = computed(() => store.passportActionResult?.diff ?? null)
const resultPassport = computed(() => store.passportActionResult?.passport ?? null)

const diffRows = computed(() => {
  const report = diff.value
  if (!report) return []
  const rows: Array<{ label: string; value: string }> = []
  for (const [metric, change] of Object.entries(report.metric_diff ?? {})) {
    rows.push({
      label: `指标 ${metric}`,
      value: `父 ${change?.parent ?? '—'} → 子 ${change?.child ?? '—'}（Δ ${change?.delta ?? '—'}）`,
    })
  }
  if (report.cost_diff) {
    rows.push({
      label: '成本',
      value: `父 $${report.cost_diff.parent ?? '—'} → 子 $${report.cost_diff.child ?? '—'}（Δ ${report.cost_diff.delta ?? '—'}）`,
    })
  }
  if (report.duration_diff) {
    rows.push({
      label: '耗时',
      value: `父 ${report.duration_diff.parent_ms ?? '—'} ms → 子 ${report.duration_diff.child_ms ?? '—'} ms（Δ ${report.duration_diff.delta_ms ?? '—'} ms）`,
    })
  }
  if (report.verdict) rows.push({ label: 'verdict', value: String(report.verdict) })
  if (report.note) rows.push({ label: '说明', value: String(report.note) })
  return rows
})
</script>

<template>
  <div class="panel">
    <section class="selector">
      <strong>选择运行记录</strong>
      <el-select v-model="store.passportRunId" size="small" placeholder="选择 run" class="run-select">
        <el-option
          v-for="run in store.runs"
          :key="run.id"
          :label="`run #${run.id} · iteration ${run.iteration} · ${run.status}${run.stop_reason ? ` · ${run.stop_reason}` : ''}`"
          :value="run.id"
        />
      </el-select>
      <el-button
        size="small"
        :disabled="store.passportRunId === null"
        :loading="store.busy === 'passport'"
        @click="store.passportRunId !== null && store.loadPassport(store.passportRunId)"
      >
        读取 Passport
      </el-button>
    </section>

    <!-- 未挂载：空状态，不伪造 -->
    <el-alert v-if="unavailable" type="info" :closable="false" show-icon>
      <template #title>Passport 数据未就绪：{{ unavailable?.waitingFor }}</template>
      <template #default>
        <p class="alert-text">{{ unavailable?.reason }}</p>
        <p class="alert-text">可复现动作：replay（public 可用，限额） / rerun（需 Owner）。</p>
      </template>
    </el-alert>

    <template v-if="passport">
      <section class="card">
        <header class="card-head">
          <strong>Passport #{{ passport.id }}</strong>
          <span class="badge" :class="passport.status === 'complete' ? 'badge--ok' : 'badge--warn'">
            {{ passport.status === 'complete' ? '可复现（complete）' : `不可宣称可复现（${passport.status}）` }}
          </span>
          <span v-if="passport.is_replay" class="badge badge--warn">回放结果（is_replay=true，非实时）</span>
          <span v-if="passport.missing_fields?.length" class="badge badge--warn">
            缺失关键字段：{{ passport.missing_fields.join('、') }}
          </span>
        </header>

        <dl class="fields">
          <template v-for="field in fields" :key="field.label">
            <dt>{{ field.label }}</dt>
            <dd :class="{ mono: field.mono, warn: field.warn }">{{ field.value }}</dd>
          </template>
        </dl>
      </section>

      <section class="actions">
        <el-button
          type="primary"
          size="small"
          :loading="store.busy === 'replay'"
          @click="store.doReplay(passport.id)"
        >
          replay（回放，public 可用）
        </el-button>
        <el-button
          size="small"
          :disabled="!canWrite"
          :loading="store.busy === 'rerun'"
          @click="store.doRerun(passport.id)"
        >
          rerun（用当前实时模型重跑，需 Owner）
        </el-button>
        <span v-if="!canWrite" class="hint hint--warn">
          非 Owner：rerun 按钮已禁用（public_demo 面禁止该写操作，后端返回 403）
        </span>
      </section>
    </template>

    <!-- 动作结果与差异报告 -->
    <section v-if="store.passportActionResult" class="card">
      <header class="card-head">
        <strong>{{ store.passportActionResult.kind }} 结果</strong>
        <span class="hint">{{ new Date(store.passportActionResult.at).toLocaleString() }}</span>
      </header>
      <p v-if="store.passportActionNote" class="alert-text">{{ store.passportActionNote }}</p>

      <div v-if="resultPassport" class="mini">
        <span>子 Passport #{{ resultPassport.id }}</span>
        <span>status {{ resultPassport.status }}</span>
        <span>is_replay {{ String(resultPassport.is_replay ?? false) }}</span>
        <span>parent {{ resultPassport.parent_passport_id ?? '—' }}</span>
      </div>

      <table v-if="diffRows.length" class="diff">
        <tbody>
          <tr v-for="row in diffRows" :key="row.label">
            <th>{{ row.label }}</th>
            <td>{{ row.value }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <el-empty
      v-if="!passport && !unavailable && store.passport === null"
      description="尚未读取 Passport：请选择 run 后点击「读取 Passport」"
    />
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.selector {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-sm);
}

.run-select {
  width: 320px;
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
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
  font-size: var(--font-size-sm);
}

.badge {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.badge--ok {
  border-color: var(--color-success);
  color: var(--color-success);
}

.badge--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.fields {
  display: grid;
  grid-template-columns: 190px 1fr;
  gap: 2px var(--space-3);
  margin: 0;
  font-size: var(--font-size-xs);
}

.fields dt {
  color: var(--color-text-secondary);
  font-family: var(--font-family-mono);
}

.fields dd {
  margin: 0;
  color: var(--color-text-primary);
  overflow-wrap: anywhere;
}

.fields dd.warn {
  color: var(--color-warning);
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.hint--warn {
  color: var(--color-warning);
}

.alert-text {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
}

.mono {
  font-family: var(--font-family-mono);
}

.mini {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.diff {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.diff th,
.diff td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  text-align: left;
}

.diff th {
  width: 160px;
  color: var(--color-text-secondary);
  font-weight: 400;
}
</style>
