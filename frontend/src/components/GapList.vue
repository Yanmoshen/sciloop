<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 空白清单（WP08-T3 / WP08-A2：每条含提出者论文 + 未被解决的证据）。
 *
 * 展示纪律：
 * - 每条 Gap 展开后必须能看到「提出者论文」与「原文片段」（真实 paper_span）；
 * - 无 span 只有卡片字段的条目会显示 `evidence_kinds=['card_field']`，如实标注来源类型；
 * - `novelty_hint` 标注为规则生成（novelty_hint_source），避免被误读为模型判断；
 * - `unsolved_scope_note` 原样展示：未解决性只在本次聚合集合内判定（不做反证检索）。
 */
import { computed, ref } from 'vue'

import type { Gap } from '@/api/idea'

const props = defineProps<{
  gaps: Gap[]
  /** 每条的「生成 idea」按钮；父组件负责调用 store */
  selectable?: boolean
}>()

const emit = defineEmits<{
  (event: 'generate', payload: { gap: Gap }): void
}>()

const openId = ref<number | null>(null)

const items = computed(() => props.gaps ?? [])
const scopeNote = computed(() => items.value[0]?.unsolved_scope_note ?? null)

function toggle(id: number): void {
  openId.value = openId.value === id ? null : id
}
</script>

<template>
  <section class="gaps">
    <header class="gaps__head">
      <h3>研究空白</h3>
      <p class="gaps__meta">{{ items.length }} 条 · 每条均带提出者与未见解决的原文证据</p>
    </header>

    <el-empty v-if="items.length === 0" description="本次聚合未抽取到可证据化的空白（不编造）" />

    <ol v-else class="gaps__list">
      <li v-for="gap in items" :key="gap.id" class="gaps__item">
        <div class="gaps__row" @click="toggle(gap.id)">
          <span class="gaps__id">Gap #{{ gap.id }}</span>
          <span class="gaps__papers">提出者 {{ gap.raised_by_paper_ids.join(', ') }}</span>
          <span class="gaps__tag">证据段 {{ gap.span_count ?? gap.unsolved_evidence?.length ?? 0 }}</span>
          <span v-for="kind in gap.evidence_kinds ?? []" :key="kind" class="gaps__tag gaps__tag--kind">{{ kind }}</span>
          <span v-if="gap.merged_count && gap.merged_count > 1" class="gaps__tag">合并 {{ gap.merged_count }} 条</span>
          <span class="gaps__toggle">{{ openId === gap.id ? '收起' : '展开' }}</span>
        </div>

        <p class="gaps__text">{{ gap.gap_text }}</p>

        <div v-if="openId === gap.id" class="gaps__detail">
          <div class="gaps__block">
            <h4>未被解决的证据（真实原文片段）</h4>
            <ol v-if="gap.unsolved_evidence?.length" class="gaps__evidence">
              <li v-for="ev in gap.unsolved_evidence" :key="ev.paper_span_id">
                <div class="gaps__ev-head">
                  <span class="gaps__paper">论文 #{{ ev.paper_id }}</span>
                  <span class="gaps__ev-meta">
                    章节 {{ ev.section_name || '未标注' }} · 定位 {{ ev.locator_kind || '未记录' }}
                    <template v-if="ev.match_coverage !== null && ev.match_coverage !== undefined">
                      · 覆盖 {{ Math.round(Number(ev.match_coverage) * 100) }}%
                    </template>
                  </span>
                  <a class="gaps__jump" :href="`/papers/parse/${ev.paper_id}#span-${ev.paper_span_id}`">
                    /papers/parse/{{ ev.paper_id }}#span-{{ ev.paper_span_id }}
                  </a>
                </div>
                <blockquote class="gaps__quote">{{ ev.quote_text || '（引用文本为空）' }}</blockquote>
              </li>
            </ol>
          </div>

          <div class="gaps__block">
            <h4>提出者论文</h4>
            <ul class="gaps__raised">
              <li v-for="ref in gap.raised_by ?? []" :key="`${ref.paper_id}-${ref.span_id ?? 'card'}`">
                <span class="gaps__paper">#{{ ref.paper_id }}</span>
                <span class="gaps__raised-title">{{ ref.title || '未获取标题' }}</span>
                <span class="gaps__ev-meta">
                  来源 {{ ref.source || '未记录' }}
                  <template v-if="ref.card_field"> · 字段 {{ ref.card_field }}</template>
                  <template v-if="ref.section_name"> · 章节 {{ ref.section_name }}</template>
                </span>
                <blockquote v-if="ref.quote_text" class="gaps__quote">{{ ref.quote_text }}</blockquote>
                <p v-if="ref.note" class="gaps__hint">{{ ref.note }}</p>
              </li>
            </ul>
          </div>

          <div v-if="gap.novelty_hint" class="gaps__block">
            <h4>新颖性提示</h4>
            <p class="gaps__hint">{{ gap.novelty_hint }}</p>
            <p class="gaps__hint gaps__hint--source">来源：{{ gap.novelty_hint_source || '未标注' }}</p>
          </div>

          <p v-if="gap.unsolved_scope_note" class="gaps__scope">{{ gap.unsolved_scope_note }}</p>
        </div>

        <button v-if="props.selectable !== false" type="button" class="gaps__btn" @click="emit('generate', { gap })">
          针对该空白生成 idea
        </button>
      </li>
    </ol>

    <p v-if="scopeNote" class="gaps__scope">{{ scopeNote }}</p>
  </section>
</template>

<style scoped>
.gaps__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
}
.gaps__meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.gaps__list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.gaps__item {
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}
.gaps__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
}
.gaps__id {
  font-family: var(--font-family-mono);
  color: var(--color-brand);
  font-weight: 600;
}
.gaps__papers,
.gaps__toggle {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.gaps__toggle {
  margin-left: auto;
  color: var(--color-brand);
}
.gaps__tag {
  padding: 0 var(--space-2);
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.gaps__tag--kind {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.gaps__text {
  margin: var(--space-2) 0 0;
  line-height: var(--line-height-base);
}
.gaps__detail {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--color-border);
}
.gaps__block {
  margin-bottom: var(--space-3);
}
.gaps__block h4 {
  margin: 0 0 var(--space-2);
  font-size: var(--font-size-sm);
}
.gaps__evidence,
.gaps__raised {
  margin: 0;
  padding-left: var(--space-4);
}
.gaps__ev-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
}
.gaps__paper {
  font-family: var(--font-family-mono);
  color: var(--color-brand);
}
.gaps__ev-meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.gaps__jump {
  color: var(--color-brand);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
}
.gaps__quote {
  margin: var(--space-1) 0 var(--space-2);
  padding: var(--space-2);
  border-left: 3px solid var(--color-border-strong);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  line-height: var(--line-height-base);
}
.gaps__hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.gaps__hint--source {
  color: var(--color-text-disabled);
}
.gaps__scope {
  margin: var(--space-2) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.gaps__btn {
  margin-top: var(--space-2);
  padding: 1px var(--space-3);
  border: 1px solid var(--color-brand);
  border-radius: var(--radius-pill);
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}
</style>
