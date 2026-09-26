<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全屏双语阅读（用户口径 2026-09-26）：
 * 「双语阅读应该是**全屏只剩下左边原文右边译文**」——导航栏、头像、页头按钮全部去掉，
 * 论文占满整屏，**左右两栏各自滚动、互不影响**。
 *
 * 为什么是**顶层路由**而不是阅读页里的一个开关：
 * 阅读页带着应用外壳（左栏导航、顶栏、头像、页头），想"只剩两栏"就得把壳隐藏 ——
 * 与其在阅读页里加一堆条件样式，不如单独一个页面（与 `/knowledge/read/:entryId` 同一手法）。
 *
 * 渲染交给 `components/PdfPane.vue`（PDF.js）：它每栏自带滚动容器，
 * 所以"两边互不影响"是天然成立的，不需要额外同步/解绑逻辑。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  fetchReaderDocument,
  listReaderVersions,
  readerVersionPdfUrl,
  registerReaderVersion,
  type ReaderVersion,
} from '@/api/reader'
import { listTranslateJobs } from '@/api/translate'
import PdfPane from '@/components/PdfPane.vue'

const route = useRoute()
const router = useRouter()

const documentId = computed(() => {
  const parsed = Number(route.params.documentId)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
})
const paperId = ref<number | null>(null)
const title = ref('')
const versions = ref<ReaderVersion[]>([])
const originalId = ref<number | null>(null)
const chineseId = ref<number | null>(null)
const loading = ref(true)
const notice = ref('')

const originalUrl = computed(() =>
  documentId.value && originalId.value ? readerVersionPdfUrl(documentId.value, originalId.value) : '',
)
const chineseUrl = computed(() =>
  documentId.value && chineseId.value ? readerVersionPdfUrl(documentId.value, chineseId.value) : '',
)

/** 有中文译本时，取 `version_no` 最大的那份当"当前译文"（与阅读页同一口径）。 */
function pickChinese(items: ReaderVersion[]): number | null {
  const chinese = items
    .filter((item) => item.kind !== 'original')
    .sort((left, right) => right.version_no - left.version_no)
  return chinese[0]?.id ?? null
}

async function load(): Promise<void> {
  const id = documentId.value
  if (!id) {
    notice.value = '没拿到要读的文档'
    loading.value = false
    return
  }
  loading.value = true
  notice.value = ''
  try {
    const [detail, versionList] = await Promise.all([
      fetchReaderDocument(id).catch(() => null),
      listReaderVersions(id),
    ])
    if (detail) {
      paperId.value = Number((detail as { paper_id?: number }).paper_id ?? 0) || null
      title.value = String((detail as { title?: string }).title ?? '')
    }
    versions.value = versionList.items ?? []
    originalId.value = versions.value.find((item) => item.kind === 'original')?.id ?? null
    chineseId.value = pickChinese(versions.value)
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

/** 有更新的翻译产物但还没登记时，就地登记（同阅读页的 `autoAttachTranslation`）。 */
async function attachNewest(): Promise<void> {
  const id = documentId.value
  if (!id || !paperId.value) return
  try {
    const page = await listTranslateJobs(50)
    const latest = (page.items ?? [])
      .filter((job) => job.paper_id === paperId.value && job.status === 'completed' && job.task_id)
      .sort((left, right) =>
        String(right.created_at ?? '').localeCompare(String(left.created_at ?? '')),
      )[0]
    if (!latest?.task_id) return
    const registered = new Set(
      versions.value.map((item) => String(item.task_id ?? '')).filter(Boolean),
    )
    if (registered.has(String(latest.task_id))) return
    const created = await registerReaderVersion(id, 'chinese', String(latest.task_id))
    if (created?.id) {
      versions.value = [...versions.value, created]
      chineseId.value = created.id
    }
  } catch {
    /* 没有产物 / 无权限 —— 保持"这次还没有译文"即可 */
  }
}

function exit(): void {
  void router.push(`/papers/reader/${documentId.value ?? ''}`)
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') exit()
}

onMounted(async () => {
  await load()
  await attachNewest()
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="dual">
    <section class="dual__pane">
      <header class="dual__label">原文</header>
      <PdfPane v-if="originalUrl" :src="originalUrl" :annotations="[]" side="source" />
      <p v-else class="dual__state">{{ loading ? '正在打开…' : '这一版没有可打开的原文件' }}</p>
    </section>

    <section class="dual__pane">
      <header class="dual__label">译文</header>
      <PdfPane v-if="chineseUrl" :src="chineseUrl" :annotations="[]" side="target" />
      <p v-else-if="loading" class="dual__state">正在打开…</p>
      <div v-else class="dual__state">
        <p>{{ notice || '这篇还没有中文译本' }}</p>
        <button
          v-if="paperId"
          class="dual__go"
          type="button"
          @click="router.push({ path: '/papers/translate', query: { paper_id: String(paperId) } })"
        >
          去翻译这篇
        </button>
      </div>
    </section>

    <!-- 唯一的"出口"：按 Esc 也行。这里刻意只放一个字，不摆导航栏也不放头像。 -->
    <button class="dual__exit" type="button" @click="exit" :title="`返回 ${title || '全文阅读'}`">
      返回
    </button>
  </div>
</template>

<style scoped>
.dual {
  display: flex;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background: var(--color-bg-page);
}
.dual__pane {
  flex: 1 1 50%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 两栏之间只靠一条分隔线，不额外画卡片边框（要"只剩原文和译文"） */
.dual__pane + .dual__pane {
  border-left: 1px solid var(--color-border);
}
.dual__label {
  flex: none;
  padding: 6px 12px;
  color: var(--color-text-tertiary);
  font-size: var(--font-size-xs);
}
.dual__state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.dual__go {
  padding: 6px 14px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}
.dual__go:hover {
  background: var(--color-bg-hover, var(--color-bg-subtle));
}
/* 低调的返回钮：半透明、鼠标悬停才显眼 —— 不构成"导航栏" */
.dual__exit {
  position: fixed;
  top: 10px;
  left: 10px;
  z-index: 10;
  padding: 4px 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated, var(--color-bg-subtle));
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-xs);
  opacity: 0.35;
  cursor: pointer;
  transition: opacity 0.15s ease;
}
.dual__exit:hover,
.dual__exit:focus-visible {
  opacity: 1;
}
</style>
