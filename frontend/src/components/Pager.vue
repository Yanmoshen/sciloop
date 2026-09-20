<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 共用分页器：`上一页 / 1 2 … 6 7 8 … 39 / 下一页` + **页码输入框（回车跳转）**。
 *
 * 为什么抽组件：文献总览**上下两处**都要同一套分页（检索行右侧 + 列表底部），
 * 两处各写一遍必然漂移（行为/样式/边界处理都会不一致）。
 *
 * 边界口径：
 * - 页码一律夹到 `[1, pageCount]`，越界输入不报错、直接落到最近的有效页；
 * - 首页 / 末页对应按钮禁用；`disabled` 为真（加载中）时全部禁用，避免连点翻页竞态；
 * - 页码按钮按「首尾 + 当前页左右各 WINDOW 个」取，中间断开处插省略号。
 */
import { computed, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    page: number
    pageCount: number
    /** 加载中 / 不可交互时整体禁用 */
    disabled?: boolean
  }>(),
  { disabled: false },
)

const emit = defineEmits<{ change: [page: number] }>()

/** 当前页左右各保留的页数 */
const WINDOW = 2

const normalizedPage = computed(() => clamp(props.page))
const jumpDraft = ref(String(normalizedPage.value))

// 外部翻页后把输入框同步回真实页码（用户可能输了个越界值）
watch(normalizedPage, (value) => {
  jumpDraft.value = String(value)
})

function clamp(value: number): number {
  const total = Math.max(1, Math.trunc(props.pageCount) || 1)
  const next = Math.trunc(Number(value))
  if (!Number.isFinite(next)) return 1
  return Math.min(Math.max(next, 1), total)
}

/** 首尾 + 当前页窗口；中间断开处插一个省略号 */
const pages = computed<Array<number | 'gap'>>(() => {
  const total = Math.max(1, Math.trunc(props.pageCount) || 1)
  const current = normalizedPage.value
  const picked = new Set<number>([1, total])
  for (let offset = -WINDOW; offset <= WINDOW; offset += 1) {
    const value = current + offset
    if (value >= 1 && value <= total) picked.add(value)
  }
  const sorted = [...picked].sort((left, right) => left - right)
  const out: Array<number | 'gap'> = []
  let previous = 0
  sorted.forEach((value) => {
    if (previous !== 0 && value - previous > 1) out.push('gap')
    out.push(value)
    previous = value
  })
  return out
})

function go(target: number): void {
  if (props.disabled) return
  const next = clamp(target)
  if (next === normalizedPage.value) return
  emit('change', next)
}

function submitJump(): void {
  go(clamp(Number(jumpDraft.value)))
  // 无论是否真的跳转，都把输入框归位到当前页，避免留着一个越界值
  jumpDraft.value = String(normalizedPage.value)
}
</script>

<template>
  <nav class="pager" aria-label="分页">
    <button
      class="pager__step"
      type="button"
      :disabled="disabled || normalizedPage <= 1"
      @click="go(normalizedPage - 1)"
    >
      上一页
    </button>

    <template v-for="(item, index) in pages" :key="`p-${item}-${index}`">
      <span v-if="item === 'gap'" class="pager__gap" aria-hidden="true">…</span>
      <button
        v-else
        class="pager__page"
        :class="{ 'is-current': item === normalizedPage }"
        type="button"
        :aria-current="item === normalizedPage ? 'page' : undefined"
        :disabled="disabled"
        @click="go(item)"
      >
        {{ item }}
      </button>
    </template>

    <button
      class="pager__step"
      type="button"
      :disabled="disabled || normalizedPage >= Math.max(1, pageCount)"
      @click="go(normalizedPage + 1)"
    >
      下一页
    </button>

    <label class="pager__jump">
      第
      <input
        v-model="jumpDraft"
        class="pager__input"
        type="text"
        inputmode="numeric"
        aria-label="跳转到页码"
        :disabled="disabled"
        @keyup.enter="submitJump"
      />
      页
    </label>
  </nav>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex-wrap: wrap;
}

.pager__step,
.pager__page {
  height: 32px;
  padding: 0 var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.pager__page {
  min-width: 32px;
}

.pager__step {
  padding: 0 var(--space-3);
}

.pager__step:hover:not(:disabled),
.pager__page:hover:not(:disabled) {
  border-color: var(--color-brand);
  color: var(--color-brand);
}

.pager__step:disabled,
.pager__page:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.pager__page.is-current {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.pager__page.is-current:hover {
  color: var(--color-text-inverse);
}

.pager__gap {
  padding: 0 var(--space-1);
  color: var(--color-text-secondary);
}

.pager__jump {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  margin-left: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

.pager__input {
  width: 56px;
  height: 32px;
  padding: 0 var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: center;
}

.pager__input:focus {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}
</style>
