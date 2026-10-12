<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getAggregation } from '@/api/idea'
import { searchPapers, type PaperSearchItem } from '@/api/papers'
import {
  confirmIdeaExpertRun,
  createIdeaExpertRun,
  getIdeaExpertRun,
  IDEA_EXPERTS,
  type IdeaExpertRun,
  type IdeaExpertStage,
} from '@/api/idea-run'

const route = useRoute()
const router = useRouter()
const aggregationId = computed(() => {
  const raw = route.params.aggregationId
  const value = Number(Array.isArray(raw) ? raw[0] : raw)
  return Number.isFinite(value) && value > 0 ? value : null
})

const run = ref<IdeaExpertRun | null>(null)
const paperIds = ref<number[]>([])
const paperTitles = ref<Record<number, string>>({})
const loading = ref(true)
const starting = ref(false)
const confirming = ref(false)
const notice = ref('')
const selectedCandidateId = ref<number | string | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null

const stages = computed(() => run.value?.stages ?? IDEA_EXPERTS.map(([key, label], index) => ({
  key,
  label,
  order: index + 1,
  status: 'pending',
} as IdeaExpertStage)))
const completedStages = computed(() => stages.value.filter((stage) => stage.status === 'completed').length)
const activeCandidate = computed(() => run.value?.candidates.find((item) => String(item.id) === String(selectedCandidateId.value)) ?? run.value?.candidates[0] ?? null)
const isTerminal = computed(() => ['completed', 'awaiting_confirmation', 'failed', 'cancelled'].includes(String(run.value?.status)))
const canConfirm = computed(() => Boolean(run.value && activeCandidate.value && ['completed', 'awaiting_confirmation'].includes(String(run.value.status)) && run.value.confirmation_status !== 'confirmed'))

function stageLabel(stage: IdeaExpertStage): string {
  return stage.label || stage.name || String(stage.key)
}
function stageStatus(stage: IdeaExpertStage): string {
  if (stage.status === 'completed') return '已完成'
  if (stage.status === 'running') return '运行中'
  if (stage.status === 'failed') return '失败'
  if (stage.status === 'skipped') return '跳过'
  return '等待'
}
function stageClass(stage: IdeaExpertStage): string {
  return `stage--${stage.status}`
}
function scoreTotal(candidate: NonNullable<IdeaExpertRun['candidates']>[number]): string {
  return typeof candidate.total_score === 'number' ? `${Math.round(candidate.total_score)} 分` : '待评分'
}
function paperLabel(id: number): string {
  return `#${id} · ${paperTitles.value[id] || '论文'}`
}
function ownerError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function load(): Promise<void> {
  if (aggregationId.value === null) {
    notice.value = '没有指定研究任务'
    loading.value = false
    return
  }
  loading.value = true
  notice.value = ''
  try {
    const [aggregation, papers] = await Promise.all([
      getAggregation(aggregationId.value),
      searchPapers({ pageSize: 100 }).catch(() => null),
    ])
    paperIds.value = aggregation.paper_ids ?? []
    if (papers) paperTitles.value = Object.fromEntries((papers.items ?? []).map((paper: PaperSearchItem) => [paper.id, paper.title]))
    const runId = route.query.run_id
    if (runId) run.value = await getIdeaExpertRun(String(runId))
  } catch (error) {
    notice.value = ownerError(error)
  } finally {
    loading.value = false
    schedulePoll()
  }
}

function schedulePoll(): void {
  if (pollTimer) clearTimeout(pollTimer)
  if (!run.value || isTerminal.value) return
  pollTimer = setTimeout(async () => {
    try {
      run.value = await getIdeaExpertRun(run.value?.id ?? '')
    } catch (error) {
      notice.value = ownerError(error)
    } finally {
      schedulePoll()
    }
  }, 2500)
}

async function startRun(): Promise<void> {
  if (aggregationId.value === null || starting.value) return
  starting.value = true
  notice.value = ''
  try {
    run.value = await createIdeaExpertRun({
      aggregation_id: aggregationId.value,
      paper_ids: paperIds.value,
      candidate_count_per_direction: 2,
    })
    selectedCandidateId.value = run.value.candidates[0]?.id ?? null
    await router.replace({ query: { ...route.query, run_id: String(run.value.id) } })
    schedulePoll()
  } catch (error) {
    notice.value = ownerError(error)
  } finally {
    starting.value = false
  }
}

async function confirm(decision: 'approve' | 'reject'): Promise<void> {
  if (!run.value || !activeCandidate.value || confirming.value) return
  confirming.value = true
  notice.value = ''
  try {
    run.value = await confirmIdeaExpertRun(run.value.id, {
      candidate_id: activeCandidate.value.id,
      confirmed: decision === 'approve',
    })
  } catch (error) {
    notice.value = ownerError(error)
  } finally {
    confirming.value = false
  }
}

function back(): void {
  void router.push('/ideas')
}

onMounted(load)
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
})
</script>

<template>
  <section class="expert-workbench">
    <header class="workbench-head">
      <button class="link-btn" type="button" @click="back">← 研究构想</button>
      <div>
        <p class="eyebrow">IDEA RESEARCH RUN</p>
        <h1>专家 Idea 工作台</h1>
      </div>
      <span class="spacer" />
      <span v-if="run" class="status-pill" :class="`status-pill--${run.status}`">{{ run.status }}</span>
      <button class="btn btn--primary" type="button" :disabled="starting || Boolean(run && !isTerminal)" @click="startRun">
        {{ starting ? '启动中…' : run ? '运行已创建' : '启动 20 专家运行' }}
      </button>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>
    <p v-if="loading" class="empty">正在加载研究任务…</p>

    <template v-else>
      <article class="panel input-panel">
        <div>
          <p class="panel-kicker">输入快照</p>
          <h2>已选择论文</h2>
        </div>
        <div class="paper-tags">
          <span v-for="id in paperIds" :key="id" class="tag">{{ paperLabel(id) }}</span>
          <span v-if="paperIds.length === 0" class="muted">暂无论文输入</span>
        </div>
        <span class="input-note">只使用这些论文的解析卡片、解析总结和全文证据</span>
      </article>

      <article class="panel">
        <header class="panel-head">
          <div>
            <p class="panel-kicker">固定链路</p>
            <h2>20 个专家阶段</h2>
          </div>
          <span class="muted">{{ completedStages }}/20 已完成</span>
        </header>
        <div class="stage-grid">
          <div v-for="stage in stages" :key="stage.key" class="stage" :class="stageClass(stage)">
            <span class="stage-index">{{ stage.order }}</span>
            <span class="stage-copy">
              <strong>{{ stageLabel(stage) }}</strong>
              <small>{{ stageStatus(stage) }}</small>
              <small v-if="stage.error" class="error-text">{{ stage.error }}</small>
            </span>
            <span class="stage-dot" aria-hidden="true" />
          </div>
        </div>
      </article>

      <div class="results-layout">
        <article class="panel candidates-panel">
          <header class="panel-head">
            <div>
              <p class="panel-kicker">候选池</p>
              <h2>8 条候选 Idea</h2>
            </div>
            <span class="muted">四个方向 · 每个 2 条</span>
          </header>
          <div v-if="run?.candidates.length" class="candidate-list">
            <button
              v-for="candidate in run.candidates"
              :key="candidate.id"
              type="button"
              class="candidate-row"
              :class="{ 'candidate-row--active': String(candidate.id) === String(activeCandidate?.id) }"
              @click="selectedCandidateId = candidate.id"
            >
              <span class="candidate-rank">{{ candidate.rank ?? '—' }}</span>
              <span class="candidate-info">
                <strong>{{ candidate.title }}</strong>
                <small>{{ candidate.mechanism || '未标注方向' }}</small>
              </span>
              <span class="candidate-score">{{ scoreTotal(candidate) }}</span>
            </button>
          </div>
          <p v-else class="empty">运行完成后，候选 Idea 会出现在这里。</p>
        </article>

        <article class="panel detail-panel">
          <header class="panel-head">
            <div>
              <p class="panel-kicker">候选详情</p>
              <h2>{{ activeCandidate?.title || '等待候选 Idea' }}</h2>
            </div>
            <span v-if="activeCandidate" class="score-large">{{ scoreTotal(activeCandidate) }}</span>
          </header>
          <template v-if="activeCandidate">
            <p class="candidate-summary">{{ activeCandidate.summary || activeCandidate.content || '暂无摘要' }}</p>
            <dl class="detail-list">
              <div v-if="activeCandidate.research_question"><dt>研究问题</dt><dd>{{ activeCandidate.research_question }}</dd></div>
              <div v-if="activeCandidate.hypothesis"><dt>核心假设</dt><dd>{{ activeCandidate.hypothesis }}</dd></div>
              <div v-if="activeCandidate.innovation"><dt>预期创新</dt><dd>{{ activeCandidate.innovation }}</dd></div>
              <div><dt>证据</dt><dd>{{ activeCandidate.evidence_count ?? activeCandidate.evidence?.length ?? 0 }} 条论文证据</dd></div>
            </dl>
            <div v-if="activeCandidate.scores" class="score-grid">
              <div v-for="(score, key) in activeCandidate.scores" :key="key"><span>{{ key }}</span><strong>{{ score ?? '—' }}</strong></div>
            </div>
            <footer class="confirm-bar">
              <span v-if="run?.confirmation_status === 'confirmed'" class="confirmed">已人工确认</span>
              <span v-else class="muted">排序完成后必须人工确认才能进入任务书</span>
              <span class="spacer" />
              <button class="btn" type="button" :disabled="!canConfirm || confirming" @click="confirm('reject')">暂不选择</button>
              <button class="btn btn--primary" type="button" :disabled="!canConfirm || confirming" @click="confirm('approve')">{{ confirming ? '提交中…' : '确认进入任务书' }}</button>
            </footer>
          </template>
          <p v-else class="empty">专家完成生成、分析、评分和排序后查看详情。</p>
        </article>
      </div>
    </template>
  </section>
</template>

<style scoped>
.expert-workbench { padding: var(--space-4); display: flex; flex-direction: column; gap: var(--space-3); }
.workbench-head, .panel-head, .confirm-bar { display: flex; align-items: center; gap: var(--space-3); flex-wrap: wrap; }
.workbench-head h1, .panel-head h2 { margin: 0; }
.workbench-head h1 { font-size: var(--font-size-xl); }
.eyebrow, .panel-kicker { margin: 0 0 4px; color: var(--color-text-secondary); font: 600 var(--font-size-2xs)/1.2 var(--font-family-mono); letter-spacing: .08em; text-transform: uppercase; }
.panel { padding: var(--space-4); background: var(--color-card-bg); border: 1px solid var(--color-border); border-radius: var(--radius-lg); box-shadow: var(--shadow-card); }
.spacer { flex: 1; }
.status-pill, .tag { padding: 3px 8px; border: 1px solid var(--color-border); border-radius: var(--radius-pill); font-size: var(--font-size-xs); }
.status-pill--completed, .status-pill--awaiting_confirmation { color: var(--color-success); border-color: var(--color-success); }
.status-pill--failed { color: var(--color-danger); border-color: var(--color-danger); }
.paper-tags { display: flex; flex-wrap: wrap; gap: var(--space-2); margin-top: var(--space-3); }
.input-note, .muted, .stage-copy small, .candidate-info small { color: var(--color-text-secondary); font-size: var(--font-size-xs); }
.input-note { display: block; margin-top: var(--space-3); }
.stage-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin-top: var(--space-4); background: var(--color-border); border: 1px solid var(--color-border); }
.stage { display: flex; align-items: center; gap: 9px; min-height: 68px; padding: 10px; background: var(--color-card-bg); }
.stage-index { display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: var(--color-bg-subtle); color: var(--color-text-secondary); font: 600 var(--font-size-xs)/1 var(--font-family-mono); }
.stage-copy { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 3px; }
.stage-copy strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--font-size-sm); }
.stage-dot { width: 7px; height: 7px; flex: none; border-radius: 50%; background: var(--color-border-strong); }
.stage--running .stage-dot { background: var(--color-brand); box-shadow: 0 0 0 4px var(--color-brand-soft); }
.stage--completed .stage-dot { background: var(--color-success); }
.stage--failed .stage-dot { background: var(--color-danger); }
.error-text { color: var(--color-danger) !important; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.results-layout { display: grid; grid-template-columns: minmax(300px, .72fr) minmax(0, 1.28fr); gap: var(--space-3); }
.candidate-list { display: flex; flex-direction: column; gap: 1px; margin-top: var(--space-4); border-top: 1px solid var(--color-border); }
.candidate-row { display: flex; align-items: center; gap: var(--space-3); width: 100%; padding: 12px 4px; border: 0; border-bottom: 1px solid var(--color-border); background: transparent; color: inherit; text-align: left; cursor: pointer; }
.candidate-row:hover, .candidate-row--active { background: var(--color-bg-subtle); }
.candidate-rank { width: 24px; color: var(--color-text-secondary); font: 600 var(--font-size-sm)/1 var(--font-family-mono); text-align: center; }
.candidate-info { display: flex; min-width: 0; flex: 1; flex-direction: column; gap: 4px; }
.candidate-info strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--font-size-sm); }
.candidate-score, .score-large { color: var(--color-brand); font-weight: 700; }
.score-large { font-size: var(--font-size-xl); }
.candidate-summary { margin: var(--space-4) 0; line-height: 1.75; white-space: pre-wrap; }
.detail-list { display: flex; flex-direction: column; gap: 10px; margin: 0; }
.detail-list div { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: var(--space-3); }
.detail-list dt { color: var(--color-text-secondary); font-size: var(--font-size-xs); }
.detail-list dd { margin: 0; font-size: var(--font-size-sm); line-height: 1.65; }
.score-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--space-2); margin-top: var(--space-4); }
.score-grid div { display: flex; justify-content: space-between; padding: 9px; background: var(--color-bg-subtle); font-size: var(--font-size-xs); }
.score-grid strong { font-family: var(--font-family-mono); }
.confirm-bar { margin-top: var(--space-5); padding-top: var(--space-3); border-top: 1px solid var(--color-border); }
.confirmed { color: var(--color-success); font-size: var(--font-size-sm); }
.empty { margin: 0; padding: var(--space-5) 0; color: var(--color-text-secondary); text-align: center; }
@media (max-width: 980px) { .stage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .results-layout { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .stage-grid { grid-template-columns: 1fr; } .score-grid { grid-template-columns: repeat(2, 1fr); } .workbench-head .btn { width: 100%; } }
</style>
