<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 独立整页阅读器（`/knowledge/read/:entryId`）—— 知识库里「在新页面打开」落到这里。
 *
 * 与知识库内嵌阅读器的区别：**没有壳层**（不带左栏导航与面包屑），整屏只放这一份内容，
 * 顶栏按钮换成「关闭」。存在的意义：长文档 / PDF 在小窗口里读着难受，学生要的是"占满整屏"。
 *
 * 入口在 `KnowledgeBaseView.openInNewTab()`（新标签页打开本路由），
 * 因此这里的「关闭」优先 `window.close()`；若浏览器不允许脚本关闭标签页，退回到知识库列表。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { loadSnapshot, type KnowledgeEntry } from '@/api/knowledge'
import KnowledgeReader from '@/components/knowledge/KnowledgeReader.vue'

const route = useRoute()
const router = useRouter()

const entry = ref<KnowledgeEntry | null>(null)
const loading = ref(true)

function closePage(): void {
  window.close()
  window.setTimeout(() => {
    void router.push('/knowledge')
  }, 150)
}

onMounted(async () => {
  const id = String(route.params.entryId ?? '')
  try {
    const snapshot = await loadSnapshot()
    entry.value = snapshot.items.find((item) => item.id === id) ?? null
  } catch {
    entry.value = null
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="krp">
    <KnowledgeReader v-if="entry" :entry="entry" standalone @back="closePage" />
    <div v-else-if="loading" class="krp__state">正在读取…</div>
    <div v-else class="krp__state">
      <p class="krp__state-text">这条记录不在知识库里（可能已被移出或删除）。</p>
      <button class="krp__btn" type="button" @click="closePage">返回知识库</button>
    </div>
  </div>
</template>

<style scoped>
.krp {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding: var(--space-3);
  background-color: var(--color-bg-muted);
}

.krp > :deep(.kbv) {
  flex: 1;
  min-height: 0;
}

.krp__state {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
}

.krp__state-text {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-md);
}

.krp__btn {
  height: 30px;
  padding: 0 var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background-color: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  box-shadow: var(--shadow-card);
  transition:
    background-color var(--motion-dur-fast) var(--motion-ease),
    border-color var(--motion-dur-fast) var(--motion-ease);
}

.krp__btn:hover {
  background-color: var(--color-bg-subtle);
}

.krp__btn:active {
  transform: scale(var(--motion-press));
}

.krp__btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

@media (prefers-reduced-motion: reduce) {
  .krp__btn {
    transition: none;
  }
}
</style>
