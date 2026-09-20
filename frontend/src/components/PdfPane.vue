<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * PDF 阅读面：用 PDF.js 原样渲染版本 PDF（canvas + 文本层），并在页面上叠加批注高亮。
 *
 * 为什么不用 <iframe> 原生阅读器：原生阅读器在独立上下文里，既拿不到页面 DOM，
 * 也无法在其上定位批注矩形——而需求是「批注直接高亮在 PDF 上」，所以必须自渲染。
 *
 * 坐标口径（已按后端实测校准）：后端 `bbox` 是 **PDF 用户空间、左下原点**
 * （例：第 1 页 [124.31, 72.9, 487.89, 98.8]，页面 612×792）；
 * 用 `viewport.convertToViewportRectangle()` 换算到视口坐标（自动处理 y 翻转）。
 *
 * 性能：**按可视区懒渲染**——先按每页真实尺寸占位，滚动到附近才画 canvas 与文本层；
 * 15 页文档不再卡在「正在渲染」。
 */
import * as pdfjs from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { onBeforeUnmount, ref, watch } from 'vue'

import type { ReaderAnnotation } from '@/api/reader'

// 用 `?url` 指向打包后的 worker 文件：pdf.js 会**为每个文档各起一个 worker**，
// 左右两个阅读面因此互不干扰（用全局 workerPort 会共享同一个 worker，第二个文档会卡住）。
// 依赖 frontend/nginx.conf 给 `.mjs` 补的 text/javascript 映射，否则浏览器按 MIME 校验拒载。
pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

const props = defineProps<{
  src: string
  annotations: ReaderAnnotation[]
  side: 'source' | 'target'
}>()

const emit = defineEmits<{
  (e: 'select-text', payload: { side: 'source' | 'target'; page: number; text: string; rect: DOMRect }): void
  (e: 'click-annotation', payload: { annotation: ReaderAnnotation; rect: DOMRect }): void
}>()

const host = ref<HTMLElement | null>(null)
const pageCount = ref(0)
const loading = ref(false)
const error = ref('')

let doc: pdfjs.PDFDocumentProxy | null = null
let observer: IntersectionObserver | null = null
let token = 0
let building = false
let rebuildQueued = false
const rendered = new Set<number>()
const pageViews = new Map<number, pdfjs.PageViewport>()

function cleanup(): void {
  token += 1
  observer?.disconnect()
  observer = null
  rendered.clear()
  pageViews.clear()
  pageCount.value = 0
  if (doc) {
    void doc.destroy()
    doc = null
  }
}

/** 只重建高亮层（批注变化时不重建文档，避免打断正在进行的渲染） */
function paintHighlights(): void {
  if (!host.value) return
  host.value.querySelectorAll<HTMLElement>('.pdf-hl').forEach((layer) => {
    const number = Number(layer.dataset.page)
    const viewport = pageViews.get(number)
    if (!viewport) return
    layer.replaceChildren()
    props.annotations
      .filter((item) => item.page === number)
      .forEach((item) => {
        const box = toHighlight(item, viewport)
        if (!box) return
        const mark = document.createElement('button')
        mark.type = 'button'
        mark.className = `pdf-mark pdf-mark--${item.kind ?? 'highlight'}`
        mark.style.left = `${box.left}px`
        mark.style.top = `${box.top}px`
        mark.style.width = `${box.width}px`
        mark.style.height = `${box.height}px`
        mark.title = item.note || item.quote_text || `批注 #${item.id}`
        mark.addEventListener('click', (event) => {
          event.stopPropagation()
          emit('click-annotation', { annotation: item, rect: mark.getBoundingClientRect() })
        })
        layer.appendChild(mark)
      })
  })
}

function toHighlight(
  annotation: ReaderAnnotation,
  viewport: pdfjs.PageViewport,
): { left: number; top: number; width: number; height: number } | null {
  const bbox = annotation.bbox
  if (!Array.isArray(bbox) || bbox.length !== 4 || bbox.some((v) => typeof v !== 'number')) return null
  const [x1, y1, x2, y2] = viewport.convertToViewportRectangle(bbox as number[])
  const left = Math.min(x1, x2)
  const top = Math.min(y1, y2)
  const width = Math.abs(x2 - x1)
  const height = Math.abs(y2 - y1)
  if (width <= 0.5 || height <= 0.5) return null
  return { left, top, width, height }
}

/** 渲染单页（canvas + 文本层），只做一次 */
async function renderPage(number: number): Promise<void> {
  if (!doc || rendered.has(number)) return
  const viewport = pageViews.get(number)
  const wrapper = host.value?.querySelector<HTMLElement>(`[data-page="${number}"]`)
  if (!viewport || !wrapper) return
  rendered.add(number)

  const page = await doc.getPage(number)
  const canvas = wrapper.querySelector('canvas')
  const ctx = canvas?.getContext('2d')
  if (!canvas || !ctx) return
  const dpr = Math.min(2, window.devicePixelRatio || 1)
  const run = token
  await page.render({
    canvasContext: ctx,
    viewport,
    transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0],
  }).promise
  if (run !== token) return

  const textHost = wrapper.querySelector<HTMLElement>('.pdf-text-layer')
  if (!textHost) return
  try {
    const textContent = await page.getTextContent()
    const layer = new pdfjs.TextLayer({
      textContentSource: textContent,
      container: textHost,
      viewport,
    })
    await layer.render()
  } catch {
    /* 文本层失败不影响阅读 */
  }
}

/** 建占位（含高亮层容器），并按可视区懒渲染；同一时刻只允许一个构建在跑 */
async function build(): Promise<void> {
  if (!props.src || !host.value) return
  if (building) {
    rebuildQueued = true
    return
  }
  building = true
  cleanup()
  host.value.replaceChildren()
  loading.value = true
  error.value = ''
  const run = token
  try {
    doc = await pdfjs.getDocument({ url: props.src }).promise
    if (run !== token || !doc) return
    pageCount.value = doc.numPages
    const containerWidth = host.value.clientWidth || 640
    const first = await doc.getPage(1)
    const baseViewport = first.getViewport({ scale: 1 })
    const scale = Math.max(0.5, Math.min(1.6, (containerWidth - 2) / baseViewport.width))

    for (let number = 1; number <= doc.numPages; number += 1) {
      const page = await doc.getPage(number)
      if (run !== token) return
      const viewport = page.getViewport({ scale })
      pageViews.set(number, viewport)

      const wrapper = document.createElement('div')
      wrapper.className = 'pdf-page'
      wrapper.dataset.page = String(number)
      wrapper.style.width = `${viewport.width}px`
      wrapper.style.height = `${viewport.height}px`

      const canvas = document.createElement('canvas')
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      canvas.width = Math.floor(viewport.width * dpr)
      canvas.height = Math.floor(viewport.height * dpr)
      canvas.style.width = `${viewport.width}px`
      canvas.style.height = `${viewport.height}px`
      wrapper.appendChild(canvas)

      const textLayer = document.createElement('div')
      textLayer.className = 'pdf-text-layer'
      wrapper.appendChild(textLayer)

      const highlightLayer = document.createElement('div')
      highlightLayer.className = 'pdf-hl'
      highlightLayer.dataset.page = String(number)
      wrapper.appendChild(highlightLayer)
      host.value.appendChild(wrapper)
    }

    paintHighlights()

    // 懒渲染：进入视口附近才画
    observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return
          const number = Number((entry.target as HTMLElement).dataset.page)
          void renderPage(number)
        })
      },
      { root: host.value, rootMargin: '600px 0px' },
    )
    host.value.querySelectorAll('.pdf-page').forEach((el) => observer?.observe(el))
    // 首页立即渲染（不必等 observer 回调），渲染完即撤掉 loading
    await renderPage(1)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    building = false
    if (run === token) loading.value = false
    if (rebuildQueued) {
      rebuildQueued = false
      void build()
    }
  }
}

function onMouseUp(): void {
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed) return
  const text = selection.toString().trim()
  if (!text) return
  const range = selection.getRangeAt(0)
  const rect = range.getBoundingClientRect()
  const node = range.startContainer.parentElement?.closest('.pdf-page') as HTMLElement | null
  const page = node?.dataset.page ? Number(node.dataset.page) : null
  if (page === null || Number.isNaN(page)) return
  emit('select-text', { side: props.side, page, text, rect })
}

// 文档只在 src 变化时重建；批注变化只重画高亮层（不打断渲染）
watch(
  () => props.src,
  () => void build(),
)

watch(
  () => props.annotations,
  () => paintHighlights(),
)

watch(host, (el) => {
  if (el) requestAnimationFrame(() => void build())
})

onBeforeUnmount(cleanup)
</script>

<template>
  <div class="pdf-pane">
    <div ref="host" class="pdf-pane__host" @mouseup="onMouseUp" />
    <p v-if="loading" class="pdf-pane__state">正在渲染 PDF…</p>
    <p v-else-if="error" class="pdf-pane__state pdf-pane__state--err">{{ error }}</p>
    <p v-else-if="pageCount === 0" class="pdf-pane__state">该版本还没有 PDF 文件</p>
  </div>
</template>

<style scoped>
.pdf-pane {
  position: relative;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.pdf-pane__host {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3);
  scrollbar-gutter: stable;
}

.pdf-pane__host::-webkit-scrollbar {
  width: 12px;
  height: 12px;
}
.pdf-pane__host::-webkit-scrollbar-track {
  background: transparent;
  margin: 10px 0;
  border-radius: 999px;
}
.pdf-pane__host::-webkit-scrollbar-thumb {
  background-color: var(--color-border-strong);
  border: 4px solid transparent;
  border-radius: 999px;
  background-clip: content-box;
  min-height: 44px;
  transition: background-color 180ms cubic-bezier(0.4, 0, 0.2, 1), border-width 180ms;
}
.pdf-pane__host::-webkit-scrollbar-thumb:hover {
  background-color: var(--color-text-secondary);
  border-width: 2px;
}
.pdf-pane__host::-webkit-scrollbar-thumb:active {
  background-color: var(--color-brand);
  border-width: 2px;
}
@supports not selector(::-webkit-scrollbar) {
  .pdf-pane__host {
    scrollbar-width: thin;
    scrollbar-color: var(--color-border-strong) transparent;
  }
}

.pdf-pane__host :deep(.pdf-page) {
  position: relative;
  flex: none;
  background: var(--color-card-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm, 4px);
  box-shadow: var(--shadow-card);
  overflow: hidden;
}

.pdf-pane__host :deep(.pdf-page canvas) {
  display: block;
}

.pdf-pane__host :deep(.pdf-text-layer) {
  position: absolute;
  inset: 0;
  overflow: hidden;
  line-height: 1;
  text-align: initial;
  z-index: 2;
}

.pdf-pane__host :deep(.pdf-text-layer span) {
  position: absolute;
  color: transparent;
  white-space: pre;
  cursor: text;
  transform-origin: 0 0;
}

.pdf-pane__host :deep(.pdf-text-layer ::selection) {
  background: color-mix(in srgb, var(--color-brand) 32%, transparent);
}

.pdf-pane__host :deep(.pdf-hl) {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 3;
}

.pdf-pane__host :deep(.pdf-mark) {
  position: absolute;
  padding: 0;
  border: 0;
  border-radius: 2px;
  background: color-mix(in srgb, var(--color-warning) 42%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--color-warning) 70%, transparent);
  cursor: pointer;
  pointer-events: auto;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.pdf-pane__host :deep(.pdf-mark:hover) {
  background: color-mix(in srgb, var(--color-warning) 62%, transparent);
}

.pdf-pane__host :deep(.pdf-mark--note) {
  background: color-mix(in srgb, var(--color-brand) 30%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--color-brand) 62%, transparent);
}

.pdf-pane__host :deep(.pdf-mark--note:hover) {
  background: color-mix(in srgb, var(--color-brand) 48%, transparent);
}

.pdf-pane__state {
  position: absolute;
  top: var(--space-4);
  left: 50%;
  transform: translateX(-50%);
  margin: 0;
  padding: 4px 12px;
  border-radius: var(--radius-pill);
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.pdf-pane__state--err {
  color: var(--color-danger);
  border-color: var(--color-danger);
}
</style>
