/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文翻译 API（EasyPaper 核心模块 ②，前端本轮对接）。
 *
 * 引擎刻意不用 pdf2zh：后端用 PyMuPDF 块级原位译写（`engine="pymupdf-block-v1"`），
 * `layout_warnings` 如实输出；无 LLM Key 时走 local-stub（译文带【本地桩·未翻译】前缀，cost=0）。
 */
import { apiUrl, get, post } from './client'

export interface TranslateSource {
  filename?: string | null
  pages?: number | null
  sha256?: string | null
}

export interface TranslateFormatState {
  available?: boolean
  bytes?: number | null
  pages?: number | null
  [key: string]: unknown
}

export interface TranslateJob {
  task_id: string
  status: string
  percent?: number
  stage?: string | null
  message?: string | null
  error?: string | null
  mode?: string | null
  highlight?: boolean
  paper_id?: number | null
  source?: TranslateSource | null
  formats?: { mono?: TranslateFormatState; dual?: TranslateFormatState } | null
  layout_warnings?: string[] | null
  highlight_summary?: Record<string, unknown> | null
  created_at?: string | null
  [key: string]: unknown
}

export interface TranslateJobList {
  items: TranslateJob[]
  total: number
  engine?: string
  note?: string | null
  source?: string
}

export function listTranslateJobs(limit = 30): Promise<TranslateJobList> {
  return get<TranslateJobList>('/translate/jobs', { query: { limit } })
}

export function fetchTranslateJob(taskId: string): Promise<TranslateJob> {
  return get<TranslateJob>(`/translate/jobs/${encodeURIComponent(taskId)}`)
}

/** 用已入库论文的原文创建翻译任务（论文库里最常用的一条路径） */
export function createTranslateFromPaper(
  paperId: number,
  options: { mode?: 'translate' | 'simplify'; force?: boolean } = {},
): Promise<{ task_id: string; status?: string }> {
  return post<{ task_id: string; status?: string }>('/translate/jobs/from-paper', {
    body: { paper_id: paperId, mode: options.mode ?? 'translate', force: options.force ?? false },
    timeoutMs: 60_000,
  })
}

/** 直接上传 PDF 创建翻译任务 */
export function createTranslateFromFile(
  file: File,
  options: { mode?: 'translate' | 'simplify' } = {},
): Promise<{ task_id: string; status?: string }> {
  const form = new FormData()
  form.append('file', file)
  form.append('mode', options.mode ?? 'translate')
  return post<{ task_id: string; status?: string }>('/translate/jobs', {
    body: form,
    timeoutMs: 120_000,
  })
}

export function cancelTranslateJob(taskId: string): Promise<TranslateJob> {
  return post<TranslateJob>(`/translate/jobs/${encodeURIComponent(taskId)}/cancel`, {})
}

export function retryTranslateJob(taskId: string): Promise<TranslateJob> {
  return post<TranslateJob>(`/translate/jobs/${encodeURIComponent(taskId)}/retry`, {})
}

/** 原文/译文逐块对照预览 */
export interface TranslatePreviewBlock {
  page?: number | null
  block_id?: string | null
  source_text?: string | null
  target_text?: string | null
  [key: string]: unknown
}

export interface TranslatePreview {
  task_id?: string
  blocks?: TranslatePreviewBlock[]
  total?: number
  [key: string]: unknown
}

export function fetchTranslatePreview(taskId: string): Promise<TranslatePreview> {
  return get<TranslatePreview>(`/translate/jobs/${encodeURIComponent(taskId)}/preview`)
}

/** 下载 PDF（`format=mono|dual`）——走 `apiUrl` 让浏览器直接下载 */
export function translatePdfUrl(taskId: string, format: 'mono' | 'dual' = 'mono'): string {
  return apiUrl(`/translate/jobs/${encodeURIComponent(taskId)}/pdf`, { format })
}
