<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 可行性四维雷达（WP08-T5 / WP08-A5：四维每维可点开依据）。
 *
 * 反黑箱纪律（与后端 scorer 一致）：
 * - 雷达只画**规则分**；若后端给了 `llm_suggestion`，单独以文字列出并**明确标注不参与总分**；
 * - 每维点开后必须能看到 `rationale` / `formula` / `signals` / `evidence[]`，
 *   任何一维缺依据都会被显式标红（`dimensions_without_evidence`）；
 * - 颜色全部走 tokens 变量，组件内零硬编码色值。
 */
import { RadarChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { formatScore, type Feasibility, type FeasibilityDimension } from '@/api/idea'

echarts.use([RadarChart, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps<{
  feasibility: Feasibility
}>()

const emit = defineEmits<{
  (event: 'select-dim', payload: { dimension: FeasibilityDimension }): void
  (event: 'evidence', payload: { dimension: FeasibilityDimension }): void
}>()

const chartEl = ref<HTMLElement | null>(null)
const activeKey = ref<string | null>(null)
let chart: echarts.ECharts | null = null

const dimensions = computed(() => props.feasibility?.dimensions ?? [])
const withoutEvidence = computed(() => props.feasibility?.dimensions_without_evidence ?? [])
const active = computed<FeasibilityDimension | null>(
  () => dimensions.value.find((dim) => dim.key === activeKey.value) ?? dimensions.value[0] ?? null,
)

function cssVar(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

function buildOption(): echarts.EChartsCoreOption {
  const brand = cssVar('--color-brand', '#2563eb')
  const border = cssVar('--color-border', '#e5e6eb')
  return {
    tooltip: {},
    legend: { show: false },
    radar: {
      indicator: dimensions.value.map((dim) => ({
        name: `${dim.label}\n${dim.score}`,
        max: 100,
      })),
      radius: '66%',
      axisName: { color: cssVar('--color-text-secondary', '#646a73'), fontSize: 12 },
      splitLine: { lineStyle: { color: border } },
      axisLine: { lineStyle: { color: border } },
    },
    series: [
      {
        type: 'radar',
        symbolSize: 6,
        data: [
          {
            value: dimensions.value.map((dim) => dim.score),
            name: '四维规则分',
            areaStyle: { opacity: 0.18, color: brand },
            lineStyle: { color: brand, width: 2 },
            itemStyle: { color: brand },
          },
        ],
      },
    ],
  }
}

function render(): void {
  if (!chartEl.value || dimensions.value.length === 0) return
  if (chart === null) chart = echarts.init(chartEl.value)
  chart.setOption(buildOption(), true)
  chart.off('click')
  chart.on('click', (params: { name?: string }) => {
    const label = String(params.name ?? '').split('\n')[0]
    const hit = dimensions.value.find((dim) => dim.label === label)
    if (hit) {
      activeKey.value = hit.key
      emit('select-dim', { dimension: hit })
    }
  })
}

function onResize(): void {
  chart?.resize()
}

onMounted(() => {
  render()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart?.dispose()
  chart = null
})

watch(() => props.feasibility?.feasibility_id, render)
watch(activeKey, () => chart?.resize())
</script>

<template>
  <section class="radar">
    <header class="radar__head">
      <h3>可行性四维</h3>
      <span class="radar__total">总分 {{ formatScore(feasibility.total_score) }}</span>
      <span v-if="feasibility.scoring" class="radar__owner">{{ feasibility.scoring.owner }}</span>
      <span v-if="withoutEvidence.length" class="radar__alarm">
        有 {{ withoutEvidence.length }} 维未绑定证据：{{ withoutEvidence.join('、') }}
      </span>
    </header>

    <div class="radar__body">
      <div ref="chartEl" class="radar__chart" />

      <ul class="radar__dims">
        <li
          v-for="dim in dimensions"
          :key="dim.key"
          :class="['radar__dim', { 'radar__dim--active': dim.key === active?.key, 'radar__dim--bad': dim.evidence_count === 0 }]"
          @click="activeKey = dim.key"
        >
          <span class="radar__dim-label">{{ dim.label }}</span>
          <span class="radar__dim-score">{{ dim.score }}</span>
          <span class="radar__dim-ev">证据 {{ dim.evidence_count }}</span>
        </li>
      </ul>
    </div>

    <div v-if="active" class="radar__detail">
      <h4>{{ active.label }} · {{ active.score }} 分</h4>
      <p class="radar__rationale">{{ active.rationale }}</p>
      <p class="radar__formula"><span class="radar__k">公式</span> {{ active.formula }}</p>
      <details class="radar__signals scroll-y">
        <summary>打分依据（真实信号）</summary>
        <pre>{{ JSON.stringify(active.signals, null, 2) }}</pre>
      </details>
      <p v-if="active.signals_missing?.length" class="radar__missing">
        未提供信号：{{ active.signals_missing.join('；') }}
      </p>
      <p v-if="active.evidence_note" class="radar__missing">{{ active.evidence_note }}</p>
      <div class="radar__ev-actions">
        <button type="button" class="radar__btn" @click="emit('evidence', { dimension: active })">
          查看该维证据 {{ active.evidence_count }}
        </button>
      </div>
      <p v-if="active.llm_suggestion" class="radar__llm">
        LLM 建议分 {{ active.llm_suggestion.suggested_score ?? '—' }}（不参与总分）：
        {{ active.llm_suggestion.comment || '无说明' }}
      </p>
    </div>

    <footer v-if="feasibility.scoring" class="radar__scoring">
      <p class="radar__formula"><span class="radar__k">总分公式</span> {{ feasibility.scoring.formula }}</p>
      <ul class="radar__weights">
        <li v-for="item in feasibility.scoring.contributions" :key="item.key">
          {{ item.label }}：{{ item.score }} × {{ item.weight }} = {{ item.contribution }}
        </li>
      </ul>
    </footer>
  </section>
</template>

<style scoped>
.radar__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
}
.radar__total {
  font-size: var(--font-size-lg);
  color: var(--color-brand);
  font-weight: 600;
}
.radar__owner {
  padding: 0 var(--space-2);
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.radar__alarm {
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}
.radar__body {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  align-items: center;
}
.radar__chart {
  width: 340px;
  height: 300px;
}
.radar__dims {
  flex: 1 1 240px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.radar__dim {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  cursor: pointer;
}
.radar__dim--active {
  background: var(--color-brand-soft);
}
.radar__dim--bad {
  border-left: 3px solid var(--color-danger);
}
.radar__dim-label {
  flex: 1 1 auto;
}
.radar__dim-score {
  font-family: var(--font-family-mono);
  font-size: var(--font-size-lg);
  color: var(--color-brand);
}
.radar__dim-ev {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.radar__detail {
  margin-top: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
}
.radar__rationale {
  margin: 0 0 var(--space-2);
  line-height: var(--line-height-base);
}
.radar__formula {
  margin: 0 0 var(--space-1);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  word-break: break-all;
}
.radar__k {
  margin-right: var(--space-2);
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
  color: var(--color-text-primary);
}
.radar__signals summary {
  cursor: pointer;
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}
.radar__signals pre {
  max-height: 240px;
  overflow: auto;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-card-bg);
  font-size: var(--font-size-xs);
}
.radar__missing {
  margin: var(--space-1) 0 0;
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}
.radar__btn {
  margin-top: var(--space-2);
  padding: 1px var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  background: var(--color-card-bg);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.radar__llm {
  margin: var(--space-2) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.radar__scoring {
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
}
.radar__weights {
  margin: var(--space-1) 0 0;
  padding-left: var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
</style>
