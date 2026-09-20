<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 证据覆盖范围标签（WP07-T4）——四种 parse_status 对应文案（计划书 §2.8 硬约束 3/4）：
 *   ok          → 全文可用（覆盖 X%）
 *   partial     → 部分可用（覆盖 X%）
 *   unavailable → 证据覆盖范围：仅摘要
 *   failed      → 解析失败
 * 覆盖率缺失时如实显示「覆盖率未获取」，**不得显示 0%**。颜色全部走 tokens.css 变量。
 */
import { computed } from 'vue'

import type { ParseStatus } from '@/api/feed'

const props = withDefaults(
  defineProps<{
    parseStatus: ParseStatus | null | undefined
    /** 0–1 覆盖率；null 表示未获取（不是 0） */
    coverage?: number | null
    /** 证据范围：fulltext / abstract_only */
    scope?: string | null
    /** 补充说明（coverage_note / parse_error） */
    note?: string | null
    /** 覆盖率的取数来源（如 paper_documents / papers.is_parsed） */
    source?: string | null
    size?: 'small' | 'default'
  }>(),
  { coverage: null, scope: null, note: null, source: null, size: 'default' },
)

/** 覆盖率百分比：一位小数，整数省略小数位；null → 未获取 */
function formatCoverage(value: number | null | undefined): string | null {
  if (value === null || value === undefined || Number.isNaN(value)) return null
  const percent = Math.round(value * 1000) / 10
  return Number.isInteger(percent) ? `${percent}%` : `${percent.toFixed(1)}%`
}

const percent = computed(() => formatCoverage(props.coverage))

const tone = computed(() => {
  switch (props.parseStatus) {
    case 'ok':
      return props.coverage !== null && props.coverage !== undefined && props.coverage < 0.6
        ? 'warn'
        : 'ok'
    case 'partial':
      return 'warn'
    case 'failed':
      return 'danger'
    case 'unavailable':
      return 'muted'
    default:
      return props.scope === 'abstract_only' ? 'muted' : 'unknown'
  }
})

const label = computed(() => {
  switch (props.parseStatus) {
    case 'ok':
      return percent.value ? `全文可用（覆盖 ${percent.value}）` : '全文可用（覆盖率未获取）'
    case 'partial':
      return percent.value ? `部分可用（覆盖 ${percent.value}）` : '部分可用（覆盖率未获取）'
    case 'unavailable':
      return '证据覆盖范围：仅摘要'
    case 'failed':
      return '解析失败'
    case null:
    case undefined:
      // 无 parse_status 但证据范围已知为仅摘要时，仍按契约文案展示
      return props.scope === 'abstract_only' ? '证据覆盖范围：仅摘要' : '解析状态未获取'
    default:
      return `解析状态未知（${String(props.parseStatus)}）`
  }
})

const detail = computed(() => {
  const lines: string[] = []
  lines.push(`parse_status=${props.parseStatus ?? 'null'}`)
  lines.push(
    percent.value ? `覆盖率=${percent.value}（coverage）` : '覆盖率未获取（coverage=null，非 0）',
  )
  if (props.scope) lines.push(`证据范围=${props.scope}`)
  if (props.source) lines.push(`取数来源=${props.source}`)
  if (props.note) lines.push(props.note)
  lines.push('门禁：只有 parse_status=ok 且 coverage≥0.60 才允许正文级 paper_span 证据')
  return lines.join('\n')
})

const iconName = computed(() => {
  switch (tone.value) {
    case 'ok':
      return '✓'
    case 'warn':
      return '!'
    case 'danger':
      return '×'
    default:
      return 'i'
  }
})
</script>

<template>
  <el-tooltip
    :content="detail"
    placement="top"
    :show-after="120"
    :enterable="true"
    :hide-after="120"
  >
    <span class="coverage-tag" :class="[`coverage-tag--${tone}`, `coverage-tag--${size}`]">
      <span class="coverage-tag__icon" aria-hidden="true">{{ iconName }}</span>
      <span class="coverage-tag__text">{{ label }}</span>
    </span>
  </el-tooltip>
</template>

<style scoped>
.coverage-tag {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  line-height: 1.6;
  white-space: nowrap;
  cursor: default;
}

.coverage-tag--small {
  font-size: var(--font-size-xs);
  padding: 0 var(--space-1);
}

.coverage-tag__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border-radius: var(--radius-pill);
  font-size: var(--font-size-2xs);
  color: var(--color-text-inverse);
  background-color: var(--color-text-disabled);
}

.coverage-tag--ok {
  border-color: var(--color-success);
  color: var(--color-success);
  background-color: var(--color-success-soft);
}

.coverage-tag--ok .coverage-tag__icon {
  background-color: var(--color-success);
}

.coverage-tag--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
  background-color: var(--color-warning-soft);
}

.coverage-tag--warn .coverage-tag__icon {
  background-color: var(--color-warning);
}

.coverage-tag--danger {
  border-color: var(--color-danger);
  color: var(--color-danger);
  background-color: var(--color-danger-soft);
}

.coverage-tag--danger .coverage-tag__icon {
  background-color: var(--color-danger);
}

.coverage-tag--muted {
  border-color: var(--color-border-strong);
  color: var(--color-text-secondary);
  background-color: var(--color-bg-subtle);
}

.coverage-tag--unknown {
  border-style: dashed;
  color: var(--color-text-secondary);
  background-color: transparent;
}
</style>
