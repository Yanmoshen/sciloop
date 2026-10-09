<script setup lang="ts">
/**
 * 压缩面板：压缩进度与结果、摘要查看 / 编辑 / 恢复。
 *
 * 失败时明确说明「原上下文保持不变」——压缩失败不留半成品状态。
 */
import { computed, ref } from 'vue'

import type { CompactionRunView, SummaryView } from '../../agent-v2/protocol'

const props = defineProps<{
  runs: CompactionRunView[]
  summaries: SummaryView[]
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'compact'): void
  (event: 'edit', payload: { summaryId: string; text: string }): void
  (event: 'restore', summaryId: string): void
  (event: 'refresh'): void
}>()

const editingId = ref<string | null>(null)
const draft = ref('')

const activeSummary = computed(() => props.summaries.find((item) => item.active) ?? null)
const lastRun = computed(() => props.runs.at(-1) ?? null)
const phase = computed(() => {
  const run = lastRun.value
  if (!run) return 'idle'
  const any = run as unknown as { phase?: string }
  return any.phase ?? (run.ok ? 'completed' : 'failed')
})

function startEdit(summary: SummaryView): void {
  editingId.value = summary.summary_id
  draft.value = summary.text ?? ''
}

function submitEdit(): void {
  if (!editingId.value) return
  emit('edit', { summaryId: editingId.value, text: draft.value })
  editingId.value = null
}
</script>

<template>
  <section class="compact">
    <header class="compact__head">
      <span class="compact__title">上下文压缩</span>
      <span class="compact__meta" :title="`最近一次压缩：覆盖至序号 ${lastRun?.covered_until ?? '-'}`">
        {{ lastRun ? `覆盖至 ${lastRun.covered_until ?? '-'}` : '尚未压缩' }}
      </span>
      <button type="button" class="link" :disabled="busy" @click="emit('compact')">立即压缩</button>
      <button type="button" class="link" @click="emit('refresh')">刷新摘要</button>
    </header>

    <p v-if="lastRun && lastRun.ok" class="compact__ok">
      已压缩：{{ lastRun.tokens_before }} → {{ lastRun.tokens_after }} tokens（触发方式 {{ lastRun.trigger }}）
    </p>
    <p v-else-if="lastRun && lastRun.error" class="compact__fail">
      压缩未完成，原上下文保持不变，可以重试。
    </p>

    <p v-if="phase === 'started'" class="compact__running">正在压缩…</p>

    <div v-if="summaries.length" class="compact__list">
      <article v-for="summary in summaries" :key="summary.summary_id" class="summary">
        <header class="summary__head">
          <span v-if="summary.active" class="tag tag--active">生效中</span>
          <span v-if="summary.edited" class="tag">已修订</span>
          <span class="summary__meta">{{ summary.trigger }} · 覆盖至 {{ summary.covered_until ?? '-' }}</span>
        </header>
        <template v-if="editingId === summary.summary_id">
          <textarea v-model="draft" class="summary__editor" rows="6" />
          <div class="summary__actions">
            <button type="button" class="btn btn--primary" @click="submitEdit">保存摘要</button>
            <button type="button" class="btn" @click="editingId = null">取消</button>
          </div>
        </template>
        <template v-else>
          <p class="summary__text">{{ summary.text }}</p>
          <div class="summary__actions">
            <button type="button" class="link" @click="startEdit(summary)">编辑</button>
            <button
              type="button"
              class="link"
              :disabled="summary.active"
              @click="emit('restore', summary.summary_id)"
            >
              恢复为生效摘要
            </button>
          </div>
        </template>
      </article>
    </div>
    <p v-else-if="activeSummary === null && !summaries.length" class="compact__empty">本会话还没有摘要</p>
  </section>
</template>

<style scoped>
.compact {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.compact__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.compact__title {
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.compact__meta {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
  margin-left: auto;
}

.compact__ok {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-success);
}

.compact__fail {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-danger);
}

.compact__running {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-brand);
}

.compact__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.summary {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.summary__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.summary__meta {
  margin-left: auto;
}

.summary__text {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  white-space: pre-wrap;
  max-height: 160px;
  overflow: auto;
}

.summary__editor {
  width: 100%;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
  padding: var(--space-2);
  resize: vertical;
}

.summary__actions {
  display: flex;
  gap: var(--space-3);
}

.tag {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
}

.tag--active {
  color: var(--color-success);
  background: var(--color-success-soft);
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

.link:disabled {
  color: var(--color-text-disabled);
  cursor: default;
  text-decoration: none;
}

.btn {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  font-size: var(--font-size-xs);
  padding: 2px var(--space-2);
  cursor: pointer;
}

.btn--primary {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.compact__empty {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
</style>
