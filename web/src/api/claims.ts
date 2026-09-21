/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 证据链与 Claim 数据层（WP13-T6）：封装 WP13 的三个端点。
 *
 * 分层约定（contracts.code_style.frontend）：组件内**禁止**直接 fetch，
 * 一切请求经 `src/api/*.ts`；写操作由 `client.ts` 自动带 `X-Owner-Token`。
 *
 * 覆盖端点（附录 B.5）：
 *   GET  /evidence/{type}/{id}        解析多态证据为可跳转结构（附录 E.3）
 *   GET  /evidence/types              受控值域 + fulltext_gate 阈值
 *   GET  /drafts/{id}/claims          Claim 列表（三态 + 证据数 + 覆盖率统计）
 *   POST /drafts/{id}/verify-evidence 重跑校验，返回 unsupported_spans 与覆盖率（Owner）
 */
import { get, post, type RequestOptions } from './client'

/** contracts.enums.claim_status */
export type ClaimStatus = 'supported' | 'contradicted' | 'insufficient'

/** contracts.evidence_rules.evidence_types */
export type EvidenceType =
  | 'paper_span'
  | 'card_field'
  | 'experiment_run'
  | 'experiment_passport'
  | 'decision'

/** ``verify_span`` 三 verdict（哈希优先） */
export type EvidenceVerdict = 'valid' | 'valid_by_hash' | 'invalid'

/** ``paper_cards`` 的 8 字段（card_field 证据的取值白名单） */
export const CARD_FIELDS = [
  'research_problem',
  'core_method',
  'key_innovation',
  'technical_route',
  'experimental_setup',
  'main_conclusions',
  'limitations',
  'transferable',
] as const

/** evidence_id 语义：evidences.id 或该类型自身主键 */
export type EvidenceIdKind = 'evidence' | 'native'

/** ``evidences`` 行 + 附录 E.3 解析结果（含平铺别名，见 api/v1/evidence.py:_flatten） */
export interface EvidenceDetail {
  /** 平铺别名，等同 evidence_id */
  id?: number | null
  evidence_id: number
  evidence_type: EvidenceType | string
  owner_type?: string | null
  owner_id?: number | null
  weight?: number | null
  /** 论文摘要；字段缺失一律 null，不编造 */
  paper?: {
    id?: number | null
    title?: string | null
    venue?: string | null
    venue_source?: string | null
    published_at?: string | null
    doi?: string | null
    source?: string | null
    external_id?: string | null
  } | null
  paper_id?: number | null
  /** 定位必须携带 document_version（禁止只用裸字符偏移） */
  document_version?: string | null
  span?: {
    paper_span_id?: number
    section_name?: string | null
    page_number?: number | null
    bbox?: Record<string, number> | null
    char_start?: number | null
    char_end?: number | null
  } | null
  card?: {
    card_id?: number | null
    version?: number | null
    field?: string | null
    value?: string | null
    value_available?: boolean
    unavailable_reason?: string | null
  } | null
  quote_text?: string | null
  quote_sha256?: string | null
  /** 平铺别名：verification.verdict */
  verdict?: EvidenceVerdict | string | null
  verification?: {
    verdict?: EvidenceVerdict | string
    hash_match?: boolean
    offset_match?: boolean | null
    expected_quote_sha256?: string
    stored_quote_sha256?: string
    reason?: string
  } | null
  /** fulltext_gate 如实披露 */
  gate?: {
    ok?: boolean
    parse_status?: string | null
    coverage?: number | null
    reason?: string | null
    coverage_note?: string | null
    evidence_scope?: string | null
  } | null
  /** 平铺别名：gate.coverage */
  coverage?: number | null
  /** 平铺别名：gate.ok */
  spans_allowed?: boolean
  section_name?: string | null
  page_number?: number | null
  char_start?: number | null
  char_end?: number | null
  evidence_scope?: string | null
  jump_url?: string | null
  warnings?: string[]
  id_kind?: EvidenceIdKind
  [key: string]: unknown
}

/** ``GET /drafts/{id}/claims`` 的单条 Claim */
export interface DraftClaim {
  id: number
  claim_id: number
  draft_id: number
  section_heading?: string | null
  claim_text?: string | null
  /** ``draft_claims`` 无文本列时的等价别名 */
  text?: string | null
  is_factual: boolean
  support_status: ClaimStatus | string
  /** ``support_status`` 的前端别名（ClaimBadge / DraftViewer 用） */
  status: ClaimStatus | string
  status_reason?: string | null
  evidence_count: number
  evidence_ids: number[]
  evidence: Array<Record<string, unknown>>
  created_at?: string | null
  [key: string]: unknown
}

export interface DraftClaimCounts {
  supported: number
  contradicted: number
  insufficient: number
  factual: number
  non_factual?: number
  total: number
}

export interface DraftClaimsResponse {
  draft_id: number
  /** false = paper_drafts 尚无该行（WP14 未产出）：如实未就绪，不是 404 */
  draft_found: boolean
  items: DraftClaim[]
  total: number
  page: number
  page_size: number
  counts: DraftClaimCounts
  /** supported ÷ 事实性总数；事实性总数为 0 时为 null（不用 0/1 冒充） */
  claim_coverage: number | null
  claim_coverage_persisted?: number | null
  coverage_formula?: string
  coverage_note?: string | null
  unsupported_spans: UnsupportedSpan[]
  warnings: string[]
  note?: string | null
}

/** 事实性但非 supported 的段落（供前端整段高亮） */
export interface UnsupportedSpan {
  claim_id: number | null
  index?: number | null
  section_heading?: string | null
  claim_text?: string | null
  char_start?: number | null
  char_end?: number | null
  support_status: ClaimStatus | string
  status_reason?: string | null
  evidence_count?: number
}

export interface VerifyEvidenceRequest {
  /** 调试入口：草稿未落库时按此正文直接校验（只读，不写库） */
  content_md?: string | null
  persist?: boolean
  use_llm_conflict?: boolean
  max_claims?: number
}

export interface VerifyEvidenceResponse {
  draft_id: number | null
  draft_found: boolean
  persisted: boolean
  /** ``draft_persisted``（读库校验）或 ``content_md_debug``（纯文本只读校验） */
  mode?: string
  claim_coverage: number | null
  counts: Record<string, number>
  claims: Array<Record<string, unknown>>
  unsupported_spans: UnsupportedSpan[]
  unsupported: UnsupportedSpan[]
  unsupported_total?: number
  citation_map: Record<string, string>
  citation_count?: number
  citation_source?: string
  conflict_detection?: string
  coverage_formula?: string
  coverage_note?: string | null
  warnings: string[]
  verified_at?: string
  [key: string]: unknown
}

/** ``ClaimBadge`` 点击后向父组件回传的载荷 */
export interface ClaimSelectPayload {
  claim_id: number | null
  status: string
  is_factual: boolean
  evidence_count: number
  evidence_ids: number[]
  char_start: number | null
  char_end: number | null
  reason: string | null
}

/** 读一条证据（附录 E.3）。`idKind='native'` 时 id 为该类型自身主键。 */
export function getEvidenceDetail(
  evidenceType: EvidenceType | string,
  evidenceId: number | string,
  options: { idKind?: EvidenceIdKind; cardField?: string; signal?: AbortSignal } = {},
) {
  const query: RequestOptions['query'] = {}
  if (options.idKind && options.idKind !== 'evidence') query.id_kind = options.idKind
  if (options.cardField && (evidenceType === 'card_field' && options.idKind === 'native')) {
    query.card_field = options.cardField
  }
  return get<EvidenceDetail>(`/evidence/${encodeURIComponent(evidenceType)}/${evidenceId}`, {
    query,
    signal: options.signal,
  })
}

/** 受控证据类型 + fulltext_gate 阈值（前端不硬编码 0.60） */
export function getEvidenceTypes(signal?: AbortSignal) {
  return get<{
    evidence_types: EvidenceType[]
    id_kinds: EvidenceIdKind[]
    fulltext_gate: { parse_status: string; min_coverage: number; rule: string }
  }>('/evidence/types', { signal })
}

/** Claim 列表（草稿不存在时 draft_found=false，不是错误） */
export function getDraftClaims(
  draftId: number | string,
  options: {
    supportStatus?: ClaimStatus
    factualOnly?: boolean
    page?: number
    pageSize?: number
    signal?: AbortSignal
  } = {},
) {
  const query: RequestOptions['query'] = {}
  if (options.supportStatus) query.support_status = options.supportStatus
  if (options.factualOnly) query.factual_only = true
  if (options.page) query.page = options.page
  if (options.pageSize) query.page_size = options.pageSize
  return get<DraftClaimsResponse>(`/drafts/${draftId}/claims`, { query, signal: options.signal })
}

/** 重跑 Claim 级证据校验（Owner 面；返回 unsupported_spans 与 claim_coverage） */
export function verifyDraftEvidence(
  draftId: number | string,
  body: VerifyEvidenceRequest = {},
  signal?: AbortSignal,
) {
  return post<VerifyEvidenceResponse>(`/drafts/${draftId}/verify-evidence`, {
    body,
    signal,
    timeoutMs: 120_000,
  })
}

/** 本地按 Claim 列表复算覆盖率（与后端同一公式，用于交叉核对，不做权威来源） */
export function computeCoverageFromClaims(items: DraftClaim[]): number | null {
  const factual = items.filter((item) => item.is_factual)
  if (factual.length === 0) return null
  const supported = factual.filter((item) => item.status === 'supported').length
  return Math.round((supported / factual.length) * 1000) / 1000
}

/** 覆盖率展示：null → 「无定义」，绝不显示 0 */
export function formatCoverage(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '无定义'
  return `${Math.round(value * 1000) / 10}%`
}
