<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * ComplianceBanner —— 合规强制元素（`docs/README-工程.md` §7）。
 *
 * 契约要求：**页脚 + 所有产出物顶部固定**「本内容由 AI 辅助生成，需研究者自行核验」。
 * 页脚由 `ShellLayout` 常驻渲染；本组件负责「产出物顶部」，因此：
 *
 * - `variant="top"`：产出物顶部固定横幅（`position: sticky`，随滚动常驻在顶部栏下方）；
 * - `variant="inline"`：产出物区块内部的合规脚注（如草稿、卡片、报告正文末尾），
 *   也用于 `ShellLayout` 的页脚固定条（此时传 `note=""` 收成单行，避免撑破固定高度）。
 *
 * 纪律：颜色一律引用 `styles/tokens.css` 变量，组件内不出现硬编码色值。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    variant?: 'top' | 'inline'
    /** 产出物名称，用于说明这条合规提示约束的是哪一份产出（如「解析卡片」「研究草稿」） */
    context?: string
    /** 追加的合规说明（如「回放结果已标 is_replay=true」）；传空串则不渲染该补充行 */
    note?: string | null
    /** 固定文案，一般无需覆盖 */
    text?: string
  }>(),
  {
    variant: 'top',
    context: '',
    note: null,
    text: '本内容由 AI 辅助生成，需研究者自行核验',
  },
)

const REQUIRED_NOTE = '产出物为研究草稿，不构成可自动投稿的论文；AI 执行、人保有否决权与最终判断。'

/** `note=null` → 用契约默认补充说明；`note=''` → 明确不渲染（页脚单行场景） */
const resolvedNote = computed(() => props.note ?? REQUIRED_NOTE)
</script>

<template>
  <div
    class="compliance"
    :class="variant === 'top' ? 'compliance--top' : 'compliance--inline'"
    role="note"
    aria-label="AI 辅助生成合规提示"
    data-testid="compliance-banner"
  >
    <span class="compliance__mark" aria-hidden="true">AI</span>
    <span class="compliance__text">{{ text }}</span>
    <span v-if="context" class="compliance__context">（{{ context }}）</span>
  </div>
</template>

<style scoped>
.compliance {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-warning);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  line-height: var(--line-height-base);
}

.compliance--top {
  position: sticky;
  top: var(--layout-header-height);
  z-index: calc(var(--z-header) - 1);
}

.compliance--inline {
  border-left-width: 1px;
  border-left-color: var(--color-border);
}

.compliance__mark {
  flex: none;
  padding: 0 var(--space-1);
  border: 1px solid var(--color-warning);
  border-radius: var(--radius-sm);
  background-color: var(--color-warning-soft);
  color: var(--color-warning);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-2xs);
  letter-spacing: 0.5px;
}

.compliance__text {
  color: var(--color-text-primary);
  font-weight: 600;
}

.compliance__context {
  color: var(--color-text-secondary);
}

</style>
