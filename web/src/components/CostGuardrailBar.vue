<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 成本双线（WP15-T4 / contracts.ui_mandatory_elements）。
 *
 * 铁律：**护栏值 8.0 USD 与演示配额 3.0 USD 必须并列显示**，
 * 接近上限变警告色；另显示 replay_saved_usd（回放省下的反事实成本）。
 *
 * 数值全部来自 GET /pipelines/{pid}/status 的 cost 或 GET /costs/summary?project_id=
 * （含 limit_usd=8.0 / quota_usd=3.0）；两者都不可用时按契约常量显示双线骨架并标注「未取到」。
 */
import { computed } from 'vue'

import { COST_DEFAULT_LIMIT_USD, COST_DEFAULT_QUOTA_USD } from '@/api/workbench'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()

/** 接近上限的告警线（超过即变警告色） */
const WARN_RATIO = 0.8

const snapshot = computed(() => store.cost)

interface Line {
  key: string
  label: string
  used: number
  limit: number
  ratio: number
  level: 'ok' | 'warn' | 'over'
  exceeded: boolean
  note: string
  source: string
}

const lines = computed<Line[]>(() => {
  const cost = snapshot.value
  const used = cost?.used_usd ?? 0
  const limit = cost?.limit_usd ?? COST_DEFAULT_LIMIT_USD
  const quota = cost?.quota_usd ?? COST_DEFAULT_QUOTA_USD
  const source = cost ? '接口' : '契约常量（未取到接口值）'
  return [
    {
      key: 'limit',
      label: '成本护栏（硬熔断线）',
      used,
      limit,
      ratio: limit > 0 ? used / limit : 0,
      level: levelOf(limit > 0 ? used / limit : 0, cost?.limit_exceeded === true),
      exceeded: cost?.limit_exceeded === true,
      note: '硬线超限即熔断（阈值不可被覆盖）',
      source,
    },
    {
      key: 'quota',
      label: '演示配额（只告警不熔断）',
      used,
      limit: quota,
      ratio: quota > 0 ? used / quota : 0,
      level: levelOf(quota > 0 ? used / quota : 0, cost?.quota_exceeded === true),
      exceeded: cost?.quota_exceeded === true,
      note: '演示配额仅告警；演示前按实测值 2 倍回设硬线',
      source,
    },
  ]
})

function levelOf(ratio: number, exceeded: boolean): 'ok' | 'warn' | 'over' {
  if (exceeded || ratio >= 1) return 'over'
  if (ratio >= WARN_RATIO) return 'warn'
  return 'ok'
}

function pct(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`
}

function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `$${Number(value).toFixed(4)}`
}

const breakdown = computed(() =>
  Object.entries(snapshot.value?.breakdown_by_stage ?? {}).sort((a, b) => Number(b[1]) - Number(a[1])),
)

const maxStageCost = computed(() =>
  breakdown.value.reduce((max, [, value]) => Math.max(max, Number(value) || 0), 0),
)
</script>

<template>
  <section class="cost">
    <header class="cost-head">
      <strong class="cost-title">成本双线</strong>
      <span class="cost-used">
        已用 {{ usd(snapshot?.used_usd) }}
        <span class="cost-src">（{{ snapshot ? '来自成本接口' : '接口未取到，显示契约默认值' }}）</span>
      </span>
      <span class="cost-replay">
        回放节省 <strong>{{ usd(snapshot?.replay_saved_usd ?? 0) }}</strong>
        <span class="cost-src">（replay_saved_usd：回放省下的反事实成本，与实时消耗分开列示）</span>
      </span>
    </header>

    <div class="cost-lines">
      <div v-for="line in lines" :key="line.key" class="cost-line" :class="`cost-line--${line.level}`">
        <div class="line-head">
          <span class="line-label">{{ line.label }}</span>
          <span class="line-values">
            {{ usd(line.used) }} / <strong>{{ usd(line.limit) }}</strong>
            <span class="line-pct">{{ pct(line.ratio) }}</span>
          </span>
        </div>
        <div class="bar" role="progressbar" :aria-valuenow="line.ratio" aria-valuemin="0" aria-valuemax="1">
          <div class="bar-fill" :style="{ width: `${Math.min(line.ratio, 1) * 100}%` }" />
        </div>
        <div class="line-foot">
          <span>{{ line.note }}</span>
          <span v-if="line.exceeded" class="line-exceeded">已超限</span>
          <span v-else-if="line.level === 'warn'" class="line-warn">接近上限</span>
          <span class="line-src">{{ line.source }}</span>
        </div>
      </div>
    </div>

    <p v-if="snapshot && !snapshot.cost_complete" class="cost-warn">
      成本统计不完整：{{ snapshot.warning ?? '有调用缺少单价，未完成部分未估算' }}
    </p>
    <p v-if="!snapshot" class="cost-warn">未取到成本接口值：以上为契约参考线，不与真实消耗混算。</p>

    <div v-if="breakdown.length" class="cost-breakdown">
      <h4>分环节成本（breakdown_by_stage）</h4>
      <ul>
        <li v-for="[stage, value] in breakdown" :key="stage">
          <span class="bd-stage">{{ stage }}</span>
          <span class="bd-bar">
            <span
              class="bd-fill"
              :style="{ width: `${maxStageCost > 0 ? (Number(value) / maxStageCost) * 100 : 0}%` }"
            />
          </span>
          <span class="bd-value">{{ usd(Number(value)) }}</span>
        </li>
      </ul>
    </div>

    <p v-if="snapshot" class="cost-meta">
      调用 {{ snapshot.calls ?? '—' }} 次 · 回放 {{ snapshot.replay_calls ?? '—' }} 次 · 失败
      {{ snapshot.failed_calls ?? '—' }} 次 · 无单价 {{ snapshot.unknown_price_calls ?? '—' }} 次 ·
      来源 {{ snapshot.source ?? '—' }} · 币种 {{ snapshot.currency ?? 'USD' }}
    </p>
  </section>
</template>

<style scoped>
.cost {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}

.cost-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-4);
  font-size: var(--font-size-xs);
}

.cost-title {
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.cost-used {
  color: var(--color-text-primary);
}

.cost-replay {
  color: var(--color-text-secondary);
}

.cost-replay strong {
  color: var(--color-success);
}

.cost-src {
  color: var(--color-text-disabled);
}

.cost-lines {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-3);
}

.cost-line {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
}

.cost-line--warn {
  border-color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.cost-line--over {
  border-color: var(--color-danger);
  background-color: var(--color-danger-soft);
}

.line-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.line-label {
  color: var(--color-text-primary);
}

.line-values strong {
  font-size: var(--font-size-lg);
  color: var(--color-text-primary);
}

.line-pct {
  margin-left: var(--space-1);
  color: var(--color-text-secondary);
}

.bar {
  height: 8px;
  margin: var(--space-2) 0 var(--space-1);
  border-radius: var(--radius-pill);
  background-color: var(--color-bg-muted);
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  background-color: var(--color-success);
  transition: width var(--motion-dur) var(--motion-ease);
}

.cost-line--warn .bar-fill {
  background-color: var(--color-warning);
}

.cost-line--over .bar-fill {
  background-color: var(--color-danger);
}

.line-foot {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.line-exceeded {
  color: var(--color-danger);
}

.line-warn {
  color: var(--color-warning);
}

.line-src {
  color: var(--color-text-disabled);
}

.cost-warn {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background-color: var(--color-warning-soft);
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}

.cost-breakdown h4 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.cost-breakdown ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.cost-breakdown li {
  display: grid;
  grid-template-columns: 90px 1fr 90px;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
}

.bd-stage {
  color: var(--color-text-secondary);
}

.bd-bar {
  height: 6px;
  border-radius: var(--radius-pill);
  background-color: var(--color-bg-muted);
  overflow: hidden;
}

.bd-fill {
  display: block;
  height: 100%;
  background-color: var(--color-brand);
}

.bd-value {
  text-align: right;
  font-family: var(--font-family-mono);
  color: var(--color-text-primary);
}

.cost-meta {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
</style>
