<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 *
 * 对话正文的 Markdown 渲染：基础语法 + 代码高亮（highlight.js）+ 公式（KaTeX）。
 *
 * 为什么要自己挂 KaTeX 规则
 * -------------------------
 * 现成的 `markdown-it-katex` 已多年未维护、与 KaTeX 0.16 不兼容。这里按 markdown-it 的
 * 官方扩展点挂两条规则（`$$...$$` 块级 / `$...$` 行内），直接调 `katex.renderToString`：
 * 规则挂在解析层，因此**代码块与行内代码里的 `$` 不会被当成公式**（预替换方案会踩这个坑）。
 *
 * 安全口径：`html: false` —— 模型输出里的 HTML 一律按纯文本转义，不走 `v-html` 的注入面。
 * 渲染结果是我们自己生成的 HTML 片段（markdown-it + KaTeX），所以 `v-html` 是可控的。
 */
import hljs from 'highlight.js/lib/common'
import katex from 'katex'
import MarkdownIt from 'markdown-it'
import type StateBlock from 'markdown-it/lib/rules_block/state_block.mjs'
import type StateInline from 'markdown-it/lib/rules_inline/state_inline.mjs'
import { computed } from 'vue'

const props = defineProps<{ content: string }>()

/** 行内公式：`$...$`（开头不能是空白、结尾不能是空白，避免把金额 `100$` 误判成公式） */
const INLINE_MATH = /^\$([^\s$][^$\n]*?)\$/

function renderMath(tex: string, displayMode: boolean): string {
  try {
    return katex.renderToString(tex, { displayMode, throwOnError: false, output: 'html' })
  } catch {
    return ''
  }
}

function katexPlugin(md: MarkdownIt): void {
  md.block.ruler.before(
    'fence',
    'math_block',
    (state: StateBlock, startLine: number, endLine: number, silent: boolean) => {
      const start = state.bMarks[startLine]! + state.tShift[startLine]!
      const max = state.eMarks[startLine]!
      if (start + 2 > max || state.src.slice(start, start + 2) !== '$$') return false
      if (silent) return true

      const firstLine = state.src.slice(start + 2, max)
      let body = firstLine
      let nextLine = startLine
      let closed = false

      if (firstLine.trim().endsWith('$$')) {
        body = firstLine.trim().slice(0, -2)
        closed = true
      }
      while (!closed) {
        nextLine += 1
        if (nextLine >= endLine) break
        const lineStart = state.bMarks[nextLine]! + state.tShift[nextLine]!
        const lineEnd = state.eMarks[nextLine]!
        const line = state.src.slice(lineStart, lineEnd)
        const trimmed = line.trim()
        if (trimmed.endsWith('$$')) {
          body += `\n${line.slice(0, line.lastIndexOf('$$'))}`
          closed = true
          break
        }
        body += `\n${line}`
      }

      const token = state.push('math_block', 'math', 0)
      token.block = true
      token.content = body.trim()
      token.markup = '$$'
      token.map = [startLine, nextLine + 1]
      state.line = nextLine + 1
      return true
    },
    { alt: ['paragraph', 'reference', 'blockquote', 'list'] },
  )

  md.renderer.rules.math_block = (tokens, idx) =>
    `<div class="math-block">${renderMath(tokens[idx]!.content, true)}</div>\n`

  md.inline.ruler.after('escape', 'math_inline', (state: StateInline, silent: boolean) => {
    if (state.src[state.pos] !== '$') return false
    const match = INLINE_MATH.exec(state.src.slice(state.pos))
    if (!match) return false
    if (silent) return true
    const token = state.push('math_inline', 'math', 0)
    token.content = match[1]!
    token.markup = '$'
    state.pos += match[0].length
    return true
  })

  md.renderer.rules.math_inline = (tokens, idx) => renderMath(tokens[idx]!.content, false)
}

const md: MarkdownIt = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        const result = hljs.highlight(code, { language: lang, ignoreIllegals: true })
        return `<pre class="hljs"><code class="language-${lang}">${result.value}</code></pre>`
      } catch {
        /* 落到下面的无语言分支 */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`
  },
})
md.use(katexPlugin)

const html = computed(() => md.render(props.content ?? ''))
</script>

<template>
  <div class="md" v-html="html" />
</template>
