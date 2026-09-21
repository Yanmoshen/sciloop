<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 方法演进时间线（WP08-T2 / 附录 A.3 method_evolution）。
 *
 * 展示纪律：匹配粒度（span / document / card）与置信度（high / medium / low）**必须显示**，
 * 因为 document 级是「术语出现在对方正文里」的弱证据，不能让读者误以为是显式引用；
 * 每条关系的证据条数与定位情况一并展示，点击向上抛 `evidence` 事件。
 */
import { computed } from 'vue'

import type { CellEvidence, EvolutionPayload, EvolutionRelation } from '@/api/idea'

const props = defineProps<{ evolution: EvolutionPayload | null }>()
const emit = defineEmits<{
  (event: 'evidence', payload: { paperId: number; label: string; items: CellEvidence[] }): void
}>()

const relations = computed<EvolutionRelation[]>(() => props.evolution?.relations ?? [])
const timeline = computed(() => props.evolution?.timeline ?? [])

const SCOPE_LABELS: Record<string, string> = {
  span: '单段落命中',
  document: '正文术语命中',
  card: '仅卡片文本',
}
const CONFIDENCE_LABELS: Record<string, string> = {
  high: '高',
  medium: '中',
  low: '低',
}
</script>

<template>
  <section class="evo">
    <header class="evo__head">
      <h3>方法演进</h3>
      <p class="evo__meta">
        <span v-if="evolution">{{ relations.length }} 条承接关系</span>
        <span v-if="evolution?.thresholds">
          · 阈值 hits≥{{ evolution.thresholds.min_hits }} 且覆盖≥{{
            Math.round(Number(evolution.thresholds.min_coverage) * 100)
          }}%
        </span>
      </p>
    </header>

    <el-empty v-if="relations.length === 0" description="本次聚合未识别到达到阈值的承接关系（不编造）" />

    <ol v-else class="evo__list">
      <li v-for="(rel, index) in relations" :key="index" class="evo__item">
        <div class="evo__arrow">
          <span class="evo__paper">#{{ rel.from_paper_id }}</span>
          <span class="evo__paper-title">{{ rel.from_title || '未获取标题' }}</span>
          <span class="evo__glyph">→</span>
          <span class="evo__paper">#{{ rel.to_paper_id }}</span>
          <span class="evo__paper-title">{{ rel.to_title || '未获取标题' }}</span>
        </div>
        <div class="evo__tags">
          <span class="evo__tag">{{ rel.mechanism || '未标注机制' }}</span>
          <span class="evo__tag evo__tag--scope">
            {{ SCOPE_LABELS[String(rel.match_scope)] || rel.match_scope }}
          </span>
          <span class="evo__tag evo__tag--conf">
            置信度 {{ CONFIDENCE_LABELS[String(rel.confidence)] || rel.confidence }}
          </span>
          <span class="evo__tag">
            命中 {{ rel.match_hits }} 词 / 覆盖
            {{ rel.match_coverage !== undefined ? Math.round(Number(rel.match_coverage) * 100) : '—' }}%
          </span>
          <span class="evo__tag" :class="{ 'evo__tag--warn': !rel.span_located }">
            {{ rel.span_located ? '有原文定位' : '无原文定位' }}
          </span>
        </div>
        <p class="evo__change">{{ rel.change }}</p>
        <p v-if="rel.mechanism_basis" class="evo__basis">判定依据：{{ rel.mechanism_basis }}</p>
        <p v-if="rel.matched_terms?.length" class="evo__terms">
          命中实词：<span v-for="term in rel.matched_terms" :key="term" class="evo__term">{{ term }}</span>
        </p>
        <button
          v-if="rel.evidence?.length"
          type="button"
          class="evo__btn"
          @click="emit('evidence', { paperId: rel.to_paper_id, label: `演进 ${rel.from_paper_id}→${rel.to_paper_id}`, items: rel.evidence })"
        >
          查看证据 {{ rel.evidence.length }}
        </button>
      </li>
    </ol>

    <div v-if="timeline.length" class="evo__timeline">
      <h4>线性时间轴</h4>
      <ol>
        <li v-for="item in timeline" :key="item.paper_id">
          <span class="evo__paper">#{{ item.paper_id }}</span>
          <span class="evo__date">{{ item.published_at || '发布日期未获取' }}</span>
          <span class="evo__paper-title">{{ item.title || '未获取标题' }}</span>
          <span class="evo__coverage">{{ item.scope === 'fulltext' ? '全文可用' : '仅摘要' }}</span>
        </li>
      </ol>
    </div>

  </section>
</template>

<style scoped>
.evo__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
}
.evo__meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.evo__list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.evo__item {
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}
.evo__arrow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.evo__paper {
  font-family: var(--font-family-mono);
  color: var(--color-brand);
}
.evo__paper-title {
  color: var(--color-text-primary);
  font-weight: 600;
}
.evo__glyph {
  color: var(--color-text-secondary);
}
.evo__tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: var(--space-2) 0;
}
.evo__tag {
  padding: 0 var(--space-2);
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.evo__tag--scope {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.evo__tag--conf {
  background: var(--color-info-soft);
  color: var(--color-info);
}
.evo__tag--warn {
  background: var(--color-warning-soft);
  color: var(--color-warning);
}
.evo__change {
  margin: 0 0 var(--space-1);
  line-height: var(--line-height-base);
}
.evo__basis,
.evo__terms,
.evo__term {
  display: inline-block;
  margin: 0 var(--space-1) var(--space-1) 0;
  padding: 0 4px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-family: var(--font-family-mono);
}
.evo__btn {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.evo__timeline {
  margin-top: var(--space-4);
}
.evo__timeline ol {
  margin: 0;
  padding-left: var(--space-4);
}
.evo__timeline li {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-1) 0;
  font-size: var(--font-size-sm);
}
.evo__date,
.evo__coverage {
  color: var(--color-text-secondary);
}
</style>
