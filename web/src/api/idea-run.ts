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
  created_at?: string | null
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
    }
  })
  const rawCandidates = Array.isArray(source.candidates) ? source.candidates : Array.isArray(source.items) ? source.items : []
  const candidates = rawCandidates.map((item, index) => {
    const value = (item && typeof item === 'object' ? item : {}) as Record<string, unknown>
    const payload = value.payload && typeof value.payload === 'object' ? value.payload as Record<string, unknown> : {}
    const scoreObject = value.score && typeof value.score === 'object' ? value.score as Record<string, unknown> : null
    const scoreDimensions = scoreObject?.dimensions && typeof scoreObject.dimensions === 'object'
      ? scoreObject.dimensions as Record<string, number | null>
      : undefined
    const scoreTotal = scoreObject && typeof scoreObject.total === 'number' ? scoreObject.total : null
    return {
      ...payload,
      ...value,
      id: (value.id ?? value.idea_id ?? `candidate-${index + 1}`) as number | string,
      title: String(value.title ?? `候选 Idea ${index + 1}`),
      mechanism: (value.mechanism ?? value.direction) as string | null | undefined,
      summary: (value.summary ?? payload.summary) as string | null | undefined,
      content: (value.content ?? payload.content) as string | null | undefined,
      evidence: Array.isArray(value.evidence) ? value.evidence as Array<Record<string, unknown>> : [],
      scores: (value.scores ?? scoreDimensions ?? payload.scores) as Record<string, number | null> | undefined,
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
export async function confirmIdeaExpertRun(
  id: number | string,
  body: { candidate_id: number | string; confirmed: boolean; note?: string },
  signal?: AbortSignal,
): Promise<IdeaExpertRun> {
  const payload = await post<unknown>(`/idea-runs/${encodeURIComponent(String(id))}/confirm`, { body, signal })
  return normalizeRun(payload)
}
