<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * idea 卡片（WP08-T4）。
 *
 * 展示纪律：
 * - 无证据的 idea 一律打红标「无 Evidence：不得输出 / 不得进入可行性」，
 *   且「生成可行性」按钮禁用（服务端同样会拦，前端只是提前告知）；
 * - `origin` 如实显示 AI 生成 / 手动录入；`novelty_note` 里若写明「模板合成（未调用 LLM）」
 *   必须原样透出，不能让模板结果看起来像模型生成；
 * - 证据数量与范围（fulltext / abstract_only）直接取自后端 gate，组件不推断。
 */
import { computed } from 'vue'

import { MECHANISM_LABELS, ORIGIN_LABELS, type Idea } from '@/api/idea'

const props = defineProps<{
  idea: Idea
  active?: boolean
  feasibilityReady?: boolean
}>()

const emit = defineEmits<{
  (event: 'select', payload: { idea: Idea }): void
  (event: 'evidence', payload: { idea: Idea }): void
  (event: 'feasibility', payload: { idea: Idea }): void
  (event: 'bind', payload: { idea: Idea }): void
  (event: 'select-idea', payload: { idea: Idea }): void
}>()

const scopeLabel = computed(() => {
  const scope = props.idea.gate?.evidence_scope
  if (scope === 'fulltext') return '全文可用'
  if (scope === 'abstract_only') return '证据覆盖范围：仅摘要'
  if (scope === 'mixed') return '证据范围：全文 + 摘要混合'
  return '证据范围未记录'
})
</script>

<template>
  <article :class="['idea', { 'idea--active': active, 'idea--nogev': !idea.has_evidence }]">
    <header class="idea__head">
      <span class="idea__id">#{{ idea.id }}</span>
      <h4 class="idea__title">{{ idea.title }}</h4>
      <span class="idea__tag">{{ ORIGIN_LABELS[idea.origin] || idea.origin }}</span>
      <span v-if="idea.mechanism" class="idea__tag idea__tag--mech">
        {{ MECHANISM_LABELS[String(idea.mechanism)] || idea.mechanism }}
      </span>
      <span v-if="idea.is_selected" class="idea__tag idea__tag--picked">已选中</span>
    </header>

    <p class="idea__content">{{ idea.content }}</p>

    <div class="idea__evidence">
      <span
        class="idea__tag"
        :class="idea.has_evidence ? 'idea__tag--ok' : 'idea__tag--danger'"
      >
        证据 {{ idea.evidence_count }} 条
      </span>
      <span class="idea__tag">{{ scopeLabel }}</span>
      <span v-for="id in idea.evidence_ids.slice(0, 4)" :key="id" class="idea__ev-id">ev#{{ id }}</span>
    </div>

    <p v-if="!idea.has_evidence" class="idea__warn">
      无 Evidence：按 contracts.evidence_rules.ideation_rule 不得输出，也不得进入可行性；请先绑定证据。
    </p>
    <p v-if="idea.novelty_note" class="idea__note">{{ idea.novelty_note }}</p>

    <footer class="idea__actions">
      <button type="button" class="idea__btn" @click="emit('select', { idea })">选中</button>
      <button type="button" class="idea__btn" @click="emit('evidence', { idea })">查看证据</button>
      <button type="button" class="idea__btn" @click="emit('bind', { idea })">绑定证据</button>
      <button
        type="button"
        class="idea__btn idea__btn--primary"
        :disabled="!idea.has_evidence"
        :title="idea.has_evidence ? '生成四维可行性报告' : '无证据不允许进入可行性'"
        @click="emit('feasibility', { idea })"
      >
        进入可行性
      </button>
      <span v-if="feasibilityReady" class="idea__tag idea__tag--ok">已生成可行性</span>
    </footer>
  </article>
</template>

<style scoped>
.idea {
  padding: var(--space-3);
  margin-bottom: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}
.idea--active {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
}
.idea--nogev {
  border-left: 3px solid var(--color-danger);
}
.idea__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.idea__id {
  font-family: var(--font-family-mono);
  color: var(--color-brand);
}
.idea__title {
  margin: 0;
  flex: 1 1 240px;
  font-size: var(--font-size-md);
}
.idea__tag {
  padding: 0 var(--space-2);
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.idea__tag--mech {
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.idea__tag--picked {
  background: var(--color-success-soft);
  color: var(--color-success);
}
.idea__tag--ok {
  background: var(--color-success-soft);
  color: var(--color-success);
}
.idea__tag--danger {
  background: var(--color-danger-soft);
  color: var(--color-danger);
}
.idea__content {
  margin: var(--space-2) 0;
  white-space: pre-wrap;
  line-height: var(--line-height-base);
}
.idea__evidence {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}
.idea__ev-id {
  font-family: var(--font-family-mono);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.idea__warn {
  margin: var(--space-2) 0 0;
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}
.idea__note {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.idea__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}
.idea__btn {
  padding: 1px var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.idea__btn--primary {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
  color: var(--color-brand);
}
.idea__btn:disabled {
  cursor: not-allowed;
  color: var(--color-text-disabled);
  border-color: var(--color-border);
  background: var(--color-bg-muted);
}
</style>
