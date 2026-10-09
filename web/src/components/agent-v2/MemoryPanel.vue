<script setup lang="ts">
/**
 * 记忆面板：用户 / 项目 / 对话三级记忆的查看、编辑与删除。
 *
 * 服务端按作用域严格隔离目录；这里只按当前选中的作用域取数，不做跨域聚合。
 */
import { computed, ref, watch } from 'vue'

import type { MemoryRecordView } from '../../agent-v2/protocol'

type Scope = 'user' | 'project' | 'conversation'

const props = defineProps<{
  /** `${scope}:${scopeId}` -> 该作用域的记忆列表（服务端严格隔离，前端不做跨域聚合）。 */
  recordsByScope: Record<string, MemoryRecordView[]>
  threadId: string | null
  projectId?: string | null
  userId?: string | null
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'load', payload: { scope: Scope; scopeId: string; includeDeleted: boolean }): void
  (event: 'save', payload: { scope: Scope; scopeId: string; text: string; memoryId?: string }): void
  (event: 'remove', payload: { memoryId: string; scopeId: string }): void
}>()

const SCOPE_LABEL: Record<Scope, string> = {
  user: '用户记忆',
  project: '项目记忆',
  conversation: '对话记忆',
}

const scope = ref<Scope>('conversation')
const includeDeleted = ref(false)
const draft = ref('')
const editingId = ref<string | null>(null)

const scopeId = computed(() => {
  if (scope.value === 'conversation') return props.threadId ?? ''
  if (scope.value === 'project') return props.projectId ?? 'default'
  return props.userId ?? 'default'
})

const memoryKey = computed(() => `${scope.value}:${scopeId.value}`)
const visible = computed(() => props.recordsByScope[memoryKey.value] ?? [])

function reload(): void {
  if (!scopeId.value) return
  emit('load', { scope: scope.value, scopeId: scopeId.value, includeDeleted: includeDeleted.value })
}

watch([scope, includeDeleted, () => props.threadId], reload, { immediate: true })

function submit(): void {
  const text = draft.value.trim()
  if (!text || !scopeId.value) return
  emit('save', {
    scope: scope.value,
    scopeId: scopeId.value,
    text,
    memoryId: editingId.value ?? undefined,
  })
  draft.value = ''
  editingId.value = null
}

function startEdit(record: MemoryRecordView): void {
  editingId.value = record.memory_id
  draft.value = record.text
}
</script>

<template>
  <section class="memory">
    <header class="memory__head">
      <div class="tabs">
        <button
          v-for="key in (['conversation', 'project', 'user'] as Scope[])"
          :key="key"
          type="button"
          class="tab"
          :class="{ 'tab--active': scope === key }"
          @click="scope = key"
        >
          {{ SCOPE_LABEL[key] }}
        </button>
      </div>
      <label class="toggle" :title="'包含已删除记录（软删除，文件保留以便审计）'">
        <input v-model="includeDeleted" type="checkbox" />
        <span>含已删除</span>
      </label>
    </header>

    <p class="memory__scope mono" :title="'作用域标识'">{{ scope }} / {{ scopeId || '—' }}</p>

    <div class="memory__editor">
      <textarea
        v-model="draft"
        rows="3"
        class="memory__input"
        :placeholder="editingId ? '修改这条记忆' : '新增一条记忆'"
      />
      <div class="memory__editor-actions">
        <button type="button" class="btn btn--primary" :disabled="busy || !scopeId" @click="submit">
          {{ editingId ? '保存修改' : '写入记忆' }}
        </button>
        <button v-if="editingId" type="button" class="btn" @click="editingId = null; draft = ''">
          取消
        </button>
      </div>
    </div>

    <ul class="memory__list">
      <li v-for="record in visible" :key="record.memory_id" class="item" :class="{ 'item--deleted': record.deleted }">
        <div class="item__head">
          <span class="tag" :title="record.origin === 'user' ? '研究者写入，自动流程不得覆盖' : '自动写入'">
            {{ record.origin === 'user' ? '研究者' : '自动' }}
          </span>
          <span class="item__version">v{{ record.version }}</span>
          <span v-if="record.deleted" class="tag tag--danger">已删除</span>
          <span class="item__meta">{{ record.updated_at }}</span>
        </div>
        <p class="item__text">{{ record.text }}</p>
        <div class="item__actions">
          <button type="button" class="link" @click="startEdit(record)">编辑</button>
          <button
            type="button"
            class="link link--danger"
            :disabled="record.deleted"
            @click="emit('remove', { memoryId: record.memory_id, scopeId })"
          >
            删除
          </button>
        </div>
      </li>
      <li v-if="!visible.length" class="memory__empty">该作用域暂无记忆</li>
    </ul>
  </section>
</template>

<style scoped>
.memory {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.memory__head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.tabs {
  display: flex;
  gap: var(--space-1);
  background: var(--color-bg-muted);
  border-radius: var(--radius-pill);
  padding: 2px;
}

.tab {
  border: 0;
  border-radius: var(--radius-pill);
  background: none;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  padding: 2px var(--space-3);
  cursor: pointer;
}

.tab--active {
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  box-shadow: var(--shadow-card);
}

.toggle {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.memory__scope {
  margin: 0;
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.memory__editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.memory__input {
  width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font-size: var(--font-size-xs);
  padding: var(--space-2);
  resize: vertical;
}

.memory__input:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}

.memory__editor-actions {
  display: flex;
  gap: var(--space-2);
}

.memory__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.item {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.item--deleted {
  opacity: 0.6;
}

.item__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.item__meta {
  margin-left: auto;
}

.item__text {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  white-space: pre-wrap;
}

.item__actions {
  display: flex;
  gap: var(--space-3);
}

.tag {
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
}

.tag--danger {
  color: var(--color-danger);
  background: var(--color-danger-soft);
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

.link--danger {
  color: var(--color-danger);
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
  padding: 3px var(--space-3);
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

.memory__empty {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.mono {
  font-family: var(--font-family-mono);
}
</style>
