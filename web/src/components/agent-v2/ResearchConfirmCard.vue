<script setup lang="ts">
/**
 * 研究意图确认卡（WP-05）。
 *
 * 研究者说出研究意图时，**先确认再执行**：确认前不得出现任何「检索中/执行中」的进度，
 * 因为那时还没有任何真实动作发生。确认后才由执行层产生真实事件。
 */
import { computed, ref } from 'vue'

import type { ResearchIntent } from './blocks'

const props = defineProps<{
  intent: ResearchIntent
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'confirm', payload: ResearchIntent): void
  (event: 'cancel'): void
}>()

const question = ref(props.intent.question)
const scope = ref(props.intent.scope ?? '')
const sources = ref<string[]>([...(props.intent.sources ?? [])])
const expanded = ref(false)

const canConfirm = computed(() => question.value.trim().length > 0 && !props.busy)

function toggleSource(name: string): void {
  sources.value = sources.value.includes(name)
    ? sources.value.filter((item) => item !== name)
    : [...sources.value, name]
}

function confirm(): void {
  if (!canConfirm.value) return
  emit('confirm', {
    question: question.value.trim(),
    scope: scope.value.trim() || null,
    sources: [...sources.value],
    budget: props.intent.budget ?? null,
  })
}
</script>

<template>
  <article class="confirm">
    <header class="confirm__head">
      <span class="confirm__title">确认研究意图</span>
      <span class="confirm__hint">确认后才开始检索</span>
    </header>

    <label class="field">
      <span class="field__label">研究问题</span>
      <textarea v-model="question" class="field__input" rows="2" :disabled="busy" />
    </label>

    <label class="field">
      <span class="field__label">范围 / 约束</span>
      <input v-model="scope" class="field__input" type="text" :disabled="busy" placeholder="例如：近三年、只看中文期刊" />
    </label>

    <div class="confirm__sources">
      <button type="button" class="link" @click="expanded = !expanded">
        {{ expanded ? '收起检索源' : '调整检索源' }}
      </button>
      <div v-if="expanded" class="chips">
        <button
          v-for="name in ['arXiv', 'Semantic Scholar', 'OpenAlex', 'GitHub', '知识库']"
          :key="name"
          type="button"
          class="chip"
          :class="{ 'chip--on': sources.includes(name) }"
          @click="toggleSource(name)"
        >
          {{ name }}
        </button>
      </div>
    </div>

    <div class="confirm__actions">
      <button type="button" class="btn btn--primary" :disabled="!canConfirm" @click="confirm">
        确认并开始
      </button>
      <button type="button" class="btn" :disabled="busy" @click="emit('cancel')">取消</button>
    </div>
  </article>
</template>

<style scoped>
.confirm {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: var(--color-bg-elevated);
  box-shadow: var(--shadow-card);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.confirm__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.confirm__title {
  font-size: var(--font-size-md);
  color: var(--color-text-primary);
}

.confirm__hint {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
  margin-left: auto;
}

.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.field__label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.field__input {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font-family: var(--font-family-base);
  font-size: var(--font-size-sm);
  padding: var(--space-2);
  resize: vertical;
}

.field__input:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}

.confirm__sources {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  align-items: flex-start;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

.chip {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-2xs);
  padding: 2px var(--space-2);
  cursor: pointer;
}

.chip--on {
  border-color: var(--color-brand);
  color: var(--color-brand);
  background: var(--color-brand-soft);
}

.confirm__actions {
  display: flex;
  gap: var(--space-2);
}

.btn {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  padding: var(--space-1) var(--space-3);
  cursor: pointer;
}

.btn:disabled {
  color: var(--color-text-disabled);
  cursor: not-allowed;
}

.btn--primary {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.link {
  border: 0;
  padding: 0;
  background: none;
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}

.link:hover,
.link:focus-visible {
  text-decoration: underline;
}
</style>
