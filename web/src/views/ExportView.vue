<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 多格式导出（核心模块 ④）：五种全库导出 + 单篇论文完整知识导出。
 *
 * 导出全部是 GET 附件下载：点击直接交给浏览器（`apiUrl` + `<a download>`），
 * 不把文件读进内存；`include_claims` 仅在支持声明导出的格式上出现。
 */
import { computed, ref } from 'vue'

import {
  EXPORT_OPTIONS,
  exportPaperUrl,
  exportUrl,
  fetchPaperExport,
  triggerDownload,
  type ExportFormat,
} from '@/api/exports'

const limit = ref(500)
const includeClaims = ref(true)
const paperIdsText = ref('')
const paperId = ref<number | null>(null)
const preview = ref<Record<string, unknown> | null>(null)
const previewTitle = ref('')
const busy = ref('')
const notice = ref('')

const paperIds = computed(() =>
  paperIdsText.value
    .split(/[\s,，]+/)
    .map((item) => Number(item.trim()))
    .filter((value) => Number.isFinite(value) && value > 0),
)

function download(format: ExportFormat): void {
  notice.value = ''
  try {
    triggerDownload(
      exportUrl(format, {
        limit: limit.value > 0 ? limit.value : undefined,
        paperIds: paperIds.value.length > 0 ? paperIds.value : undefined,
        includeClaims: includeClaims.value,
      }),
    )
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  }
}

async function loadPreview(): Promise<void> {
  if (!paperId.value || busy.value) return
  busy.value = 'preview'
  notice.value = ''
  try {
    const data = await fetchPaperExport(paperId.value, includeClaims.value)
    preview.value = data
    const paper = (data.paper ?? {}) as Record<string, unknown>
    previewTitle.value = String(paper.title ?? `论文 #${paperId.value}`)
  } catch (error) {
    preview.value = null
    previewTitle.value = ''
    notice.value =
      (error as { status?: number })?.status === 404
        ? `论文 #${paperId.value} 不存在或不可导出。`
        : error instanceof Error
          ? error.message
          : String(error)
  } finally {
    busy.value = ''
  }
}

function downloadPaper(): void {
  if (!paperId.value) return
  triggerDownload(exportPaperUrl(paperId.value, includeClaims.value))
}

const previewClaims = computed(() => {
  const claims = (preview.value?.claims ?? []) as unknown[]
  return Array.isArray(claims) ? claims.length : 0
})
</script>

<template>
  <section class="exporter">
    <header class="exporter__head">
      <h1>多格式导出</h1>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <article class="controls">
      <label class="field">
        <span class="field__label">导出上限</span>
        <input v-model.number="limit" class="field__input" type="number" min="1" max="5000" />
      </label>
      <label class="field">
        <span class="field__label">指定论文 ID（可选，逗号分隔）</span>
        <input v-model="paperIdsText" class="field__input" type="text" placeholder="例如：547, 548" />
      </label>
      <label class="toggle" :class="{ 'toggle--on': includeClaims }">
        <input v-model="includeClaims" type="checkbox" />
        <span class="toggle__track"><span class="toggle__dot" /></span>
        <span class="toggle__text">包含 Claim 声明</span>
      </label>
    </article>

    <div class="cards">
      <article v-for="option in EXPORT_OPTIONS" :key="option.format" class="card">
        <div class="card__head">
          <h2>{{ option.title }}</h2>
          <span class="chip">.{{ option.extension }}</span>
        </div>
        <p class="card__target">{{ option.target }}</p>
        <footer class="card__foot">
          <button class="btn btn--primary" type="button" @click="download(option.format)">
            下载
          </button>
          <span v-if="option.claims" class="chip chip--soft">
            {{ includeClaims ? '含声明' : '不含声明' }}
          </span>
        </footer>
      </article>

      <article class="card card--single">
        <div class="card__head">
          <h2>单篇论文完整知识</h2>
          <span class="chip">.json</span>
        </div>
        <div class="card__row">
          <input v-model.number="paperId" class="field__input" type="number" min="1" placeholder="论文 ID" />
          <button class="btn" type="button" :disabled="!paperId || busy === 'preview'" @click="loadPreview">
            {{ busy === 'preview' ? '读取中…' : '预览' }}
          </button>
          <button class="btn btn--primary" type="button" :disabled="!paperId" @click="downloadPaper">
            下载
          </button>
        </div>
        <p v-if="previewTitle" class="card__target">{{ previewTitle }} · {{ previewClaims }} 条声明</p>
      </article>
    </div>
  </section>
</template>

<style scoped>
.exporter {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.exporter__head h1 {
  margin: 0;
  font-size: var(--font-size-xl);
}
.controls {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-4);
  padding: var(--space-4);
  background: var(--color-card-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 220px;
}
.field__label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.field__input {
  height: 34px;
  padding: 0 12px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
}
.field__input:focus {
  outline: none;
  border-color: var(--color-brand);
}
.toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}
.toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}
.toggle__track {
  width: 40px;
  height: 22px;
  padding: 3px;
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  border: 1px solid var(--color-border-strong);
  transition: background-color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.toggle__dot {
  display: block;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--color-text-secondary);
  transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1), background-color 180ms;
}
.toggle--on .toggle__track {
  background: var(--color-brand);
  border-color: var(--color-brand);
}
.toggle--on .toggle__dot {
  transform: translateX(18px);
  background: var(--color-text-inverse);
}
.toggle__text {
  font-size: var(--font-size-sm);
}
.toggle:hover .toggle__track {
  border-color: var(--color-brand);
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--space-3);
}
.card {
  padding: var(--space-4);
  background: var(--color-card-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  transition:
    transform 180ms cubic-bezier(0.16, 1, 0.3, 1),
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    box-shadow 200ms;
}
.card:hover {
  transform: translateY(-2px);
  border-color: var(--color-brand);
  box-shadow: var(--shadow-card);
}
.card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.card__head h2 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.card__target {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.card__foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: auto;
}
.card__row {
  display: flex;
  gap: var(--space-2);
}
.card__row .field__input {
  flex: 1;
  min-width: 0;
}
</style>
