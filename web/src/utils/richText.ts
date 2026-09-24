/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 *
 * 富文本的**最小**渲染：只放行 `**加粗**`。
 *
 * 为什么要有这个 util：解析速览与跨篇综述都用同一套约定（允许 1–2 处加粗、其余不许有标记），
 * 而 `v-html` 有注入面 —— 两处各写一遍「先转义再替换」迟早会写歪一处。
 * 这里**先转义全部 HTML，再把 `**…**` 换成 `<strong>`**，所以放行出去的标签只有我们自己生成的那几种。
 */

export function toBoldHtml(text: string): string {
  const escaped = String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
}

/**
 * 按空行切成**段落**，每段各自转义并放行加粗。
 *
 * 为什么要单独一个函数：`v-html` 里 HTML 会把换行当空白折叠，
 * 于是"后端分好的 2–3 段"到界面上又被压成一坨（2026-09-24 实测踩到：
 * 速览 `chars=306`、后端 3 段，界面却只渲染出 1 段）。
 * 调用方用 `v-for` 渲染成多个 `<p>`，段落才是真的。
 */
export function toParagraphHtmlList(text: string): string[] {
  return String(text ?? '')
    .split(/\n\s*\n/)
    .map((block) => toBoldHtml(block.trim()))
    .filter((block) => block.length > 0)
}
