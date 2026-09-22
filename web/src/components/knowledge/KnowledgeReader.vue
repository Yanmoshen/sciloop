<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 知识库阅读器：**占满内容区**，按扩展名选渲染方式（用户口径：文件一律内嵌阅读器，不用 HTML 直接渲染）。
 *
 * | 扩展名 | 怎么渲染 |
 * |---|---|
 * | pdf | 复用 `PdfPane`（PDF.js） |
 * | docx | `docx-preview`（动态引入，不进首屏包） |
 * | xlsx / xls / csv / tsv | `xlsx`（SheetJS）转表格（动态引入） |
 * | md | `MarkdownText`（含高亮与公式） |
 * | 代码类 | `highlight.js`（仓库已有依赖） |
 * | txt / log | 等宽纯文本 |
 * | 图片 | 浏览器原生 |
 * | 其它（pptx / doc / 音视频 / 压缩包…） | **「当前文件类型不支持在此打开」** + 下载 |
 *
 * 顶部按钮：返回列表 / 在新页面打开（内容存在的条目会给一个可打开的地址）。
 * ⚠️ demo 里二进制文件只有登记信息（没有真实字节），此时显示「没有可预览的文件内容」，
 * 接上文件存储后同一个分支会自动走真渲染（判断只看 `file_url`）。
 */
import hljs from 'highlight.js/lib/common'
import { computed, nextTick, ref, watch } from 'vue'

import { formatOf, formatSize, formatTime, hasContent, lastTouched, type KnowledgeEntry } from '@/api/knowledge'
import MarkdownText from '@/components/MarkdownText.vue'
import PdfPane from '@/components/PdfPane.vue'

const props = withDefaults(
  defineProps<{
    entry: KnowledgeEntry
    /** 独立整页模式：顶栏换成「关闭」，并去掉「在新页面打开」（自己就是那个新页面） */
    standalone?: boolean
  }>(),
  { standalone: false },
)

const emit = defineEmits<{
  (e: 'back'): void
  (e: 'open-standalone'): void
}>()

const format = computed(() => formatOf(props.entry.name, hasContent(props.entry)))
const fileUrl = computed(() => props.entry.file_url)

const EXT_LANG: Record<string, string> = {
  py: 'python',
  ipynb: 'json',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  jsx: 'javascript',
  vue: 'html',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  hpp: 'cpp',
  cs: 'csharp',
  go: 'go',
  rs: 'rust',
  rb: 'ruby',
  php: 'php',
  sh: 'bash',
  bash: 'bash',
  sql: 'sql',
  r: 'r',
  m: 'matlab',
  tex: 'latex',
  bib: 'latex',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  xml: 'xml',
  html: 'xml',
  css: 'css',
  md: 'markdown',
}

const codeHtml = ref('')
const sheetRows = ref<string[][]>([])
const sheetError = ref('')
const docxHost = ref<HTMLElement | null>(null)
const docxError = ref('')

function highlightCode(): void {
  const lang = EXT_LANG[format.value.ext]
  const code = props.entry.content
  if (!code) {
    codeHtml.value = ''
    return
  }
  try {
    codeHtml.value = lang && hljs.getLanguage(lang)
      ? hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
      : hljs.highlightAuto(code).value
  } catch {
    codeHtml.value = hljs.highlightAuto(code).value
  }
}

/** CSV / TSV 文本或 xlsx 原文件 → 表格（解析交给 SheetJS，引号、转义、多 sheet 都不自己写） */
async function buildSheet(): Promise<void> {
  sheetRows.value = []
  sheetError.value = ''
  const text = props.entry.content
  const url = fileUrl.value
  if (!text && !url) return
  try {
    const XLSX = await import('xlsx')
    const book = text
      ? XLSX.read(text, { type: 'string' })
      : XLSX.read(await (await fetch(url as string)).arrayBuffer(), { type: 'array' })
    const first = book.SheetNames[0]
    const sheet = first ? book.Sheets[first] : undefined
    if (!sheet) {
      sheetError.value = '这个表格里没有可显示的内容'
      return
    }
    sheetRows.value = XLSX.utils.sheet_to_json<string[]>(sheet, { header: 1, blankrows: false, defval: '' })
  } catch {
    sheetError.value = '表格解析失败，可以下载原文件后查看'
  }
}

/** docx → 文档视图（动态引入，避免首屏包变大） */
async function renderDocx(): Promise<void> {
  docxError.value = ''
  const url = fileUrl.value
  if (!url) return
  await nextTick()
  const host = docxHost.value
  if (!host) return
  try {
    const { renderAsync } = await import('docx-preview')
    const response = await fetch(url)
    const blob = await response.blob()
    host.innerHTML = ''
    await renderAsync(blob, host, undefined, { className: 'kbv-docx', inWrapper: true })
  } catch {
    docxError.value = '文档渲染失败，可以下载原文件后查看'
  }
}

const canOpenStandalone = computed(() => !!fileUrl.value || hasContent(props.entry))

function download(): void {
  if (fileUrl.value) {
    window.open(fileUrl.value, '_blank', 'noopener')
    return
  }
  if (!hasContent(props.entry)) return
  const url = URL.createObjectURL(new Blob([props.entry.content], { type: 'text/plain;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = props.entry.name
  anchor.click()
  URL.revokeObjectURL(url)
}

function prepare(): void {
  codeHtml.value = ''
  sheetRows.value = []
  sheetError.value = ''
  docxError.value = ''
  const renderer = format.value.renderer
  if (renderer === 'code') highlightCode()
  if (renderer === 'sheet') void buildSheet()
  if (renderer === 'docx') void renderDocx()
}

watch(() => props.entry.id, prepare, { immediate: true })
</script>

<template>
  <section class="kbv">
    <header class="kbv__bar">
      <button class="kbv__btn" type="button" @click="emit('back')">
        {{ standalone ? '关闭' : '返回列表' }}
      </button>
      <span class="kbv__name" :title="entry.name">{{ entry.name }}</span>
      <span class="kbv__chip">{{ format.label }}</span>
      <span class="kbv__meta">{{ formatSize(entry.size) }} · {{ formatTime(lastTouched(entry)) }}</span>
      <span class="kbv__spacer" />
      <button
        v-if="!standalone && canOpenStandalone"
        class="kbv__btn"
        type="button"
        @click="emit('open-standalone')"
      >
        在新页面打开
      </button>
      <button v-if="canOpenStandalone" class="kbv__btn" type="button" @click="download">下载</button>
    </header>

    <div class="kbv__body scroll-y">
      <MarkdownText v-if="format.renderer === 'markdown'" :content="entry.content" />

      <pre v-else-if="format.renderer === 'code'" class="kbv__code hljs"><code v-html="codeHtml" /></pre>

      <pre v-else-if="format.renderer === 'text'" class="kbv__text">{{ entry.content }}</pre>

      <table v-else-if="format.renderer === 'sheet' && sheetRows.length" class="kbv__sheet">
        <tbody>
          <tr v-for="(row, rowIndex) in sheetRows" :key="rowIndex">
            <component
              :is="rowIndex === 0 ? 'th' : 'td'"
              v-for="(cell, cellIndex) in row"
              :key="cellIndex"
            >
              {{ cell }}
            </component>
          </tr>
        </tbody>
      </table>
      <p v-else-if="format.renderer === 'sheet' && sheetError" class="kbv__state-text">{{ sheetError }}</p>

      <img v-else-if="format.renderer === 'image' && fileUrl" class="kbv__image" :src="fileUrl" :alt="entry.name" />

      <PdfPane v-else-if="format.renderer === 'pdf' && fileUrl" :src="fileUrl" :annotations="[]" side="source" />

      <div v-else-if="format.renderer === 'docx' && fileUrl" ref="docxHost" class="kbv__docx" />
      <p v-else-if="format.renderer === 'docx' && docxError" class="kbv__state-text">{{ docxError }}</p>

      <div v-else class="kbv__state">
        <p class="kbv__state-title">
          {{ format.renderer === 'unsupported' ? '当前文件类型不支持在此打开' : '没有可预览的文件内容' }}
        </p>
        <p class="kbv__state-note">
          {{
            format.renderer === 'unsupported'
              ? '可以下载后用本机软件打开。'
              : '这条记录只有登记信息（本地演示数据），接上文件存储后即可在此预览。'
          }}
        </p>
        <button v-if="fileUrl" class="kbv__btn" type="button" @click="download">下载</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.kbv {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
  overflow: hidden;
}

.kbv__bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
}

.kbv__name {
  min-width: 0;
  font-size: var(--font-size-md);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kbv__chip {
  flex: none;
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbv__meta {
  flex: none;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-family: var(--font-family-mono);
}

.kbv__spacer {
  flex: 1;
}

.kbv__btn {
  flex: none;
  height: 26px;
  padding: 0 var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background-color: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-xs);
  cursor: pointer;
  box-shadow: var(--shadow-card);
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease),
    background-color var(--motion-dur-fast) var(--motion-ease);
}

.kbv__btn:hover {
  background-color: var(--color-bg-subtle);
  color: var(--color-text-primary);
}

.kbv__btn:active {
  transform: scale(var(--motion-press));
}

.kbv__btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbv__body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--space-4);
}

.kbv__code,
.kbv__text {
  margin: 0;
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-sm);
  line-height: 1.7;
  overflow-x: auto;
  white-space: pre;
}

.kbv__text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.kbv__sheet {
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.kbv__sheet th,
.kbv__sheet td {
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  text-align: left;
  white-space: nowrap;
}

.kbv__sheet th {
  background-color: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-weight: 400;
}

.kbv__image {
  max-width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.kbv__docx :deep(.kbv-docx-wrapper) {
  background: transparent;
}

.kbv__state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-5);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-lg);
}

.kbv__state-title {
  margin: 0;
  font-size: var(--font-size-md);
}

.kbv__state-note {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbv__state-text {
  margin: 0;
  color: var(--color-warning);
  font-size: var(--font-size-sm);
}

@media (prefers-reduced-motion: reduce) {
  .kbv__btn {
    transition: none;
  }
}
</style>
