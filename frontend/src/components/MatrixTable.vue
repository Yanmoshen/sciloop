<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 对比矩阵（WP08-T1 / WP08-A1：行列语义正确，单元格可展开证据）。
 *
 * 展示纪律：
 * - 卡片字段为空一律显示「材料未提供」并标注 card_field，**不填占位内容**；
 * - 单元格证据分两类：`card_field`（卡片字段出处）与 `paper_span`（真实原文段落，
 *   带章节与定位方式）；点「证据」展开，点条目向上抛 `evidence` 事件由父组件打开证据面板。
 * - 覆盖率走 CoverageTag 口径（`coverage_tag` 由后端按 fulltext_gate 生成），
 *   组件不自行推断覆盖范围。
 */
import { computed, ref } from 'vue'

import type { CellEvidence, MatrixCell, MatrixRow, ComparisonMatrix } from '@/api/idea'

const props = defineProps<{
  matrix: ComparisonMatrix | null
  /** 高亮单元格对应的论文（父组件选中行同步） */
  activePaperId?: number | null
}>()

const emit = defineEmits<{
  (event: 'evidence', payload: { paperId: number; dimension: string; items: CellEvidence[] }): void
  (event: 'paper', payload: { paperId: number; title: string | null }): void
}>()

const expanded = ref<string | null>(null)

const rows = computed<MatrixRow[]>(() => props.matrix?.rows ?? [])
const dimensions = computed(() => props.matrix?.dimensions ?? [])

function cellOf(row: MatrixRow, key: string): MatrixCell | undefined {
  return row.values?.[key]
}

function cellText(cell: MatrixCell | undefined): string[] {
  if (!cell || cell.missing) return []
  // ``kind`` 属于维度定义而非单元格：文本型维度用 ``text``，列表型维度用 ``items``
  if (!cell.items?.length && cell.text) return [cell.text]
  return []
}

function cellItems(cell: MatrixCell | undefined): string[] {
  if (!cell || cell.missing) return []
  return cell.items ?? (cell.text ? [cell.text] : [])
}

function toggle(row: MatrixRow, key: string): void {
  const token = `${row.paper_id}:${key}`
  expanded.value = expanded.value === token ? null : token
}

function openEvidence(row: MatrixRow, key: string, cell: MatrixCell | undefined): void {
  emit('evidence', { paperId: row.paper_id, dimension: key, items: cell?.evidence ?? [] })
}

function isOpen(row: MatrixRow, key: string): boolean {
  return expanded.value === `${row.paper_id}:${key}`
}

const evidenceTotal = computed(() =>
  rows.value.reduce(
    (sum, row) =>
      sum +
      dimensions.value.reduce((inner, dim) => inner + (cellOf(row, dim.key)?.evidence?.length ?? 0), 0),
    0,
  ),
)
</script>

<template>
  <section class="matrix">
    <header class="matrix__head">
      <h3>对比矩阵</h3>
      <p class="matrix__meta">
        <span v-if="matrix"> {{ matrix.row_count }} 篇 × {{ matrix.column_count }} 维 </span>
        <span v-if="matrix"> · 单元格证据 {{ evidenceTotal }} 条 </span>
        <span v-if="matrix?.generated_by"> · 生成方式 {{ matrix.generated_by }} </span>
      </p>
    </header>

    <el-empty v-if="!matrix || rows.length === 0" description="暂无聚合结果：请先创建聚合" />

    <div v-else class="matrix__scroll">
      <table class="matrix__table">
        <thead>
          <tr>
            <th class="matrix__th matrix__th--paper">论文</th>
            <th v-for="dim in dimensions" :key="dim.key" class="matrix__th">
              {{ dim.label }}
              <span class="matrix__field">{{ dim.card_field }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in rows" :key="row.paper_id">
            <tr :class="['matrix__row', { 'matrix__row--active': row.paper_id === activePaperId }]">
              <th class="matrix__td matrix__td--paper" @click="emit('paper', { paperId: row.paper_id, title: row.title })">
                <div class="matrix__paper-id">#{{ row.paper_id }}</div>
                <div class="matrix__paper-title">{{ row.title || '未获取标题' }}</div>
                <div class="matrix__paper-meta">
                  <span>{{ row.venue || 'venue 未获取' }}</span>
                  <span class="sl-coverage">{{ row.coverage_tag || '覆盖范围未记录' }}</span>
                </div>
              </th>
              <td v-for="dim in dimensions" :key="dim.key" class="matrix__td">
                <template v-if="cellOf(row, dim.key)?.missing">
                  <span class="matrix__missing">材料未提供</span>
                  <span class="matrix__field-note">{{ cellOf(row, dim.key)?.note }}</span>
                </template>
                <template v-else>
                  <p v-for="(text, index) in cellText(cellOf(row, dim.key))" :key="`t${index}`" class="matrix__text">
                    {{ text }}
                  </p>
                  <ol v-if="cellItems(cellOf(row, dim.key)).length" class="matrix__list">
                    <li v-for="(item, index) in cellItems(cellOf(row, dim.key))" :key="index">{{ item }}</li>
                  </ol>
                  <div class="matrix__cell-actions">
                    <button
                      v-if="(cellOf(row, dim.key)?.evidence?.length ?? 0) > 0"
                      type="button"
                      class="matrix__btn"
                      @click="openEvidence(row, dim.key, cellOf(row, dim.key))"
                    >
                      证据 {{ cellOf(row, dim.key)?.evidence?.length }}
                    </button>
                    <button type="button" class="matrix__btn matrix__btn--ghost" @click="toggle(row, dim.key)">
                      {{ isOpen(row, dim.key) ? '收起' : '定位方式' }}
                    </button>
                  </div>
                  <ul v-if="isOpen(row, dim.key)" class="matrix__evidence">
                    <li v-for="(ev, index) in cellOf(row, dim.key)?.evidence" :key="index">
                      <span class="matrix__ev-kind">{{ ev.kind }}</span>
                      <span class="matrix__ev-label">{{ ev.label }}</span>
                      <span v-if="ev.match_coverage !== null && ev.match_coverage !== undefined" class="matrix__ev-cov">
                        覆盖 {{ Math.round(Number(ev.match_coverage) * 100) }}%
                      </span>
                      <a v-if="ev.jump_url" class="matrix__ev-jump" :href="ev.jump_url">{{ ev.jump_url }}</a>
                    </li>
                    <li v-if="cellOf(row, dim.key)?.note" class="matrix__ev-note">
                      备注：{{ cellOf(row, dim.key)?.note }}
                    </li>
                  </ul>
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <p v-if="matrix?.missing?.length" class="matrix__warn">
      有 {{ matrix.missing.length }} 篇论文缺少解析卡片（{{ matrix.missing.map((m) => m.paper_id).join(', ') }}）：
      已标注缺失，未填充占位内容。
    </p>
    <p v-if="matrix?.compliance_note" class="sl-compliance">{{ matrix.compliance_note }}</p>
  </section>
</template>

<style scoped>
.matrix__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
}
.matrix__meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.matrix__scroll {
  overflow-x: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
}
.matrix__table {
  border-collapse: collapse;
  width: 100%;
  min-width: 1080px;
  font-size: var(--font-size-sm);
}
.matrix__th {
  position: sticky;
  top: 0;
  z-index: 1;
  padding: var(--space-2) var(--space-3);
  text-align: left;
  background: var(--color-bg-subtle);
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}
.matrix__th--paper {
  left: 0;
  min-width: 200px;
}
.matrix__field {
  display: block;
  color: var(--color-text-secondary);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
}
.matrix__td {
  padding: var(--space-3);
  vertical-align: top;
  border-bottom: 1px solid var(--color-border);
  border-right: 1px solid var(--color-border);
  min-width: 220px;
  max-width: 320px;
}
.matrix__td--paper {
  position: sticky;
  left: 0;
  z-index: 1;
  background: var(--color-card-bg);
  text-align: left;
  cursor: pointer;
  min-width: 200px;
}
.matrix__row--active .matrix__td,
.matrix__row--active .matrix__td--paper {
  background: var(--color-brand-soft);
}
.matrix__paper-id {
  font-family: var(--font-family-mono);
  color: var(--color-text-secondary);
}
.matrix__paper-title {
  color: var(--color-text-primary);
  font-weight: 600;
  line-height: var(--line-height-tight);
}
.matrix__paper-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.matrix__text {
  margin: 0 0 var(--space-2);
  line-height: var(--line-height-base);
}
.matrix__list {
  margin: 0 0 var(--space-2);
  padding-left: var(--space-4);
}
.matrix__list li {
  margin-bottom: var(--space-1);
}
.matrix__missing {
  display: inline-block;
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
}
.matrix__field-note {
  display: block;
  margin-top: var(--space-1);
  color: var(--color-text-disabled);
  font-size: var(--font-size-xs);
}
.matrix__cell-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-1);
}
.matrix__btn {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.matrix__btn--ghost {
  color: var(--color-text-secondary);
}
.matrix__evidence {
  margin: var(--space-2) 0 0;
  padding-left: var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.matrix__ev-kind {
  margin-right: var(--space-2);
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.matrix__ev-cov {
  margin-left: var(--space-2);
  color: var(--color-text-secondary);
}
.matrix__ev-jump {
  display: block;
  color: var(--color-brand);
  font-family: var(--font-family-mono);
  word-break: break-all;
}
.matrix__ev-note {
  list-style: none;
  margin-top: var(--space-1);
}
.matrix__warn {
  margin-top: var(--space-2);
  color: var(--color-warning);
  font-size: var(--font-size-sm);
}
</style>
