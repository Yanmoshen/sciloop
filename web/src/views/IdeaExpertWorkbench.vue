<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getAggregation } from '@/api/idea'
import { searchPapers, type PaperSearchItem } from '@/api/papers'
import {
  confirmIdeaExpertRun,
  createIdeaExpertRun,
  getIdeaExpertRun,
  listIdeaExpertRuns,
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
/** 20 个专家卡片里当前展开的是哪一位（点卡片切换，右侧/下方显示他的产出） */
const activeStageKey = ref<string>('')
/** POST /idea-runs 同步等待时还没有 run 对象，用本地时间保持等待状态可见。 */
const localStartedAt = ref<number | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null

const stages = computed(() => run.value?.stages ?? IDEA_EXPERTS.map(([key, label], index) => ({
  key,
  label,
  order: index + 1,
  status: 'pending',
} as IdeaExpertStage)))
function isStageCompleted(stage: IdeaExpertStage): boolean {
  return ['completed', 'done', 'success', 'ok'].includes(String(stage.status).toLowerCase())
}
function isStageRunning(stage: IdeaExpertStage): boolean {
  return ['running', 'active', 'in_progress'].includes(String(stage.status).toLowerCase())
}
function isStageWaiting(stage: IdeaExpertStage): boolean {
  return ['pending', 'queued', 'idle'].includes(String(stage.status).toLowerCase())
}
function isStageFailed(stage: IdeaExpertStage): boolean {
  return ['failed', 'error'].includes(String(stage.status).toLowerCase())
}

const completedStages = computed(() => stages.value.filter(isStageCompleted).length)
const currentStage = computed(() =>
  stages.value.find(isStageRunning) ??
  stages.value.find(isStageFailed) ??
  stages.value.find(isStageWaiting) ??
  null,
)
const recentStage = computed(() => {
  const finished = stages.value.filter(isStageCompleted)
  return finished[finished.length - 1] ?? null
})
const runStartIso = computed(() => {
  if (run.value?.created_at) return run.value.created_at
  return localStartedAt.value ? new Date(localStartedAt.value).toISOString() : null
})
const runStartText = computed(() => formatDateTime(runStartIso.value))
const resultGeneratedText = computed(() =>
  formatDateTime(run.value?.completed_at ?? run.value?.created_at ?? null),
)
const recentStatusText = computed(() => {
  if (starting.value && !run.value) return '已提交模型请求，等待第 1 个专家返回'
  if (run.value?.status === 'failed') return run.value.failure_reason || currentStage.value?.error || run.value.error || '运行失败，请查看错误信息'
  if (currentStage.value && isStageRunning(currentStage.value)) {
    return `${stageLabel(currentStage.value)}正在处理`
  }
  if (recentStage.value) return `${stageLabel(recentStage.value)}已完成，等待下一阶段`
  if (run.value) return '运行已创建，等待专家队列开始'
  return '尚未启动专家运行'
})
const activeCandidate = computed(() => run.value?.candidates.find((item) => String(item.id) === String(selectedCandidateId.value)) ?? run.value?.candidates[0] ?? null)

/**
 * 点开的那个专家：默认第一位。
 *
 * 2026-10-14：20 个专家卡片改成**可点开**，点谁看谁自己的产出（研究者口径：
 * 左边 20 个专家、点开看这一位说了什么，右边证据链要人话，不要 JSON）。
 */
const activeStage = computed(
  () => stages.value.find((stage) => String(stage.key) === activeStageKey.value) ?? stages.value[0] ?? null,
)

/**
 * 专家产出摊平成「标签 + 人话」两列。
 *
 * - 有 `findings[]` 的专家：每条 `claim` 本身就是模型写的人话结论，直接用；
 * - 没有 findings 的专家（如排序专家）：把标量字段平铺成「键：值」，数组只报条数 ——
 *   **任何情况下都不把 JSON 打到界面上**（旧版直接把对象渲染成 `{"score":88,…}`）。
 */
const expertFindings = computed<Array<{ label: string; text: string }>>(() => {
  const output = activeStage.value?.output
  if (!output || typeof output !== 'object') return []
  const value = output as Record<string, unknown>
  const rows: Array<{ label: string; text: string }> = []
  const findings = Array.isArray(value.findings) ? value.findings : []
  findings.forEach((item, index) => {
    if (!item || typeof item !== 'object') return
    const finding = item as Record<string, unknown>
    const claim = typeof finding.claim === 'string' ? finding.claim.trim() : ''
    if (!claim) return
    const tag = [finding.evidence_type, finding.idea_id]
      .filter((part): part is string => typeof part === 'string' && part.trim().length > 0)
      .join(' · ')
    rows.push({ label: tag || `结论 ${index + 1}`, text: claim })
  })
  if (!rows.length) {
    for (const [key, raw] of Object.entries(value)) {
      if (key === 'findings' || raw === null || raw === undefined) continue
      if (typeof raw === 'string' || typeof raw === 'number' || typeof raw === 'boolean') {
        rows.push({ label: key, text: String(raw) })
      } else if (Array.isArray(raw)) {
        rows.push({ label: key, text: `${raw.length} 条` })
      }
    }
  }
  return rows
})

/** 候选证据链：引用编号 + 论文 + 出处字段 + 原文摘录（已是人话，见 api 里的 flattenQuote） */
const candidateEvidence = computed<Array<{ ref: string; meta: string; quote: string }>>(() => {
  const list = activeCandidate.value?.evidence ?? []
  return list.map((item, index) => {
    const ref = String(item.ref_id ?? item.id ?? `E${index + 1}`)
    const meta = [item.paper_id ? `论文 ${item.paper_id}` : '', String(item.card_field ?? item.kind ?? '')]
      .filter(Boolean)
      .join(' · ')
    return { ref, meta, quote: String(item.quote_text ?? '').trim() }
  })
})

const isTerminal = computed(() => ['completed', 'awaiting_confirmation', 'failed', 'cancelled'].includes(String(run.value?.status)))
const canConfirm = computed(() => Boolean(run.value && activeCandidate.value && ['completed', 'awaiting_confirmation'].includes(String(run.value.status)) && run.value.confirmation_status !== 'confirmed'))

function stageLabel(stage: IdeaExpertStage): string {
  return stage.label || stage.name || String(stage.key)
}
function stageStatus(stage: IdeaExpertStage): string {
  if (isStageCompleted(stage)) return '已完成'
  if (isStageRunning(stage)) return '运行中'
  if (isStageFailed(stage)) return '失败'
  if (String(stage.status).toLowerCase() === 'skipped') return '跳过'
  return '等待'
}
function stageClass(stage: IdeaExpertStage): string {
  if (isStageCompleted(stage)) return 'stage--completed'
  if (isStageRunning(stage)) return 'stage--running'
  if (isStageFailed(stage)) return 'stage--failed'
  if (String(stage.status).toLowerCase() === 'skipped') return 'stage--skipped'
  return 'stage--pending'
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
function formatDateTime(value: string | number | null | undefined): string {
  if (!value) return '等待记录'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return date.toLocaleString('zh-CN', { hour12: false })
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
    if (runId) {
      run.value = await getIdeaExpertRun(String(runId))
    } else {
      // Task pages are opened from the list without a run_id. Restore the
      // newest persisted 20-expert result automatically.
      const runs = await listIdeaExpertRuns(aggregationId.value)
      const latestRun = runs[0]
      // The history endpoint intentionally returns a compact summary. Load
      // the selected run once more to render all 20 expert outputs and the
      // eight persisted candidates.
      run.value = latestRun?.id ? await getIdeaExpertRun(latestRun.id) : null
      if (run.value?.id) {
        await router.replace({ query: { ...route.query, run_id: String(run.value.id) } })
      }
    }
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
  localStartedAt.value = Date.now()
  notice.value = ''
  try {
    run.value = await createIdeaExpertRun({
      aggregation_id: aggregationId.value,
      paper_ids: paperIds.value,
      candidate_count_per_direction: 2,
    })
    localStartedAt.value = run.value.created_at ? Date.parse(run.value.created_at) || localStartedAt.value : localStartedAt.value
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

      <article v-if="starting || (run && !isTerminal)" class="panel run-monitor" aria-live="polite">
        <header class="panel-head">
          <div>
            <p class="panel-kicker">运行监视</p>
            <h2>{{ starting && !run ? '正在启动专家运行' : '专家运行中' }}</h2>
          </div>
          <span class="run-pulse" aria-hidden="true" />
          <span class="spacer" />
          <span class="muted">开始于 {{ runStartText }}</span>
        </header>
        <div class="run-monitor__grid">
          <div class="run-stat">
            <span>专家进度</span>
            <strong>{{ completedStages }}/20</strong>
            <small>已完成 / 总数</small>
          </div>
          <div class="run-stat">
            <span>当前阶段</span>
            <strong>{{ currentStage ? `${currentStage.order}. ${stageLabel(currentStage)}` : '等待队列' }}</strong>
            <small>{{ currentStage ? stageStatus(currentStage) : '模型请求已提交' }}</small>
          </div>
          <div class="run-stat run-stat--wide">
            <span>最近状态</span>
            <strong>{{ recentStatusText }}</strong>
            <small>页面会自动刷新专家状态</small>
          </div>
        </div>
      </article>

      <article v-if="run?.status === 'failed'" class="panel failure-panel" role="alert">
        <div>
          <p class="panel-kicker">运行未完成</p>
          <h2>{{ run.failure_reason || '专家链路未完成' }}</h2>
          <p class="failure-detail">已完成 {{ run.completed_experts ?? completedStages }}/{{ run.experts_total ?? 20 }} 个专家；失败 {{ run.failed_experts?.length ?? 0 }} 个。</p>
          <p v-if="run.error" class="failure-detail failure-detail--mono">{{ run.error }}</p>
        </div>
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
          <div
            v-for="stage in stages"
            :key="stage.key"
            class="stage"
            :class="[stageClass(stage), { 'stage--active': String(stage.key) === String(activeStage?.key) }]"
            role="button"
            tabindex="0"
            :aria-label="`查看「${stageLabel(stage)}」这位专家的产出`"
            @click="activeStageKey = String(stage.key)"
            @keyup.enter="activeStageKey = String(stage.key)"
          >
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

      <!-- 点开的这位专家说了什么（人话；不打印 JSON） -->
      <article v-if="activeStage" class="panel expert-output">
        <header class="panel-head">
          <div>
            <p class="panel-kicker">专家产出</p>
            <h2>{{ activeStage.order }}. {{ stageLabel(activeStage) }}</h2>
          </div>
          <span class="muted">{{ stageStatus(activeStage) }}</span>
          <span class="spacer" />
          <span v-if="activeStage.model_ref" class="muted">{{ activeStage.model_ref }}</span>
        </header>
        <ul v-if="expertFindings.length" class="finding-list">
          <li v-for="(row, index) in expertFindings" :key="`${activeStage.key}-${index}`">
            <span class="finding-tag">{{ row.label }}</span>
            <p class="finding-text">{{ row.text }}</p>
          </li>
        </ul>
        <p v-else class="empty">这位专家没有留下可读结论。</p>
      </article>

      <div class="results-layout">
        <article class="panel candidates-panel">
          <header class="panel-head">
            <div>
              <p class="panel-kicker">候选池</p>
              <h2>8 条候选 Idea</h2>
            </div>
            <span class="muted">四个方向 · 每个 2 条</span>
            <span v-if="run && isTerminal" class="result-time">生成完成于 {{ resultGeneratedText }}</span>
            <span v-else-if="run" class="result-time">开始于 {{ runStartText }}</span>
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
            <!-- 打分：分数是数字，理由走 tooltip（口径与分数分列，不塞 JSON） -->
            <div v-if="activeCandidate.scores" class="score-grid">
              <div
                v-for="(score, key) in activeCandidate.scores"
                :key="key"
                :title="activeCandidate.score_reasons?.[key] ?? ''"
              >
                <span>{{ key }}</span>
                <strong>{{ score ?? '—' }}</strong>
                <small v-if="activeCandidate.score_refs?.[key]?.length">{{ (activeCandidate.score_refs?.[key] ?? []).join(' ') }}</small>
              </div>
            </div>
            <!-- 证据链：编号 + 论文/字段 + 原文摘录（人话） -->
            <section v-if="candidateEvidence.length" class="evidence-chain">
              <h3>证据链</h3>
              <ul>
                <li v-for="item in candidateEvidence" :key="item.ref">
                  <span class="evidence-ref">{{ item.ref }}</span>
                  <div>
                    <p v-if="item.meta" class="evidence-meta">{{ item.meta }}</p>
                    <p class="evidence-quote">{{ item.quote || '（这条证据没有摘录）' }}</p>
                  </div>
                </li>
              </ul>
            </section>
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
.run-monitor { border-color: var(--color-border-strong); background: var(--color-bg-subtle); }
.failure-panel { border-color: var(--color-danger); background: color-mix(in srgb, var(--color-danger) 6%, var(--color-card-bg)); }
.failure-detail { margin: 8px 0 0; color: var(--color-text-secondary); font-size: var(--font-size-sm); }
.failure-detail--mono { font-family: var(--font-family-mono); font-size: var(--font-size-xs); overflow-wrap: anywhere; }
.run-monitor__grid { display: grid; grid-template-columns: minmax(150px, .7fr) minmax(200px, 1fr) minmax(260px, 1.5fr); gap: var(--space-2); margin-top: var(--space-4); }
.run-stat { display: flex; min-height: 84px; flex-direction: column; justify-content: center; gap: 4px; padding: 12px; border: 1px solid var(--color-border); background: var(--color-card-bg); }
.run-stat span, .run-stat small { color: var(--color-text-secondary); font-size: var(--font-size-xs); }
.run-stat strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--font-size-md); }
.run-stat--wide strong { white-space: normal; }
.run-pulse { width: 8px; height: 8px; flex: none; border-radius: 50%; background: var(--color-brand); box-shadow: 0 0 0 5px var(--color-brand-soft); animation: run-pulse 1.2s ease-in-out infinite; }
.result-time { margin-left: auto; color: var(--color-text-secondary); font-size: var(--font-size-xs); }
@keyframes run-pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: .45; transform: scale(.8); } }
.stage-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1px; margin-top: var(--space-4); background: var(--color-border); border: 1px solid var(--color-border); }
.stage { display: flex; align-items: center; gap: 9px; min-height: 68px; padding: 10px; background: var(--color-card-bg); cursor: pointer; }
.stage:hover { background: var(--color-bg-subtle); }
.stage:focus-visible { outline: 2px solid var(--color-brand); outline-offset: -2px; }
.stage--active { background: var(--color-brand-soft); box-shadow: inset 0 0 0 1px var(--color-brand); }
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
.score-grid small { color: var(--color-text-secondary); font-family: var(--font-family-mono); }
/* 专家产出：标签 + 人话结论（点开某位专家后显示） */
.expert-output .finding-list { display: flex; flex-direction: column; gap: var(--space-3); margin: var(--space-3) 0 0; padding: 0; list-style: none; }
.expert-output .finding-list li { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: var(--space-3); align-items: start; }
.finding-tag { padding: 2px 8px; border-radius: var(--radius-sm); background: var(--color-bg-subtle); color: var(--color-text-secondary); font: 500 var(--font-size-xs)/1.6 var(--font-family-mono); text-align: center; overflow-wrap: anywhere; }
.finding-text { margin: 0; font-size: var(--font-size-sm); line-height: 1.7; }
/* 证据链：编号 + 论文/字段 + 原文摘录 */
.evidence-chain { margin-top: var(--space-5); }
.evidence-chain h3 { margin: 0 0 var(--space-2); font-size: var(--font-size-sm); font-weight: 600; }
.evidence-chain ul { display: flex; flex-direction: column; gap: var(--space-3); margin: 0; padding: 0; list-style: none; }
.evidence-chain li { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: var(--space-3); align-items: start; }
.evidence-ref { padding: 2px 6px; border-radius: var(--radius-sm); background: var(--color-brand-soft); font: 600 var(--font-size-xs)/1.6 var(--font-family-mono); text-align: center; }
.evidence-meta { margin: 0 0 2px; color: var(--color-text-secondary); font-size: var(--font-size-xs); }
.evidence-quote { margin: 0; font-size: var(--font-size-sm); line-height: 1.7; }
.confirm-bar { margin-top: var(--space-5); padding-top: var(--space-3); border-top: 1px solid var(--color-border); }
.confirmed { color: var(--color-success); font-size: var(--font-size-sm); }
.empty { margin: 0; padding: var(--space-5) 0; color: var(--color-text-secondary); text-align: center; }
@media (max-width: 980px) { .stage-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .run-monitor__grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .run-stat--wide { grid-column: 1 / -1; } .results-layout { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .stage-grid { grid-template-columns: 1fr; } .score-grid { grid-template-columns: repeat(2, 1fr); } .workbench-head .btn { width: 100%; } }
</style>
