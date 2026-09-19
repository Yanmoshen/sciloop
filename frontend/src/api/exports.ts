/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 多格式导出 API（EasyPaper 核心模块 ④，前端本轮对接）。
 *
 * 六种导出全部是 **GET + 附件下载**，因此统一走 `apiUrl()` 交给浏览器直接下载
 * （不经过 fetch，避免把大文件读进内存；也天然带 Content-Disposition 文件名）。
 */
import { apiUrl, get } from './client'

export type ExportFormat = 'json' | 'csl-json' | 'bibtex' | 'csv' | 'obsidian'

export interface ExportOption {
  format: ExportFormat
  title: string
  /** 目标读者/工具，用于卡片副标题 */
  target: string
  extension: string
  /** 是否支持 include_claims（CSV/JSON/Obsidian 支持） */
  claims: boolean
}

export const EXPORT_OPTIONS: ExportOption[] = [
  { format: 'json', title: '全库知识 JSON', target: 'EasyPaper §5.4 形状，便于二次开发', extension: 'json', claims: true },
  { format: 'csl-json', title: 'CSL-JSON', target: 'Zotero / Mendeley 文献管理', extension: 'json', claims: false },
  { format: 'bibtex', title: 'BibTeX', target: 'LaTeX 论文写作', extension: 'bib', claims: false },
  { format: 'csv', title: 'CSV ZIP', target: 'Excel / 数据分析（entities + relationships）', extension: 'zip', claims: true },
  { format: 'obsidian', title: 'Obsidian ZIP', target: 'Markdown 笔记 + wiki 链接', extension: 'zip', claims: true },
]

export function exportUrl(
  format: ExportFormat,
  options: { limit?: number; paperIds?: number[]; includeClaims?: boolean } = {},
): string {
  const query: Record<string, string | number | boolean> = {}
  if (options.limit !== undefined) query.limit = options.limit
  if (options.paperIds && options.paperIds.length > 0) query.paper_ids = options.paperIds.join(',')
  if (options.includeClaims !== undefined) query.include_claims = options.includeClaims
  return apiUrl(`/exports/${format}`, query)
}

export function exportPaperUrl(paperId: number, includeClaims = true): string {
  return apiUrl(`/exports/paper/${paperId}`, { include_claims: includeClaims })
}

/** 单篇论文完整知识（JSON，直接读取用于预览） */
export interface PaperKnowledgeExport {
  paper?: Record<string, unknown>
  card?: Record<string, unknown> | null
  claims?: unknown[] | null
  [key: string]: unknown
}

export function fetchPaperExport(paperId: number, includeClaims = true): Promise<PaperKnowledgeExport> {
  return get<PaperKnowledgeExport>(`/exports/paper/${paperId}`, {
    query: { include_claims: includeClaims },
  })
}

/** 触发浏览器下载（同源 GET，无需令牌） */
export function triggerDownload(url: string): void {
  const link = document.createElement('a')
  link.href = url
  link.rel = 'noopener'
  link.style.display = 'none'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
}
