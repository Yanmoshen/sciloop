<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 排序四维 + 影响力三项分项可视化（WP07-T3）——ECharts 分组条形图 + 明细表。
 *
 * 口径（计划书 §2.7.2/§2.7.3）：
 * - 四维（relevance / recency / citation_trend / evidence_completeness）是**推荐排序主口径**；
 * - 三项（venue / citation_velocity / code_heat）是**辅助展示分，明确不参与默认排序**；
 * - 每项展示 value / source / confidence；缺失显示「未获取」，**绝不显示 0 分**；
 * - llm_novelty 仅作辅助标签，stable=false 时只展示区间并标 unstable。
 * 图表颜色全部从 tokens.css 变量读取（组件内无硬编码色值）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

import {
  describeNovelty,
  DIMENSION_LABELS,
  INFLUENCE_DIMENSIONS,
  RANK_DIMENSIONS,
  type Breakdown,
  type NoveltyTag,
  type ScoreDimension,
} from '@/api/feed'

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = withDefaults(
  defineProps<{
    rankBreakdown: Breakdown
    influenceBreakdown: Breakdown
    rankWeights?: Record<string, number>
    influenceWeights?: Record<string, number>
    rankScore?: number | null
    influenceScore?: number | null
    scoreCoverage?: number | null
    influenceCoverage?: number | null
    novelty?: NoveltyTag | null
  }>(),
  {
    rankWeights: () => ({}),
    influenceWeights: () => ({}),
    rankScore: null,
    influenceScore: null,
    scoreCoverage: null,
    influenceCoverage: null,
    novelty: null,
  },
)

const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

interface Row {
  key: string
  label: string
  group: 'rank' | 'influence'
  dim: ScoreDimension | null
  weight: number | null
}

const emptyDim: ScoreDimension = { value: null, source: null, confidence: null }

const rows = computed<Row[]>(() => [
  ...RANK_DIMENSIONS.map((key) => ({
    key,
    label: DIMENSION_LABELS[key] ?? key,
    group: 'rank' as const,
    dim: props.rankBreakdown?.[key] ?? emptyDim,
    weight: props.rankWeights?.[key] ?? null,
  })),
  ...INFLUENCE_DIMENSIONS.map((key) => ({
    key,
    label: DIMENSION_LABELS[key] ?? key,
    group: 'influence' as const,
    dim: props.influenceBreakdown?.[key] ?? emptyDim,
    weight: props.influenceWeights?.[key] ?? null,
  })),
])

function token(names: string[]): string {
  if (typeof window === 'undefined') return ''
  const styles = getComputedStyle(document.documentElement)
  for (const name of names) {
    const value = styles.getPropertyValue(name)
    if (value && value.trim()) return value.trim()
  }
  return ''
}

function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '未获取'
  return String(Math.round(value * 100) / 100)
}

function formatWeight(weight: number | null): string {
  if (weight === null || weight === undefined) return '权重未获取'
  return `权重 ${weight}`
}

function formatConfidence(confidence: number | null | undefined): string {
  if (confidence === null || confidence === undefined || Number.isNaN(confidence)) {
    return '置信度未获取'
  }
  return `置信度 ${Math.round(confidence * 100) / 100}`
}

/** 数据完整度：null → 未获取；0 → 明确说明「分项均未获取」，避免被读成 0 分 */
function formatCoverage(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '未获取'
  if (value <= 0) return '0（分项均未获取）'
  return String(Math.round(value * 100) / 100)
}

/** 缺失原因：如实展示 source 标注，不猜测、不补值 */
function missingReason(dim: ScoreDimension | null): string {
  const source = dim?.source ?? null
  if (!source) return '来源未标注'
  if (source === 'not_scored') return '尚未计算（source=not_scored）'
  return `${source} 未返回可用数值`
}

const option = computed(() => {
  const brand = token(['--color-brand', '--brand'])
  const warn = token(['--color-warning', '--warning'])
  const border = token(['--color-border'])
  const textPrimary = token(['--color-text-primary'])
  const textSecondary = token(['--color-text-secondary'])
  const labels = rows.value.map((row) => row.label)
  const rankData = rows.value.map((row) =>
    row.group === 'rank' && row.dim?.value !== null && row.dim?.value !== undefined
      ? row.dim.value
      : null,
  )
  const influenceData = rows.value.map((row) =>
    row.group === 'influence' && row.dim?.value !== null && row.dim?.value !== undefined
      ? row.dim.value
      : null,
  )

  return {
    animation: false,
    grid: { left: 92, right: 28, top: 20, bottom: 24 },
    tooltip: {
      trigger: 'item',
      backgroundColor: token(['--color-card-bg']),
      borderColor: border,
      textStyle: { color: textPrimary, fontSize: 12 },
      formatter: (params: { dataIndex: number; seriesIndex: number }) => {
        const row = rows.value[params.dataIndex]
        if (!row) return ''
        const head = `<strong>${row.label}</strong>（${
          row.group === 'rank' ? '排序四维' : '影响力三项 · 辅助分'
        }）`
        const value =
          row.dim?.value === null || row.dim?.value === undefined
            ? `未获取：${missingReason(row.dim)}`
            : `value=${formatScore(row.dim.value)}`
        return [
          head,
          value,
          `source=${row.dim?.source ?? '未获取'}`,
          formatConfidence(row.dim?.confidence),
          formatWeight(row.weight),
        ].join('<br/>')
      },
    },
    xAxis: {
      type: 'value',
      max: 100,
      min: 0,
      axisLabel: { color: textSecondary, fontSize: 10 },
      axisLine: { lineStyle: { color: border } },
      splitLine: { lineStyle: { color: border } },
    },
    yAxis: {
      type: 'category',
      data: labels,
      inverse: true,
      axisLabel: { color: textPrimary, fontSize: 11 },
      axisLine: { lineStyle: { color: border } },
      axisTick: { show: false },
    },
    series: [
      {
        name: '排序四维',
        type: 'bar',
        barMaxWidth: 12,
        data: rankData,
        itemStyle: { color: brand, borderRadius: [0, 3, 3, 0] },
      },
      {
        name: '影响力三项（辅助分）',
        type: 'bar',
        barMaxWidth: 12,
        data: influenceData,
        itemStyle: { color: warn, borderRadius: [0, 3, 3, 0] },
      },
    ],
  }
})

function renderChart(): void {
  if (chartEl.value === null) return
  if (chart === null) chart = echarts.init(chartEl.value)
  chart.setOption(option.value as echarts.EChartsCoreOption, true)
  chart.resize()
}

function handleResize(): void {
  chart?.resize()
}

onMounted(() => {
  renderChart()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  chart?.dispose()
  chart = null
})

watch(option, () => renderChart())

const noveltyText = computed(() => describeNovelty(props.novelty).text)
const novelty = computed(() => describeNovelty(props.novelty))
</script>

<template>
  <div class="breakdown">
    <div class="breakdown__head">
      <div class="breakdown__scores">
        <span class="score-chip">
          推荐排序分 rank_score
          <em>{{ formatScore(rankScore) }}</em>
          <small>数据完整度 score_coverage：{{ formatCoverage(scoreCoverage) }}</small>
        </span>
        <span class="score-chip score-chip--aux">
          影响力分 influence_score
          <em>{{ formatScore(influenceScore) }}</em>
          <small>
            辅助分 · 不用于推荐排序（完整度 {{ formatCoverage(influenceCoverage) }}）
          </small>
        </span>
      </div>
    </div>

    <div class="breakdown__chart-head">
      <span class="legend"><i class="legend__dot legend__dot--rank" />排序四维（主口径）</span>
      <span class="legend">
        <i class="legend__dot legend__dot--aux" />影响力三项（辅助分，不参与默认排序）
      </span>
    </div>

    <div ref="chartEl" class="breakdown__chart" role="img" aria-label="排序四维与影响力三项分组条形图" />

    <ul class="breakdown__list">
      <li v-for="row in rows" :key="row.key" class="dim-row" :class="`dim-row--${row.group}`">
        <span class="dim-row__label">
          <span class="dim-row__group">{{ row.group === 'rank' ? '四维' : '辅助' }}</span>
          {{ row.label }}
        </span>
        <span class="dim-row__value" :class="{ 'dim-row__value--missing': row.dim?.value === null }">
          {{ formatScore(row.dim?.value) }}
        </span>
        <span class="dim-row__meta sl-source-tag">
          source={{ row.dim?.source ?? '未获取' }} · {{ formatConfidence(row.dim?.confidence) }} ·
          {{ formatWeight(row.weight) }}
          <template v-if="row.dim?.value === null">
            <br /><span class="dim-row__reason">{{ missingReason(row.dim) }}</span>
          </template>
        </span>
      </li>
    </ul>

    <dl class="breakdown__novelty">
      <dt>LLM 新颖性标签（辅助标签，不进入任何分数）</dt>
      <dd>
        {{ noveltyText }}
      </dd>
    </dl>
  </div>
</template>

<style scoped>
.breakdown {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.breakdown__scores {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.score-chip {
  display: inline-flex;
  flex-direction: column;
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.score-chip em {
  color: var(--color-brand);
  font-style: normal;
  font-size: var(--font-size-lg);
  font-family: var(--font-family-mono);
}

.score-chip--aux em {
  color: var(--color-warning);
}

.score-chip small {
  color: var(--color-text-secondary);
}

.breakdown__chart-head {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.legend {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.legend__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-sm);
}

.legend__dot--rank {
  background-color: var(--color-brand);
}

.legend__dot--aux {
  background-color: var(--color-warning);
}

.breakdown__chart {
  width: 100%;
  height: 220px;
}

.breakdown__list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.dim-row {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) 72px minmax(200px, 2fr);
  gap: var(--space-2);
  align-items: baseline;
  padding: var(--space-1) 0;
  border-bottom: 1px dashed var(--color-border);
  font-size: var(--font-size-xs);
}

.dim-row__label {
  color: var(--color-text-primary);
}

.dim-row__group {
  display: inline-block;
  min-width: 32px;
  margin-right: var(--space-1);
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background-color: var(--color-brand-soft);
  color: var(--color-brand);
  font-size: var(--font-size-2xs);
  text-align: center;
}

.dim-row--influence .dim-row__group {
  background-color: var(--color-warning-soft);
  color: var(--color-warning);
}

.dim-row__value {
  font-family: var(--font-family-mono);
  color: var(--color-text-primary);
}

.dim-row__value--missing {
  color: var(--color-warning);
}

.dim-row__reason {
  color: var(--color-text-secondary);
}

.breakdown__novelty {
  display: flex;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--font-size-xs);
}

.breakdown__novelty dt {
  flex: 0 0 auto;
  color: var(--color-text-secondary);
}

.breakdown__novelty dd {
  margin: 0;
  color: var(--color-text-primary);
}


</style>
