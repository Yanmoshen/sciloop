import { get, post } from './client'

/** Fixed 20 expert stages used by the idea research pipeline. */
export const IDEA_EXPERTS = [
  ['evidence', '论文证据提取'],
  ['synthesis', '跨论文综述'],
  ['gaps', '研究空白发现'],
  ['question', '研究问题重构'],
  ['seeds', '发散 Idea 生成'],
  ['refinement', '方法迭代'],
  ['transfer', '场景迁移'],
  ['combination', '技术融合'],
  ['paradigm', '范式拓展'],
  ['novelty', '新颖性与重复检查'],
  ['method_feasibility', '方法可行性'],
  ['data_feasibility', '数据可行性'],
  ['resources', '算力与资源'],
  ['experiments', '实验设计'],
  ['falsifiability', '指标与可证伪性'],
  ['risk', '风险、伦理与合规'],
  ['impact', '科学影响与应用价值'],
  ['critique', '对抗式批评'],
  ['scoring', '独立评分与证据审计'],
  ['ranking', '决策排序'],
] as const

export type IdeaExpertKey = (typeof IDEA_EXPERTS)[number][0]
export type IdeaRunStatus = 'idle' | 'queued' | 'running' | 'completed' | 'awaiting_confirmation' | 'failed' | 'cancelled' | string
export type IdeaExpertStatus = 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'skipped' | string

export interface IdeaExpertStage {
  key: IdeaExpertKey | string
  name?: string | null
  label?: string | null
  order?: number
  status: IdeaExpertStatus
  message?: string | null
  error?: string | null
  started_at?: string | null
  completed_at?: string | null
  duration_ms?: number | null
  output?: unknown
  raw_output?: string | null
  model_ref?: string | null
  prompt_version?: string | null
  attempts?: number
  cost_usd?: number | null
}

export interface IdeaRunCandidate {
  id: number | string
  title: string
  mechanism?: string | null
  summary?: string | null
  content?: string | null
  research_question?: string | null
  hypothesis?: string | null
  innovation?: string | null
  evidence?: Array<Record<string, unknown>>
  evidence_count?: number
  analyses?: Record<string, unknown>
  scores?: Record<string, number | null>
  /** 各维度打分的理由（人话），键与 `scores` 一致 —— 见 `normalizeDimensions` */
  score_reasons?: Record<string, string>
  /** 各维度打分引用的证据编号（如 `E7`），键与 `scores` 一致 */
  score_refs?: Record<string, string[]>
  total_score?: number | null
  rank?: number | null
  recommendation?: string | null
  decision?: string | null
  risks?: string[]
}

export interface IdeaExpertRun {
  id: number | string
  run_id?: number | string
  aggregation_id: number | null
  project_id?: number | null
  status: IdeaRunStatus
  model_ref?: string | null
  paper_ids?: number[]
  stages: IdeaExpertStage[]
  candidates: IdeaRunCandidate[]
  ranking?: IdeaRunCandidate[]
  selected_candidate_id?: number | string | null
  confirmation_status?: 'pending' | 'confirmed' | 'rejected' | string
  error?: string | null
  failure_reason?: string | null
  completed_experts?: number
  failed_experts?: string[]
  experts_total?: number
  candidate_count?: number
  expert_count?: number
  created_at?: string | null
  completed_at?: string | null
  generation_seconds?: number | null
  updated_at?: string | null
  [key: string]: unknown
}

export interface CreateIdeaRunBody {
  aggregation_id: number
  paper_ids?: number[]
  project_id?: number | null
  candidate_count_per_direction?: number
  model_ref?: string | null
}

/**
 * 把 `score.dimensions` 拆成三样东西。
 *
 * ⚠️ 2026-10-14 修：`dimensions` 每一项的真实形态是 `{score, reason, evidence_refs}`，
 * 改前把它**当数字**直接塞进 `scores`，模板里 `{{ score }}` 就把对象渲染成了
 * `{"score":88,"reason":"…"}` 这种 JSON 文本（研究者报的"右侧还是 JSON，要人话"）。
 * 现在分开：分数（数字）/ 理由（人话）/ 引用证据编号，界面各取所需。
 */
function normalizeDimensions(raw: unknown): {
  scores?: Record<string, number | null>
  reasons?: Record<string, string>
  refs?: Record<string, string[]>
} {
  if (!raw || typeof raw !== 'object') return {}
  const scores: Record<string, number | null> = {}
  const reasons: Record<string, string> = {}
  const refs: Record<string, string[]> = {}
  for (const [key, item] of Object.entries(raw as Record<string, unknown>)) {
    if (item && typeof item === 'object') {
      const value = item as Record<string, unknown>
      scores[key] = typeof value.score === 'number' ? value.score : null
      if (typeof value.reason === 'string' && value.reason.trim()) reasons[key] = value.reason.trim()
      const listed = Array.isArray(value.evidence_refs) ? value.evidence_refs.map(String) : []
      if (listed.length) refs[key] = listed
      continue
    }
    scores[key] = typeof item === 'number' ? item : null
  }
  return {
    scores,
    reasons: Object.keys(reasons).length ? reasons : undefined,
    refs: Object.keys(refs).length ? refs : undefined,
  }
}

/** 反转义 JSON 字符串里的 `\"` `\n` 这类转义，让摘录读起来是正常句子。 */
function unescapeJsonText(raw: string): string {
  return raw
    .replace(/\\"/g, '"')
    .replace(/\\n/g, ' ')
    .replace(/\\t/g, ' ')
    .replace(/\\u([0-9a-fA-F]{4})/g, (_match, code: string) => String.fromCharCode(parseInt(code, 16)))
    .replace(/\\\\/g, '\\')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * 证据里的 `quote_text` 可能是 JSON 字符串（论文卡字段原样存），摊平成可读文本。
 *
 * ⚠️ 管道里这份 JSON **在 2500 字处被截断**（实测 `Unterminated string`），所以不能只靠
 * `JSON.parse`；解析失败时退回正则把 `"键":"值"` 里的**值**抠出来，保证界面上永远不会出现
 * `[{"limitation":"…` 这种原文（研究者口径：证据链要人话）。
 */
function flattenQuote(value: unknown): string {
  if (typeof value !== 'string') return value == null ? '' : String(value)
  const text = value.trim()
  if (!text.startsWith('{') && !text.startsWith('[')) return text

  try {
    const parsed: unknown = JSON.parse(text)
    const parts: string[] = []
    const walk = (node: unknown): void => {
      if (typeof node === 'string') parts.push(node)
      else if (Array.isArray(node)) node.forEach(walk)
      else if (node && typeof node === 'object') Object.values(node as Record<string, unknown>).forEach(walk)
    }
    walk(parsed)
    const joined = parts.map((part) => part.trim()).filter(Boolean).join('；')
    if (joined) return joined
  } catch {
    /* 截断的 JSON 走下面的正则兜底 */
  }

  const values = [...text.matchAll(/"\s*:\s*"((?:[^"\\]|\\.)*)"/g)].map((match) => unescapeJsonText(match[1]))
  return values.filter((item) => item.length > 0).join('；') || unescapeJsonText(text)
}

/** 候选 Idea 的证据链：只留研究者要看的四件事，其余原样带回。 */
function normalizeEvidence(raw: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item) => item && typeof item === 'object')
    .map((item) => {
      const value = item as Record<string, unknown>
      return { ...value, quote_text: flattenQuote(value.quote_text) }
    })
}

function normalizeRun(payload: unknown, aggregationId?: number): IdeaExpertRun {
  const raw = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
  const source = (raw.run && typeof raw.run === 'object' ? raw.run : raw) as Record<string, unknown>
  const rawStages = Array.isArray(source.stages) ? source.stages : Array.isArray(source.experts) ? source.experts : []
  const stages = IDEA_EXPERTS.map(([key, label], index) => {
    const found = rawStages.find((item) => {
      const value = item && typeof item === 'object' ? item as Record<string, unknown> : {}
      return String(value.key ?? value.expert_key ?? value.id ?? '') === key || Number(value.order ?? value.expert_number) === index + 1
    }) as Record<string, unknown> | undefined
    return {
      key,
      name: label,
      label,
      order: index + 1,
      status: (found?.status as IdeaExpertStatus | undefined) ?? (source.status === 'completed' ? 'completed' : 'pending'),
      message: (found?.message as string | null | undefined) ?? null,
      error: (found?.error as string | null | undefined) ?? null,
      started_at: (found?.started_at as string | null | undefined) ?? null,
      completed_at: (found?.completed_at as string | null | undefined) ?? null,
      duration_ms: (found?.duration_ms as number | null | undefined) ?? null,
      output: found?.output,
      raw_output: (found?.raw_output as string | null | undefined) ?? null,
      model_ref: (found?.model_ref as string | null | undefined) ?? null,
      prompt_version: (found?.prompt_version as string | null | undefined) ?? null,
      attempts: typeof found?.attempts === 'number' ? found.attempts : undefined,
      cost_usd: typeof found?.cost_usd === 'number' ? found.cost_usd : null,
    }
  })
  const rawCandidates = Array.isArray(source.candidates) ? source.candidates : Array.isArray(source.items) ? source.items : []
  const candidates = rawCandidates.map((item, index) => {
    const value = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>
    const payload = value.payload && typeof value.payload === 'object' ? value.payload as Record<string, unknown> : {}
    const scoreObject = value.score && typeof value.score === 'object' ? value.score as Record<string, unknown> : null
    const dimensions = normalizeDimensions(scoreObject?.dimensions ?? value.scores ?? payload.scores)
    const scoreTotal = scoreObject && typeof scoreObject.total === 'number' ? scoreObject.total : null
    return {
      ...payload,
      ...value,
      id: (value.id ?? value.idea_id ?? `candidate-${index + 1}`) as number | string,
      title: String(value.title ?? `候选 Idea ${index + 1}`),
      mechanism: (value.mechanism ?? value.direction) as string | null | undefined,
      summary: (value.summary ?? payload.summary) as string | null | undefined,
      content: (value.content ?? payload.content) as string | null | undefined,
      evidence: normalizeEvidence(value.evidence),
      scores: dimensions.scores,
      score_reasons: dimensions.reasons,
      score_refs: dimensions.refs,
      total_score: typeof value.total_score === 'number' ? value.total_score : typeof value.score === 'number' ? value.score : scoreTotal,
      rank: typeof value.rank === 'number' ? value.rank : index + 1,
    }
  })
  return {
    ...source,
    id: (source.id ?? source.run_id ?? `run-${Date.now()}`) as number | string,
    run_id: source.run_id as number | string | undefined,
    aggregation_id: Number(source.aggregation_id ?? aggregationId ?? 0) || null,
    status: String(source.status ?? 'queued'),
    stages,
    candidates,
    ranking: Array.isArray(source.ranking) ? candidates : candidates.slice().sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999)),
    paper_ids: Array.isArray(source.paper_ids) ? source.paper_ids.map(Number) : [],
    selected_candidate_id: (source.selected_candidate_id as number | string | null | undefined) ?? null,
    confirmation_status: source.confirmation_status as IdeaExpertRun['confirmation_status'],
  }
}

export async function createIdeaExpertRun(body: CreateIdeaRunBody, signal?: AbortSignal): Promise<IdeaExpertRun> {
  const payload = await post<unknown>('/idea-runs', { body, signal, timeoutMs: 180_000 })
  return normalizeRun(payload, body.aggregation_id)
}

export async function getIdeaExpertRun(id: number | string, signal?: AbortSignal): Promise<IdeaExpertRun> {
  const payload = await get<unknown>(`/idea-runs/${encodeURIComponent(String(id))}`, { signal })
  return normalizeRun(payload)
}

export async function listIdeaExpertRuns(aggregationId: number, signal?: AbortSignal): Promise<IdeaExpertRun[]> {
  const payload = await get<{ items?: unknown[] }>(`/idea-runs`, {
    query: { aggregation_id: aggregationId },
    signal,
  })
  return (payload.items ?? []).map((item) => normalizeRun({ run: item }, aggregationId))
}
export async function confirmIdeaExpertRun(
  id: number | string,
  body: { candidate_id: number | string; confirmed: boolean; note?: string },
  signal?: AbortSignal,
): Promise<IdeaExpertRun> {
  const payload = await post<unknown>(`/idea-runs/${encodeURIComponent(String(id))}/confirm`, { body, signal })
  return normalizeRun(payload)
}
