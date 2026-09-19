<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 四源健康条（WP07-T4）：arXiv / Semantic Scholar / OpenAlex / GitHub 连通状态。
 * 展示 last_http_status、last_success_at、confidence、degraded_reason；degraded 高亮。
 * 取数纪律：状态一律用接口返回的真实留痕，缺失显示「未获取」，不猜测、不补 0。
 */
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { fetchSourceHealth, type SourceHealthEntry, type SourceHealthResponse } from '@/api/feed'

/** 固定展示顺序（与契约外部源一致） */
const SOURCE_ORDER = ['arxiv', 'semantic_scholar', 'openalex', 'github']

const props = withDefaults(defineProps<{ autoLoad?: boolean }>(), { autoLoad: true })

const health = ref<SourceHealthResponse | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const probe = ref(false)

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

const degradedCount = computed(() => sources.value.filter((item) => !item.ok).length)

function formatTime(value: string | null): string {
  if (!value) return '未获取'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString()
}

function httpLabel(entry: SourceHealthEntry): string {
  return entry.last_http_status === null || entry.last_http_status === undefined
    ? 'HTTP 未获取'
    : `HTTP ${entry.last_http_status}`
}

function confidenceLabel(entry: SourceHealthEntry): string {
  if (entry.confidence === null || entry.confidence === undefined) return '置信度未获取'
  return `置信度 ${entry.confidence}`
}

async function load(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    health.value = await fetchSourceHealth({ probe: probe.value })
  } catch (err) {
    health.value = null
    error.value = err instanceof ApiError ? err.message : (err as Error).message
  } finally {
    loading.value = false
  }
}

function toggleProbe(value: boolean): void {
  probe.value = value
  void load()
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
      <span
        class="health-bar__overall"
        :class="degradedCount > 0 ? 'health-bar__overall--warn' : 'health-bar__overall--ok'"
      >
        {{
          health === null
            ? '状态未获取'
            : degradedCount > 0
              ? `降级 ${degradedCount}/${sources.length}`
              : '四源正常'
        }}
      </span>
      <span class="health-bar__checked">检查时间：{{ formatTime(health?.checked_at ?? null) }}</span>
      <div class="health-bar__actions">
        <el-checkbox
          :model-value="probe"
          size="small"
          label="真实探活"
          title="probe=true 会发起真实外部请求；默认只读库内留痕"
          @update:model-value="(v: string | number | boolean) => toggleProbe(Boolean(v))"
        />
        <el-button size="small" text :loading="loading" @click="load">刷新</el-button>
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
        :class="{ 'health-item--degraded': !entry.ok }"
      >
        <div class="health-item__row">
          <span class="health-dot" :class="entry.ok ? 'health-dot--ok' : 'health-dot--bad'" />
          <span class="health-item__label">{{ entry.label ?? entry.source }}</span>
          <span class="health-item__status">{{ entry.ok ? '可用' : '降级' }}</span>
        </div>
        <div class="health-item__meta sl-source-tag">
          <span>{{ httpLabel(entry) }}</span>
          <span>·</span>
          <span>{{ confidenceLabel(entry) }}</span>
        </div>
        <div class="health-item__meta sl-source-tag">
          最近成功：{{ formatTime(entry.last_success_at) }}
        </div>
        <div v-if="!entry.configured" class="health-item__reason health-item__reason--muted">
          凭据未配置（{{ entry.credentials_required ? '必需' : '可选' }}）
        </div>
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

/* degraded 高亮（颜色走 tokens 变量） */
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

.health-dot--bad {
  background-color: var(--color-warning);
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

.health-item__reason--muted {
  color: var(--color-text-secondary);
}

</style>
