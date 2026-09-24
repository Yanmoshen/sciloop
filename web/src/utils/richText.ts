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
