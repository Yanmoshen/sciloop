/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文外链解析（**唯一实现**，卡片与列表共用，避免两处各拼一套）。
 *
 * 口径：
 * - **来源链接**：arXiv → `https://arxiv.org/abs/<external_id>`（摘要页比裸 PDF 更适合阅读）
 *   → DOI → `https://doi.org/<doi>` → 接口给的 `pdf_url`；
 *   三者都没有 → 返回 `null`，**调用方不渲染该行**（不打印「未获取」这类占位）。
 * - **代码/仓库链接**：只看 `code_url`；GitHub 仓库给短名 `Github`，其他域名保留原样短名。
 */

export interface PaperLinkSource {
  source?: string | null
  external_id?: string | null
  doi?: string | null
  pdf_url?: string | null
}

export interface ResolvedLink {
  href: string
  text: string
}

/** 论文来源链接（arXiv 摘要页 / DOI / PDF）；无依据时返回 null */
export function paperSourceLink(item: PaperLinkSource | null | undefined): ResolvedLink | null {
  if (!item) return null
  const externalId = (item.external_id ?? '').trim()
  if ((item.source ?? '').toLowerCase() === 'arxiv' && externalId) {
    const href = `https://arxiv.org/abs/${externalId}`
    return { href, text: href }
  }
  const doi = (item.doi ?? '').trim()
  if (doi) {
    const href = `https://doi.org/${doi}`
    return { href, text: href }
  }
  const pdf = (item.pdf_url ?? '').trim()
  if (pdf) return { href: pdf, text: pdf }
  return null
}

/** 代码仓库链接；GitHub 仓库的链接名固定为 `Github` */
export function codeRepoLink(
  item: (PaperLinkSource & { code_url?: string | null }) | null | undefined,
): ResolvedLink | null {
  if (!item) return null
  const url = (item.code_url ?? '').trim()
  if (!url) return null
  return { href: url, text: /(^|\.)github\.com(\/|$)/i.test(url) ? 'Github' : '代码仓库' }
}
