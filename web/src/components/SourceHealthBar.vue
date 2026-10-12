<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 四源健康条（文献总览 + 设置 · 拉取设置共同使用，语义必须一致）。
 *
 * 两个按钮的分工（2026-10-11 按修改书 §3.1 固定）
 * - **立即检测** → `GET /sources/health?probe=true`：对已配置的源各发一次真实请求，
 *   四个卡片进入"检测中"，结束后一次性用本次响应替换卡片数据。
 * - **刷新** → `GET /sources/health?probe=false`：只读已持久化的探活留痕，零外部调用、
 *   不消耗配额，也不伪造时间或状态。
 *
 * 状态纪律（修改书 §3.2）
 * - `status=disabled` / `configured=false`（必需凭证）：灰灯 + "未配置"，**绝不允许绿**；
 * - `status=ok`：绿灯"可用"；
 * - `status=degraded`：警示灯"降级" + 失败原因；
 * - 从未检测过：灰灯"未检测"。
 * 每张卡片同时展示**各自的**最近一次检测时间与最近成功时间，两者不能混用；
 * 顶层 `checked_at` 只表示"最近一轮探活完成时间"。
 */
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { fetchSourceHealth, type SourceHealthEntry, type SourceHealthResponse } from '@/api/feed'

/** 固定展示顺序（与契约外部源一致） */
const SOURCE_ORDER = ['arxiv', 'semantic_scholar', 'openalex', 'github']

const props = withDefaults(defineProps<{ autoLoad?: boolean }>(), { autoLoad: true })

const health = ref<SourceHealthResponse | null>(null)
/** 任一请求进行中：按钮 loading / 防重复点击 */
const loading = ref(false)
/** 「立即检测」进行中：四张卡片显示"检测中"（刷新不进入此态） */
const probing = ref(false)
const error = ref<string | null>(null)

const sources = computed<SourceHealthEntry[]>(() => {
  const map = health.value?.sources ?? {}
  const ordered: SourceHealthEntry[] = []
  SOURCE_ORDER.forEach((key) => {
    const entry = map[key]
    if (entry) ordered.push(entry)
  })
  Object.entries(map).forEach(([key, entry]) => {
    if (!SOURCE_ORDER.includes(key)) ordered.push(entry)
  })
  return ordered
})

/** 四态：只有 `ok` 是绿的 */
type HealthState = 'ok' | 'degraded' | 'disabled' | 'unknown'

function stateOf(entry: SourceHealthEntry): HealthState {
  if (entry.status === 'disabled') return 'disabled'
  if (entry.configured === false && entry.credentials_required) return 'disabled'
  if (entry.status === 'degraded' || entry.ok === false) return 'degraded'
  if (entry.status === 'unknown' || entry.ok === null || entry.ok === undefined) return 'unknown'
  return 'ok'
}

const STATE_LABEL: Record<HealthState, string> = {
  ok: '可用',
  degraded: '降级',
  disabled: '未启用',
  unknown: '未检测',
}

function statusLabel(entry: SourceHealthEntry): string {
  const state = stateOf(entry)
  if (state === 'disabled') return entry.configured ? '未启用' : '未配置'
  return STATE_LABEL[state]
}

/** 已知的原因码换成用户能行动的说法；未知码如实原文回显（不猜） */
const REASON_LABELS: Record<string, string> = {
  not_configured: '未配置访问凭证，本来源不会发起请求',
  not_checked: '尚未检测',
  probe_failed: '本次检测没有拿到结果',
  empty_feed: '请求成功但没有返回内容',
  paper_not_found: '探活样本未命中（来源本身是通的）',
  probe_work_not_found: '探活样本未命中（来源本身是通的）',
}

function reasonOf(entry: SourceHealthEntry): string {
  const state = stateOf(entry)
  if (state === 'disabled') {
    return entry.configured ? '当前来源未启用' : REASON_LABELS.not_configured
  }
  if (state === 'unknown') return REASON_LABELS.not_checked
  if (state !== 'degraded') return ''
  const code = entry.last_probe_detail || entry.degraded_reason
  if (!code) return '本次检测未成功，原因未返回'
  return REASON_LABELS[code] ?? code
}

function formatTime(value: string | null | undefined): string {
  if (!value) return '未获取'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value)
  return parsed.toLocaleString('zh-CN', { hour12: false })
}

function probedAt(entry: SourceHealthEntry): string {
  return formatTime(entry.last_probe_at)
}

function succeededAt(entry: SourceHealthEntry): string {
  return formatTime(entry.last_probe_success_at ?? entry.last_success_at)
}

function latencySuffix(entry: SourceHealthEntry): string {
  const ms = entry.last_probe_latency_ms
  // 0/负数多来自"命中缓存"，不是真实耗时 —— 不显示，免得看起来像"瞬间完成"
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return ''
  return ` · ${Math.round(ms)} ms`
}

/** 顶部汇总：**不得**把未配置来源算作"正常"，也不得在存在其它状态时显示"全源正常" */
const summary = computed<{ text: string; tone: 'ok' | 'warn' | 'quiet' }>(() => {
  const list = sources.value
  if (!health.value || list.length === 0) return { text: '状态未获取', tone: 'quiet' }
  const states = list.map(stateOf)
  const degraded = states.filter((state) => state === 'degraded').length
  const disabled = states.filter((state) => state === 'disabled').length
  const unknown = states.filter((state) => state === 'unknown').length
  const configured = list.length - disabled
  const parts: string[] = []
  if (degraded > 0) parts.push(`降级 ${degraded}/${configured}`)
  if (disabled > 0) parts.push(`未配置 ${disabled} 个`)
  if (unknown > 0) parts.push(`未检测 ${unknown} 个`)
  if (parts.length === 0) return { text: `${list.length} 源正常`, tone: 'ok' }
  return { text: parts.join(' · '), tone: degraded > 0 ? 'warn' : 'quiet' }
})

async function load(probe = false): Promise<void> {
  loading.value = true
  probing.value = probe
  error.value = null
  try {
    health.value = await fetchSourceHealth({ probe })
  } catch (err) {
    health.value = null
    error.value = err instanceof ApiError ? err.message : (err as Error).message
  } finally {
    loading.value = false
    probing.value = false
  }
}

function runProbe(): void {
  void load(true)
}

function refreshSnapshot(): void {
  void load(false)
}

onMounted(() => {
  if (props.autoLoad) void load()
})

defineExpose({ load })
</script>

<template>
  <section class="health-bar sl-card" aria-label="数据源健康状态">
    <header class="health-bar__head">
      <span class="health-bar__title">数据源健康</span>
      <span class="health-bar__overall" :class="`health-bar__overall--${summary.tone}`">
        {{ summary.text }}
      </span>
      <span class="health-bar__checked">
        最近一轮检测：{{ formatTime(health?.checked_at ?? null) }}
      </span>
      <div class="health-bar__actions">
        <el-button size="small" :loading="probing" :disabled="loading" @click="runProbe">
          立即检测
        </el-button>
        <el-button size="small" text :disabled="loading" @click="refreshSnapshot">刷新</el-button>
      </div>
    </header>

    <el-alert
      v-if="error"
      class="health-bar__error"
      type="error"
      :closable="false"
      show-icon
      :title="`数据源健康接口不可用：${error}`"
    />

    <ul v-else-if="sources.length > 0" class="health-bar__list">
      <li
        v-for="entry in sources"
        :key="entry.source"
        class="health-item"
        :class="`health-item--${stateOf(entry)}`"
      >
        <div class="health-item__row">
          <span v-if="probing" class="health-dot health-dot--loading" aria-label="检测中" />
          <span
            v-else
            class="health-dot"
            :class="`health-dot--${stateOf(entry)}`"
            :aria-label="statusLabel(entry)"
          />
          <span class="health-item__label">{{ entry.label ?? entry.source }}</span>
          <span class="health-item__status">{{ statusLabel(entry) }}</span>
        </div>

        <div class="health-item__meta">
          最近检测：{{ probing ? '检测中…' : probedAt(entry) }}{{ probing ? '' : latencySuffix(entry) }}
        </div>
        <div class="health-item__meta">最近成功：{{ succeededAt(entry) }}</div>

        <div v-if="reasonOf(entry)" class="health-item__reason">{{ reasonOf(entry) }}</div>
      </li>
    </ul>

    <el-skeleton v-else :rows="1" animated />
  </section>
</template>

<style scoped>
.health-bar {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

.health-bar__head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.health-bar__title {
  font-size: var(--font-size-sm);
  font-weight: 600;
}

.health-bar__overall {
  padding: 1px var(--space-2);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  border: 1px solid var(--color-border);
}

.health-bar__overall--ok {
  color: var(--color-success);
  background-color: var(--color-success-soft);
  border-color: var(--color-success);
}

.health-bar__overall--warn {
  color: var(--color-warning);
  background-color: var(--color-warning-soft);
  border-color: var(--color-warning);
}

.health-bar__overall--quiet {
  color: var(--color-text-secondary);
  background-color: var(--color-bg-muted);
}

.health-bar__checked {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.health-bar__actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-left: auto;
}

.health-bar__list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.health-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
}

/* 只有"降级"高亮警示色；未启用/未检测保持中性（灰），不制造"有事"的错觉 */
.health-item--degraded {
  border-color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.health-item__row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.health-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-pill);
  background-color: var(--color-text-disabled);
}

.health-dot--ok {
  background-color: var(--color-success);
}

.health-dot--degraded {
  background-color: var(--color-warning);
}

.health-dot--disabled,
.health-dot--unknown {
  background-color: var(--color-text-disabled);
}

.health-dot--loading {
  background-color: var(--color-brand);
  animation: health-pulse 900ms ease-in-out infinite alternate;
}

@keyframes health-pulse {
  from { opacity: 0.35; transform: scale(0.75); }
  to { opacity: 1; transform: scale(1.15); }
}

.health-item__label {
  font-size: var(--font-size-sm);
}

.health-item__status {
  margin-left: auto;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.health-item__meta {
  display: flex;
  gap: var(--space-1);
  flex-wrap: wrap;
}

.health-item__reason {
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}

.health-item--disabled .health-item__reason,
.health-item--unknown .health-item__reason {
  color: var(--color-text-secondary);
}
</style>
