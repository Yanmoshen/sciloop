<script setup lang="ts">
/**
 * 顶栏全局搜索：**只搜项目与对话（按标题）**。
 *
 * 口径（研究者 2026-09-25 定稿）：
 * - **不做论文搜索** —— 这里原来是「搜索论文标题 / 摘要」，回车跳到论文库，已下线；
 * - 匹配**标题**：项目名 + 对话标题，大小写不敏感的子串匹配；
 * - 结果**同一个列表按最近修改时间倒序**（越近越靠上），不分组；
 * - 每行右侧**浅色小字**标归属：`项目` / `未分组对话` / 项目内对话标**所属项目名**；
 * - 交互：↑↓ 移动 + 回车打开（默认第一条）、Esc 先关下拉再清空、
 *   鼠标悬停高亮、点击打开、点空白处关闭；
 * - 选中后：**对话→直接打开**；**项目→在左栏展开并高亮定位**（不跳走）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type { ConversationBrief } from '@/api/conversations'
import { useConversationStore } from '@/stores/conversations'
import { useSessionStore } from '@/stores/session'

const emit = defineEmits<{
  openConversation: [conversation: ConversationBrief]
  revealProject: [projectId: number]
}>()

const conversations = useConversationStore()
const session = useSessionStore()

const query = ref('')
const focused = ref(false)
const activeIndex = ref(0)
const rootRef = ref<HTMLElement | null>(null)

/** 最多几条：下拉不该盖住整屏，也不该让人滚 */
const MAX_ROWS = 8

interface Row {
  key: string
  kind: 'project' | 'conversation'
  title: string
  /** 行右侧的浅色归属小字 */
  hint: string
  at: number
  conversation?: ConversationBrief
  projectId?: number
}

function projectName(projectId: number | null | undefined): string | null {
  if (projectId === null || projectId === undefined) return null
  return session.projects.find((p) => p.id === projectId)?.name ?? null
}

/**
 * 项目的"最近修改"从**它下面对话的最新修改时间**推出来 ——
 * 项目对象本身没有 updated_at（`CreatedProject` 只有 id/name/status/mode/is_demo）。
 * 一个对话都没有的项目排到最后（时间为 0）。
 */
function projectRecency(projectId: number): number {
  let max = 0
  for (const c of conversations.items) {
    if (c.project_id !== projectId) continue
    const t = Date.parse(c.updated_at ?? '') || 0
    if (t > max) max = t
  }
  return max
}

const rows = computed<Row[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return []
  const out: Row[] = []

  for (const c of conversations.items) {
    const title = c.title ?? '未命名对话'
    if (!title.toLowerCase().includes(q)) continue
    const parent = projectName(c.project_id)
    out.push({
      key: `c-${c.id}`,
      kind: 'conversation',
      title,
      hint: parent ?? '未分组对话',
      at: Date.parse(c.updated_at ?? '') || 0,
      conversation: c,
    })
  }

  for (const p of session.projects) {
    if (!p.name.toLowerCase().includes(q)) continue
    out.push({
      key: `p-${p.id}`,
      kind: 'project',
      title: p.name,
      hint: '项目',
      at: projectRecency(p.id),
      projectId: p.id,
    })
  }

  return out.sort((a, b) => b.at - a.at).slice(0, MAX_ROWS)
})

const listOpen = computed(() => focused.value && query.value.trim().length > 0)

/** 命中片段单独切出来给模板加粗（不用 v-html，避免把标题当 HTML 解析） */
function splitMatch(title: string): [string, string, string] {
  const q = query.value.trim()
  if (!q) return [title, '', '']
  const at = title.toLowerCase().indexOf(q.toLowerCase())
  if (at === -1) return [title, '', '']
  return [title.slice(0, at), title.slice(at, at + q.length), title.slice(at + q.length)]
}

/**
 * 打字一律认为"要用搜索"：**光靠 @focus 不够** ——
 * 点了页面别处会把 focused 置 false，但输入框可能仍保持 DOM 焦点；
 * 这时再打字不会触发 focus 事件，下拉就永远不出来（实测踩到）。
 */
function onInput(): void {
  activeIndex.value = 0
  focused.value = true
}

function move(delta: number): void {
  if (rows.value.length === 0) return
  const next = activeIndex.value + delta
  activeIndex.value = Math.max(0, Math.min(rows.value.length - 1, next))
}

function closeAndClear(): void {
  query.value = ''
  activeIndex.value = 0
  focused.value = false
}

function choose(index: number): void {
  const row = rows.value[index]
  if (!row) return
  if (row.kind === 'project' && row.projectId !== undefined) {
    emit('revealProject', row.projectId)
    closeAndClear()
    return
  }
  if (row.conversation) {
    emit('openConversation', row.conversation)
    closeAndClear()
  }
}

/** Esc：下拉开着就先关下拉；已经关了（或没结果）再清空输入 */
function onEscape(): void {
  if (listOpen.value) {
    focused.value = false
    return
  }
  closeAndClear()
}

function onDocumentClick(event: MouseEvent): void {
  const root = rootRef.value
  if (!root) return
  if (!root.contains(event.target as Node)) focused.value = false
}

onMounted(() => document.addEventListener('click', onDocumentClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocumentClick))
</script>

<template>
  <div ref="rootRef" class="gsearch">
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="7" cy="7" r="4.6" stroke="currentColor" stroke-width="1.4" />
      <path
        d="M10.6 10.6 14 14"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
      />
    </svg>
    <input
      v-model="query"
      type="search"
      placeholder="搜索项目或对话"
      aria-label="搜索项目或对话"
      role="combobox"
      aria-controls="gsearch-list"
      :aria-expanded="listOpen"
      autocomplete="off"
      @focus="focused = true"
      @input="onInput"
      @keydown.down.prevent="move(1)"
      @keydown.up.prevent="move(-1)"
      @keydown.enter.prevent="choose(activeIndex)"
      @keydown.esc.prevent="onEscape"
    />

    <div v-if="listOpen" id="gsearch-list" class="gsearch__list" role="listbox">
      <p v-if="rows.length === 0" class="gsearch__empty">没有匹配的项目或对话</p>
      <button
        v-for="(row, index) in rows"
        v-else
        :key="row.key"
        class="gsearch__row"
        :class="{ 'gsearch__row--on': index === activeIndex }"
        type="button"
        role="option"
        :aria-selected="index === activeIndex"
        @mouseenter="activeIndex = index"
        @click="choose(index)"
      >
        <span class="gsearch__title">
          <template v-for="(part, partIndex) in splitMatch(row.title)" :key="partIndex">
            <b v-if="partIndex === 1">{{ part }}</b>
            <template v-else>{{ part }}</template>
          </template>
        </span>
        <span class="gsearch__hint">{{ row.hint }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
/* 输入框沿用顶栏原来那套视觉（原来是内联在 HomeLayout 里的 .search） */
.gsearch {
  position: relative;
  flex: 1;
  max-width: 560px;
  display: flex;
  align-items: center;
  gap: 12px;
  height: 40px;
  padding: 0 16px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  border-radius: 12px;
  color: var(--h-fg-subtle);
}

.gsearch:focus-within {
  border-color: var(--h-line-strong);
}

.gsearch input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  outline: none;
}

.gsearch input::placeholder {
  color: var(--h-fg-subtle);
}

/* 下拉：定位在输入框正下方，宽度跟输入框一致 */
.gsearch__list {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  z-index: 30;
  max-height: 320px;
  overflow-y: auto;
  padding: 6px;
  background: var(--h-surface-raised);
  border: 1px solid var(--h-line-strong);
  border-radius: 12px;
  box-shadow: var(--shadow-popover);
}

.gsearch__empty {
  margin: 0;
  padding: 10px 12px;
  color: var(--h-fg-subtle);
  font-size: var(--font-size-sm);
}

.gsearch__row {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
}

.gsearch__row--on {
  background: var(--h-hover);
}

/* 标题允许省略：长标题不该把右边的归属小字挤没 */
.gsearch__title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gsearch__title b {
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 2px;
}

.gsearch__hint {
  flex: none;
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--h-fg-subtle);
  font-size: var(--font-size-xs);
}
</style>
