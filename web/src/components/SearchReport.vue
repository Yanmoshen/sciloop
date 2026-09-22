<script setup lang="ts">
/**
 * 联网检索结果面板（可折叠）。
 *
 * 研究者 2026-09-22 定的版面：
 * - 放在**消息流里那一轮的下方**（不在输入框上方、也不进抽屉）；
 * - 收起时只有一行：`联网搜索 · 21 条`；
 * - 展开后按 **来源 + 这一组实际用的搜索词** 分组（`arXiv · 搜「subword tokenization fairness」`），
 *   每组列出结果**标题与链接**（可点开原页）；
 * - 只用黑白灰，不搞五颜六色；
 * - "哪一层坏了"（服务没起 / 没外网 / 被限流）在**过程行**里说，不塞进这个面板。
 */
import { computed, ref } from 'vue'

import type { SearchReport } from '@/api/chat'

const props = defineProps<{ report: SearchReport }>()

/** 默认收起：它只是"过程"，别抢正文的位置 */
const open = ref(false)

const groups = computed(() => props.report.groups ?? [])
const total = computed(() =>
  groups.value.reduce((sum, group) => sum + (group.results?.length ?? 0), 0),
)

/** 分组标题：`arXiv · 搜「关键词」` —— 来源写具体，后面跟这一组实际用的词 */
function groupHead(group: SearchReport['groups'][number]): string {
  const sources = (group.sources_used ?? []).filter(Boolean)
  const source = sources.length ? sources.join('、') : group.capability === 'academic' ? '学术接口' : '网页'
  const query = (group.query ?? '').trim()
  return query ? `${source} · 搜「${query}」` : source
}

/** 这一组没结果时，把"谁没响应"如实说出来（原因分类在过程行里） */
function failedText(group: SearchReport['groups'][number]): string {
  const failed = (group.sources_failed ?? []).map((item) => item.source).filter(Boolean)
  return failed.length ? `（${failed.join('、')} 没响应）` : ''
}

/** 链接显示成短形态：去掉协议与结尾斜杠，太长就中段省略 */
function prettyUrl(url: string): string {
  const trimmed = (url ?? '').replace(/^https?:\/\//, '').replace(/\/$/, '')
  return trimmed.length > 64 ? `${trimmed.slice(0, 60)}…` : trimmed
}
</script>

<template>
  <div class="sr">
    <button
      class="sr__head"
      type="button"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="sr__caret" aria-hidden="true">{{ open ? '▾' : '▸' }}</span>
      <span class="sr__title">{{ props.report.title }}</span>
    </button>

    <div v-if="open" class="sr__body">
      <p v-if="!groups.length" class="sr__empty">这次没有拿到结果。</p>
      <div v-for="(group, index) in groups" :key="index" class="sr__group">
        <p class="sr__group-head">{{ groupHead(group) }}</p>
        <p v-if="!group.results?.length" class="sr__empty">
          这一组没有结果{{ failedText(group) }}
        </p>
        <a
          v-for="hit in group.results ?? []"
          :key="hit.url"
          class="sr__hit"
          :href="hit.url"
          target="_blank"
          rel="noreferrer noopener"
        >
          <span class="sr__hit-title">{{ hit.title || hit.url }}</span>
          <span class="sr__hit-url">{{ prettyUrl(hit.url) }}</span>
        </a>
      </div>
      <p class="sr__note">共 {{ total }} 条；标题可点开原页核对。</p>
    </div>
  </div>
</template>

<style scoped>
.sr {
  margin: 8px 0;
  border: 1px solid var(--h-line);
  border-radius: var(--radius-md);
  background: var(--h-surface);
}

.sr__head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--h-fg);
  font-family: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.sr__head:hover {
  background: var(--h-hover);
}

.sr__head:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 2px;
}

.sr__caret {
  color: var(--h-fg-muted);
  font-size: 11px;
}

.sr__title {
  font-weight: 500;
}

.sr__body {
  padding: 2px 12px 10px 22px;
  border-top: 1px solid var(--h-line);
}

.sr__group {
  margin-top: 10px;
}

.sr__group-head {
  margin: 0 0 6px;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
  font-weight: 500;
}

.sr__hit {
  display: block;
  padding: 5px 6px;
  border-radius: var(--radius-sm);
  color: var(--h-fg);
  text-decoration: none;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.sr__hit:hover {
  background: var(--h-hover);
}

.sr__hit:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 2px;
}

.sr__hit-title {
  display: block;
  font-size: var(--font-size-sm);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.sr__hit-url {
  display: block;
  color: var(--h-fg-subtle);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.sr__empty {
  margin: 0 0 4px;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
}

.sr__note {
  margin: 10px 0 0;
  color: var(--h-fg-subtle);
  font-size: 12px;
}
</style>
