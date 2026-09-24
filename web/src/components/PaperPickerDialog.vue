<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 选文弹窗（解析首屏「解析论文」用）：**整块复用论文库的搜索模块**（PaperTable 的选择模式），
 * 底部只放四个动作：单篇解析 / 多篇聚合解析 / 查看已选 / 关闭。
 *
 * 为什么复用而不是另写一个搜索：论文库那个模块已包含关键词/领域/来源/解析状态筛选、排序、
 * 服务端分页与**跨页选择**（用户实测过的痛点：翻页丢失已选）。另写一份必然走样。
 *
 * 可用规则（2026-09-24 用户口径）：
 * - 「单篇解析」**选 1 篇及以上**都能点 —— 选多篇时是**每篇各自独立解析一次**
 *   （按钮文案相应变成「逐篇解析」，底层 `buildCardsForSelected` 本就是逐篇 + 并发 3）；
 * - 「多篇聚合解析」仍需 2 篇及以上 —— 聚合的语义就是"多篇放一起比"，1 篇没有可比的第二篇；
 * - 两个动作提交后**都自动关窗**（进度看首屏「最近解析」的状态图标）；
 * - 「查看已选」是叠在弹窗之上的浮层（`PaperTable` 的 `.picked-overlay`）。
 */
import { computed, ref } from 'vue'

import PaperTable from '@/components/PaperTable.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  /** 已提交解析任务：复用方据此立刻刷新列表并开始轮询（否则要等一轮才发现） */
  (e: 'submitted'): void
}>()

const table = ref<InstanceType<typeof PaperTable> | null>(null)

const selectedCount = computed(() => table.value?.selectedCount ?? 0)
// 用户 2026-09-24 口径：**选多篇也能「单篇解析」** —— 每篇各自独立解析一次。
// 底层 `buildCardsForSelected` 本来就是"逐篇建卡 + 并发 3 + 跑完汇总"，
// 之前只是被这里 "=== 1" 的判断挡住了。
const canSingle = computed(() => selectedCount.value >= 1)
const canAggregate = computed(() => selectedCount.value >= 2)

/** 选 1 篇就是单篇解析；选多篇时动作变成"逐篇解析"，按钮文案跟着说清楚 */
const singleLabel = computed(() => (selectedCount.value > 1 ? '逐篇解析' : '单篇解析'))

function close(): void {
  emit('update:open', false)
}

async function runSingle(): Promise<void> {
  if (!canSingle.value) return
  await table.value?.buildCardsForSelected()
  // 提交完就退出这个界面（用户口径）；进度看首屏「最近解析」的状态图标
  emit('submitted')
  close()
}

async function runAggregate(): Promise<void> {
  if (!canAggregate.value) return
  await table.value?.aggregateSelected()
  emit('submitted')
  close()
}
</script>

<template>
  <Teleport to="body">
    <div v-if="props.open" class="picker-overlay" @click.self="close">
      <section class="picker" role="dialog" aria-modal="true" aria-label="选择论文">
        <header class="picker__head">
          <h2 class="picker__title">选择论文</h2>
          <span class="picker__spacer" />
          <button class="picker__close" type="button" aria-label="关闭" @click="close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path
                d="M3.5 3.5l7 7M10.5 3.5l-7 7"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
              />
            </svg>
          </button>
        </header>

        <div class="picker__body scroll-y">
          <PaperTable ref="table" select-mode />
        </div>

        <footer class="picker__foot">
          <span class="picker__count">已选 {{ selectedCount }} 篇</span>
          <span class="picker__spacer" />
          <button class="pk-btn" type="button" :disabled="!canSingle" @click="runSingle">
            {{ singleLabel }}
          </button>
          <button
            class="pk-btn pk-btn--primary"
            type="button"
            :disabled="!canAggregate"
            @click="runAggregate"
          >
            多篇聚合解析
          </button>
          <button class="pk-btn" type="button" @click="table?.openPicked()">查看已选</button>
          <button class="pk-btn" type="button" @click="close">关闭</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.picker-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  /* 与 PaperImportDialog / ProjectCreateDialog 同口径：遮罩色与主题解耦，深色下加深。
     必须带 backdrop blur —— 不带的话底层页面的标题会透上来、跟弹窗标题糊在一起。 */
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .picker-overlay {
  background: rgba(0, 0, 0, 0.55);
}

.picker {
  display: flex;
  flex-direction: column;
  width: min(960px, 100%);
  height: min(820px, 100%);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg);
  animation: picker-in 180ms cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes picker-in {
  from {
    opacity: 0;
    transform: translateY(8px) scale(0.985);
  }
}

@media (prefers-reduced-motion: reduce) {
  .picker {
    animation: none;
  }
}

.picker__head,
.picker__foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
}

.picker__head {
  border-bottom: 1px solid var(--color-border);
}

.picker__foot {
  border-top: 1px solid var(--color-border);
}

.picker__title {
  margin: 0;
  font-size: var(--font-size-md);
}

.picker__count {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}

.picker__spacer {
  flex: 1;
}

.picker__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.picker__close:hover {
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
}

.picker__close:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.picker__body {
  flex: 1;
  min-height: 0;
  padding: var(--space-3);
  overflow-y: auto;
}

.pk-btn {
  padding: 5px 12px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.pk-btn:hover:not(:disabled) {
  background: var(--color-bg-subtle);
}

.pk-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.pk-btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.pk-btn--primary {
  border-color: var(--color-brand);
  color: var(--color-brand);
}
</style>
