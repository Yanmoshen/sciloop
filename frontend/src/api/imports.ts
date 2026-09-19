/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文导入 API（EasyPaper 核心模块 ①，后端先行，前端本轮对接）。
 *
 * | 端点 | 用途 |
 * |---|---|
 * | `POST /papers/import` | 上传 PDF 批量导入（multipart，Owner 面，202 + task_id） |
 * | `POST /papers/import/identifiers` | 按 DOI / arXiv ID 批量导入（Owner 面） |
 * | `GET /papers/import-jobs/{task_id}` | 导入任务进度（轮询口） |
 * | `GET /papers/imports` | 导入历史（按 papers.raw.upload 聚合，分页） |
 */
import { get, post } from './client'

/** `POST /papers/import` 的响应（长任务受理） */
export interface ImportAccepted {
  task_id: string
  status?: string
  job?: string
  [key: string]: unknown
}

export interface ImportJobCounts {
  created?: number
  reused?: number
  failed?: number
  duplicate?: number
  [key: string]: number | undefined
}

export interface ImportJobItem {
  name?: string
  status?: string
  code?: string
  message?: string
  paper_id?: number | null
  [key: string]: unknown
}

export interface ImportJob {
  task_id: string
  status: string
  counts?: ImportJobCounts | null
  items?: ImportJobItem[] | null
  error?: string | null
  events?: Array<{ at?: string; level?: string; text?: string }> | null
  progress?: { stage?: string; label?: string; pct?: number; eta_seconds?: number | null } | null
  [key: string]: unknown
}

/** 上传 PDF（≤20MB/个、单次 ≤20 个；参数与后端契约一致） */
export function importPdfs(files: File[], projectId?: number | null): Promise<ImportAccepted> {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  if (projectId !== null && projectId !== undefined) form.append('project_id', String(projectId))
  return post<ImportAccepted>('/papers/import', { body: form, timeoutMs: 120_000 })
}

/** 按 DOI / arXiv ID 批量导入（换行或逗号分隔） */
export function importIdentifiers(
  values: string[],
  projectId?: number | null,
): Promise<ImportAccepted> {
  return post<ImportAccepted>('/papers/import/identifiers', {
    body: { identifiers: values, project_id: projectId ?? null },
    timeoutMs: 60_000,
  })
}

export function fetchImportJob(taskId: string): Promise<ImportJob> {
  return get<ImportJob>(`/papers/import-jobs/${encodeURIComponent(taskId)}`)
}

export interface ImportHistoryItem {
  id: number
  created_at?: string | null
  source_type?: string | null
  status?: string | null
  paper_id?: number | null
  paper_source?: string | null
  name_or_identifier?: string | null
  title?: string | null
  title_source?: string | null
  file_sha256?: string | null
  extracted_arxiv_id?: string | null
  extracted_doi?: string | null
}

export interface ImportHistoryPage {
  items: ImportHistoryItem[]
  page: number
  page_size: number
  total: number
  scope_note?: string | null
  source_type?: string | null
}

export function fetchImportHistory(page = 1, pageSize = 20): Promise<ImportHistoryPage> {
  return get<ImportHistoryPage>('/papers/imports', { query: { page, page_size: pageSize } })
}
