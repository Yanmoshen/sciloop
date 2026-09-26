/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全文阅读器 API（EasyPaper 核心模块 ③，前端本轮对接）。
 *
 * 关键语义（照后端实现，不自造）：
 * - 文档登记（`POST /reader/documents`）会解析原文并**自动登记 original 版本**（immutable）
 * - 翻译产物通过 `POST .../versions {kind: chinese, task_id}` 登记为**不可变版本**（阅读页会自动接入）
 * - 版本 PDF 与文本索引分别走 `.../versions/{id}/pdf` 与 `.../versions/{id}/text`
 * - 批注锚点是 `block_id` + `quote_sha256`（内容寻址）；更新必须带 `revision`，冲突返回 409
 */
import { apiUrl, del, get, patch, post, put } from './client'

export interface ReaderDocumentSummary {
  id: number
  paper_id?: number | null
  title?: string | null
  title_source?: string | null
  schema_version?: number
  parse_status?: string | null
  fingerprint?: string | null
  source_url?: string | null
  page_count?: number | null
  block_count?: number | null
  section_count?: number | null
  payload_sha256?: string | null
  warnings?: string[] | null
  parser?: string | null
}

export interface ReaderDocumentPage {
  items: ReaderDocumentSummary[]
  page: number
  page_size: number
  total: number
}

/** 正文块（block id 为内容寻址，稳定） */
export interface ReaderBlock {
  id: string
  page?: number | null
  type?: string | null
  section_id?: string | null
  source_text?: string | null
  sentences?: string[] | null
  bbox?: number[] | null
}

export interface ReaderSection {
  id: string
  name?: string | null
  title?: string | null
  page?: number | null
  block_ids?: string[] | null
}

export interface ReaderPageMeta {
  page: number
  width?: number | null
  height?: number | null
  char_count?: number | null
  is_scan?: boolean
  block_ids?: string[] | null
}

export interface ReaderDocumentDetail extends ReaderDocumentSummary {
  blocks: ReaderBlock[]
  sections: ReaderSection[]
  pages: ReaderPageMeta[]
  parser_version?: string | null
  created_at?: string | null
  updated_at?: string | null
  note?: string | null
}

export interface ReaderVersion {
  id: number
  document_id: number
  kind: string
  version_no: number
  file_name?: string | null
  file_sha256?: string | null
  file_size_bytes?: number | null
  is_immutable?: boolean
  hash_verified?: boolean
  engine?: string | null
  task_id?: string | null
  block_count?: number | null
  page_map?: Record<string, unknown> | null
  highlight_summary?: Record<string, unknown> | null
  layout_warnings?: string[] | null
  created_at?: string | null
  pdf_url?: string | null
  text_url?: string | null
}

export interface ReaderVersionList {
  document_id: number
  items: ReaderVersion[]
  kinds?: string[]
  note?: string | null
  total: number
}

export interface ReaderState {
  document_id: number
  current_block?: string | null
  offset?: number | null
  mode?: string | null
  font_size?: number | null
  understood_blocks?: string[] | null
  favorite_terms?: string[] | null
  revision?: number
  updated_at?: string | null
}

export interface ReaderAnnotation {
  id: number
  document_id: number
  version_id?: number | null
  uid?: string | null
  kind?: string | null
  note?: string | null
  quote_text?: string | null
  quote_sha256?: string | null
  block_id?: string | null
  anchor_text?: string | null
  anchor_sha256?: string | null
  anchor_resolved?: boolean
  align_status?: string | null
  page?: number | null
  bbox?: number[] | null
  revision?: number
  created_at?: string | null
  updated_at?: string | null
}

export interface ReaderAnnotationPage {
  items: ReaderAnnotation[]
  total: number
  page?: number
  page_size?: number
}

export function listReaderDocuments(page = 1, pageSize = 20): Promise<ReaderDocumentPage> {
  return get<ReaderDocumentPage>('/reader/documents', { query: { page, page_size: pageSize } })
}

/** 登记阅读文档（解析原文 PDF、自动登记 original 版本）；已登记时返回既有记录 */
export function registerReaderDocument(
  paperId: number,
  force = false,
): Promise<ReaderDocumentSummary> {
  return post<ReaderDocumentSummary>('/reader/documents', {
    body: { paper_id: paperId, force },
    timeoutMs: 180_000,
  })
}

export function fetchReaderDocument(documentId: number): Promise<ReaderDocumentDetail> {
  return get<ReaderDocumentDetail>(`/reader/documents/${documentId}`)
}

export function listReaderVersions(documentId: number): Promise<ReaderVersionList> {
  return get<ReaderVersionList>(`/reader/documents/${documentId}/versions`)
}

/** 把翻译产物登记为不可变版本（kind: chinese，中文译本） */
export function registerReaderVersion(
  documentId: number,
  kind: 'chinese',
  taskId?: string,
): Promise<ReaderVersion> {
  return post<ReaderVersion>(`/reader/documents/${documentId}/versions`, {
    body: { kind, task_id: taskId ?? null },
    timeoutMs: 60_000,
  })
}

export function readerVersionPdfUrl(documentId: number, versionId: number): string {
  return apiUrl(`/reader/documents/${documentId}/versions/${versionId}/pdf`)
}

export interface ReaderVersionText {
  document_id?: number
  version_id?: number
  kind?: string
  blocks?: Array<{ block_id?: string; page?: number; source_text?: string; target_text?: string }>
  page_map?: Record<string, unknown> | null
  [key: string]: unknown
}

export function fetchReaderVersionText(
  documentId: number,
  versionId: number,
): Promise<ReaderVersionText> {
  return get<ReaderVersionText>(`/reader/documents/${documentId}/versions/${versionId}/text`)
}

export function fetchReaderState(documentId: number): Promise<ReaderState> {
  return get<ReaderState>(`/reader/state/${documentId}`)
}

export function updateReaderState(
  documentId: number,
  patchBody: Partial<
    Pick<ReaderState, 'current_block' | 'offset' | 'mode' | 'font_size' | 'understood_blocks' | 'favorite_terms'>
  >,
): Promise<ReaderState> {
  return patch<ReaderState>(`/reader/state/${documentId}`, { body: patchBody })
}

export function listReaderAnnotations(
  documentId: number,
  options: { q?: string; kind?: string; align_status?: string; page?: number; page_size?: number } = {},
): Promise<ReaderAnnotationPage> {
  return get<ReaderAnnotationPage>(`/reader/documents/${documentId}/annotations`, {
    query: { page_size: 100, ...options },
  })
}

export function createReaderAnnotation(
  documentId: number,
  body: {
    version_id: number
    quote_text: string
    block_id?: string | null
    note?: string | null
    kind?: string
    page?: number | null
  },
): Promise<ReaderAnnotation> {
  return post<ReaderAnnotation>(`/reader/documents/${documentId}/annotations`, { body })
}

/** 更新批注：后端是 **PUT**（不是 PATCH），且必须携带 `revision`，冲突返回 409 */
export function updateReaderAnnotation(
  documentId: number,
  annotationId: number,
  body: { revision: number; quote_text?: string; note?: string; kind?: string; block_id?: string },
): Promise<ReaderAnnotation> {
  return put<ReaderAnnotation>(
    `/reader/documents/${documentId}/annotations/${annotationId}`,
    { body },
  )
}

export function deleteReaderAnnotation(documentId: number, annotationId: number): Promise<void> {
  return del<void>(`/reader/documents/${documentId}/annotations/${annotationId}`)
}

export function alignReaderAnnotation(
  documentId: number,
  annotationId: number,
): Promise<ReaderAnnotation> {
  return post<ReaderAnnotation>(
    `/reader/documents/${documentId}/annotations/${annotationId}/align`,
    {},
  )
}

export function readerArchiveUrl(documentId: number): string {
  return apiUrl(`/reader/documents/${documentId}/archive`)
}

export function restoreReaderDocument(documentId: number): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>(`/reader/documents/${documentId}/restore`, {
    timeoutMs: 120_000,
  })
}
