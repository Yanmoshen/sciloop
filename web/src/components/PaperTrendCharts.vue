<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 文献总览的两个图（真实数据，不造假）：
 *   ① 圆环「解析构成」：已解析 / 未解析（统一口径：全文成功 **且** 有解析卡片）
 *   ② 折线+细柱「论文与解析趋势」：总论文 / 已解析 / 未解析（累计，走左轴画线）
 *      + 新增论文 / 新增已解析（增量，走右轴画细柱）；后两项默认勾选，前两项可选勾选
 *
 * 用项目已有的 echarts（与 InfluenceBreakdown 同一套按需引入），好处是：
 * 悬浮数值由 echarts 的 tooltip 承担 —— **不会**出现 SVG `<title>` 那种原生白框，
 * 长文本也由 tooltip 自己排版，不会撑破圆环中心。
 * 颜色一律取 tokens.css 变量，主题切换后重绘。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BarChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'

import { fetchPaperTrends, type PapersOverview, type PapersTrends } from '@/api/papers'
import { useSessionStore } from '@/stores/session'

echarts.use([PieChart, LineChart, BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ overview: PapersOverview | null }>()
const session = useSessionStore()

/** 窗口：7 天 / 30 天 / 全部（后端 0 = 全部历史） */
const range = ref<'7' | '30' | 'all'>('7')
const bucket = ref<'day' | 'week'>('day')

/** 折线图五个序列的开关（默认展示：总论文 / 新增论文 / 新增已解析） */
const seriesOn = ref<Record<string, boolean>>({
  total: true,
  new_papers: true,
  new_parsed: true,
  parsed: false,
  unparsed: false,
})

const trends = ref<PapersTrends | null>(null)
const loading = ref(false)
const errorText = ref('')

const donutEl = ref<HTMLElement | null>(null)
const trendEl = ref<HTMLElement | null>(null)
let donutChart: echarts.ECharts | null = null
let trendChart: echarts.ECharts | null = null

const SERIES_META = [
  { key: 'total', label: '总论文数', kind: 'line' as const, axis: 'left' as const, color: '--color-brand' },
  { key: 'new_papers', label: '新增论文数', kind: 'bar' as const, axis: 'right' as const, color: '--color-brand' },
  { key: 'new_parsed', label: '新增已解析论文数', kind: 'bar' as const, axis: 'right' as const, color: '--color-success' },
  { key: 'parsed', label: '已解析论文数', kind: 'line' as const, axis: 'left' as const, color: '--color-success' },
  { key: 'unparsed', label: '未解析论文数', kind: 'line' as const, axis: 'left' as const, color: '--color-text-disabled' },
]

/** 语义色 → 主题实际色值（同一序列在圆环、折线、图例上用同一个色，避免同色不同义） */
function tokenColor(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

const parsed = computed(() => props.overview?.papers_parsed ?? 0)
const unparsed = computed(() => props.overview?.papers_unparsed ?? 0)

function pct(value: number, total: number): string {
  if (!total) return '0'
  return ((value / total) * 100).toFixed(1)
}

/* ---------------- 圆环 ---------------- */
function renderDonut(): void {
  const el = donutEl.value
  if (!el) return
  if (donutChart === null) donutChart = echarts.init(el)
  donutChart.setOption(
    {
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const item = p as { name: string; value: number }
          const total = parsed.value + unparsed.value
          return `${item.name}：${item.value} 篇 · ${pct(item.value, total)}%`
        },
      },
      series: [
        {
          type: 'pie',
          radius: ['62%', '84%'],
          center: ['50%', '50%'],
          avoidLabelOverlap: false,
          label: {
            show: true,
            position: 'center',
            formatter: () => `{t|已解析 / 总量}\n{v|${parsed.value} / ${parsed.value + unparsed.value}}`,
            rich: {
              t: { fontSize: 12, color: tokenColor('--color-text-secondary'), lineHeight: 20 },
              v: { fontSize: 19, fontWeight: 500, color: tokenColor('--color-text-primary'), lineHeight: 24 },
            },
          },
          emphasis: { scale: false, label: { show: true } },
          labelLine: { show: false },
          data: [
            { name: '已解析', value: parsed.value, itemStyle: { color: tokenColor('--color-success') } },
            {
              name: '未解析',
              value: unparsed.value,
              itemStyle: { color: tokenColor('--color-bg-muted') },
            },
          ],
        },
      ],
    },
    true,
  )
}

/* ---------------- 折线 + 细柱 ---------------- */
function renderTrend(): void {
  const el = trendEl.value
  const data = trends.value
  if (!el || !data) return
  if (trendChart === null) trendChart = echarts.init(el)

  const active = SERIES_META.filter((meta) => seriesOn.value[meta.key])
  const leftKeys = active.filter((meta) => meta.axis === 'left')
  const rightKeys = active.filter((meta) => meta.axis === 'right')

  trendChart.setOption(
    {
      grid: { left: 8, right: 8, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: tokenColor('--color-text-secondary') } },
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params : [params]
          const rows = list as Array<{ axisValue: string; seriesName: string; value: number; color: string }>
          if (!rows.length) return ''
          const head = `<div style="margin-bottom:4px">${rows[0].axisValue}</div>`
          const body = rows
            .map(
              (row) =>
                `<div style="display:flex;gap:8px;align-items:center">
                   <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${row.color}"></span>
                   <span style="flex:1">${row.seriesName}</span>
                   <b style="margin-left:12px">${row.value}</b>
                 </div>`,
            )
            .join('')
          return head + body
        },
      },
      xAxis: {
        type: 'category',
        data: data.axis,
        axisLine: { lineStyle: { color: tokenColor('--color-border') } },
        axisLabel: { color: tokenColor('--color-text-secondary'), fontSize: 11 },
      },
      yAxis: [
        {
          type: 'value',
          name: '累计',
          nameTextStyle: { color: tokenColor('--color-text-secondary'), fontSize: 11 },
          splitLine: { lineStyle: { color: tokenColor('--color-border'), type: 'dashed' } },
          axisLabel: { color: tokenColor('--color-text-secondary'), fontSize: 11 },
        },
        {
          type: 'value',
          name: '新增',
          nameTextStyle: { color: tokenColor('--color-text-secondary'), fontSize: 11 },
          splitLine: { show: false },
          axisLabel: { color: tokenColor('--color-text-secondary'), fontSize: 11 },
        },
      ],
      series: [
        ...leftKeys.map((meta) => ({
          name: meta.label,
          type: 'line' as const,
          yAxisIndex: 0,
          smooth: true,
          symbolSize: 5,
          lineStyle: { width: 2, color: tokenColor(meta.color) },
          itemStyle: { color: tokenColor(meta.color) },
          data: data.series[meta.key as keyof PapersTrends['series']],
        })),
        ...rightKeys.map((meta) => ({
          name: meta.label,
          type: 'bar' as const,
          yAxisIndex: 1,
          barMaxWidth: 9,
          itemStyle: { color: tokenColor(meta.color), opacity: 0.8, borderRadius: [2, 2, 0, 0] },
          data: data.series[meta.key as keyof PapersTrends['series']],
        })),
      ],
    },
    true,
  )
}

async function loadTrends(): Promise<void> {
  loading.value = true
  errorText.value = ''
  try {
    trends.value = await fetchPaperTrends({
      days: range.value === 'all' ? 0 : Number(range.value),
      bucket: bucket.value,
    })
    renderTrend()
  } catch (error) {
    trends.value = null
    errorText.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

function toggleSeries(key: string): void {
  seriesOn.value = { ...seriesOn.value, [key]: !seriesOn.value[key] }
  renderTrend()
}

function resize(): void {
  donutChart?.resize()
  trendChart?.resize()
}

onMounted(() => {
  renderDonut()
  void loadTrends()
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  donutChart?.dispose()
  trendChart?.dispose()
  donutChart = null
  trendChart = null
})

/** 圆环随 overview 变化重绘（统计卡与图表始终同源） */
watch(() => props.overview?.papers_parsed, () => renderDonut())
/** 主题切换后 SVG/Canvas 里写死的是旧色值，必须重绘 */
watch(() => session.theme, () => {
  renderDonut()
  renderTrend()
})
watch(range, () => void loadTrends())
watch(bucket, () => void loadTrends())
</script>

<template>
  <div class="charts">
    <article class="sl-card chart-card">
      <header class="chart-card__head">
        <h2>解析构成</h2>
      </header>
      <div ref="donutEl" class="chart-card__donut" />
      <ul class="donut-legend">
        <li>
          <span class="donut-legend__dot donut-legend__dot--ok" />
          已解析
          <b>{{ parsed }} 篇 · {{ pct(parsed, parsed + unparsed) }}%</b>
        </li>
        <li>
          <span class="donut-legend__dot donut-legend__dot--muted" />
          未解析
          <b>{{ unparsed }} 篇 · {{ pct(unparsed, parsed + unparsed) }}%</b>
        </li>
      </ul>
    </article>

    <article class="sl-card chart-card chart-card--wide">
      <header class="chart-card__head">
        <h2>论文与解析趋势</h2>
        <div class="chart-card__spacer" />
        <div class="chart-switch">
          <button
            v-for="item in [
              { key: '7', label: '近 7 天' },
              { key: '30', label: '近 30 天' },
              { key: 'all', label: '全部' },
            ]"
            :key="item.key"
            class="btn btn--ghost btn--sm"
            :class="{ 'is-on': range === item.key }"
            type="button"
            :disabled="loading"
            @click="range = item.key as '7' | '30' | 'all'"
          >
            {{ item.label }}
          </button>
        </div>
        <div class="chart-switch">
          <button
            class="btn btn--ghost btn--sm"
            :class="{ 'is-on': bucket === 'day' }"
            type="button"
            :disabled="loading"
            @click="bucket = 'day'"
          >
            按天
          </button>
          <button
            class="btn btn--ghost btn--sm"
            :class="{ 'is-on': bucket === 'week' }"
            type="button"
            :disabled="loading"
            @click="bucket = 'week'"
          >
            按周
          </button>
        </div>
      </header>

      <div class="series-legend">
        <label v-for="meta in SERIES_META" :key="meta.key" class="series-legend__item">
          <input
            type="checkbox"
            :checked="seriesOn[meta.key]"
            @change="toggleSeries(meta.key)"
          />
          <span
            class="series-legend__swatch"
            :class="{ 'series-legend__swatch--bar': meta.kind === 'bar' }"
            :style="{ background: `var(${meta.color})` }"
          />
          {{ meta.label }}（{{ meta.kind === 'bar' ? '柱' : '线' }}）
        </label>
      </div>

      <div v-if="errorText" class="hint hint--err">趋势数据获取失败：{{ errorText }}</div>
      <div v-else-if="loading && !trends" class="chart-card__skeleton">
        <el-skeleton :rows="4" animated />
      </div>
      <div ref="trendEl" class="chart-card__trend" />
    </article>
  </div>
</template>

<style scoped>
.charts {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: var(--space-3);
}

.chart-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

.chart-card__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.chart-card__head h2 {
  margin: 0;
  font-size: var(--font-size-md);
}

.chart-card__spacer {
  flex: 1;
}

.chart-switch {
  display: inline-flex;
  gap: 2px;
}

.btn--ghost {
  border-color: transparent;
  background: transparent;
  color: var(--color-text-secondary);
}

.btn--ghost.is-on {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
  color: var(--color-brand);
}

.btn--sm {
  height: 26px;
  padding: 0 var(--space-2);
  font-size: var(--font-size-xs);
}

.chart-card__donut {
  height: 168px;
}

.chart-card__trend {
  height: 268px;
}

.chart-card__skeleton {
  padding: var(--space-2) 0;
}

.donut-legend {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}

.donut-legend li {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.donut-legend b {
  margin-left: auto;
  color: var(--color-text-primary);
  font-weight: 500;
}

.donut-legend__dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.donut-legend__dot--ok {
  background: var(--color-success);
}

.donut-legend__dot--muted {
  background: var(--color-bg-muted);
}

.series-legend {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.series-legend__item {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  cursor: pointer;
}

.series-legend__swatch {
  width: 14px;
  height: 3px;
  border-radius: 2px;
}

.series-legend__swatch--bar {
  width: 8px;
  height: 10px;
}

@media (max-width: 1080px) {
  .charts {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
