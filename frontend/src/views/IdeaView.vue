<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究构思页（WP08-T7 / 附录 C.1：idea 列表 + 生成器 + 证据抽屉 + 手动输入入口）。
 *
 * 覆盖入口：路由 `/ideas`（WP01 已注册）。整页覆盖 WP01 占位实现。
 *
 * 硬红线在 UI 上的落地：
 * - 无 Evidence 的 idea 不能进入可行性（按钮禁用 + 红标，服务端同样拦截）；
 * - 生成结果必须如实标注 `generation_mode`：`template` 显示「模板合成（未调用 LLM）」，
 *   `llm` 显示真实 provider/model_ref/cost（回放则标 is_replay）；
 * - 被丢弃的条目（无证据）与非法证据引用号原样列出，不做静默过滤。
 *
 * 说明：`EvidenceDrawer.vue` 不在 WP08 owned_paths 内，故抽屉内联实现；
 * 证据解析统一走 WP13 `GET /evidence/{type}/{id}`。
 */
import { computed, onMounted, ref } from 'vue'

import { getEvidenceDetail, type EvidenceDetail } from '@/api/claims'
import {
  evidenceTypeLabel,
  formatScore,
  MODE_LABELS,
  MECHANISM_LABELS,
  RISK_LEVEL_LABELS,
  type EvidenceCandidate,
  type Idea,
  type IdeaMechanism,
  type IdeaMode,
} from '@/api/idea'
import IdeaCard from '@/components/IdeaCard.vue'
import ScoreRadar from '@/components/ScoreRadar.vue'
import TaskbookForm from '@/components/TaskbookForm.vue'
import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { useIdeaStore } from '@/stores/idea'
import { useSessionStore } from '@/stores/session'

const store = useIdeaStore()
const session = useSessionStore()

/** 本页全部产出动作（生成 / 手动录入 / 绑证据 / 可行性 / 任务书）都是写操作，仅 Owner 面可用 */
const canWrite = computed(() => session.isOwner)
const writeDeniedNote = computed(() =>
  canWrite.value
    ? null
    : '生成 idea、绑定证据、可行性计算与任务书均为写操作：public_demo 只读面会被后端拒绝（403 owner_token_required）。' +
      '可在设置页填入服务端 OWNER_TOKEN 后重试。',
)

const tab = ref<'evidence' | 'feasibility' | 'taskbook'>('evidence')
const aggregationId = ref<number | null>(null)
const mode = ref<IdeaMode>('template')
const count = ref(5)
/**
 * model_ref 默认留空：不预填任何 stub 标识（预填会让「llm 模式」看起来已配置好，
 * 实际可能拿一个并不存在的 stub 模型去调用）。占位文案只作示例提示，不构成取值。
 */
const modelRef = ref('')
const useLlmForFeasibility = ref(false)

const manualTitle = ref('')
const manualContent = ref('')
const manualMechanism = ref<IdeaMechanism>('combination')

const bindPaperId = ref<number | null>(null)
const bindSpanId = ref<number | null>(null)
const bindCardField = ref('limitations')
const bindNotice = ref<string | null>(null)
/** 生成动作反馈：前置条件不满足或后端未返回结果时给出可见原因 */
const generateNotice = ref<string | null>(null)
/** 手动录入反馈：缺字段 / 只读面 / 后端未返回结果时给出可见原因 */
const manualNotice = ref<string | null>(null)

const drawerOpen = ref(false)
const drawerTitle = ref('')
const drawerItems = ref<EvidenceDetail[]>([])
const detailLoading = ref<number | null>(null)
const details = ref<Record<number, EvidenceDetail>>({})
const detailErrors = ref<Record<number, string>>({})

const CARD_FIELDS = [
  'research_problem',
  'core_method',
  'key_innovation',
  'technical_route',
  'experimental_setup',
  'main_conclusions',
  'limitations',
  'transferable',
]

const ideas = computed(() => store.ideas)
const selected = computed(() => store.selectedIdea)
const generation = computed(() => store.generation)
const feasibility = computed(() => store.feasibility)

const busy = computed(() =>
  ['loadAggregations', 'loadAggregation', 'loadIdeas', 'generateIdeas', 'createManualIdea', 'bindEvidence',
    'createFeasibility', 'createTaskbook', 'patchTaskbook', 'lockTaskbook'].includes(store.busy ?? ''),
)

/** 降级：模板合成（未调用 LLM）/ LLM 失败 / 无证据 idea 被丢弃 / 仅摘要级证据 */

/** 可重试动作：重拉聚合与 idea 列表（失败不清空已展示内容） */
async function retryIdeas(): Promise<void> {
  await store.loadAggregations(session.currentProjectId)
  await store.loadIdeas({
    projectId: session.currentProjectId,
    aggregationId: aggregationId.value,
  })
}

/**
 * 权限态：只读面下写入口已**前置禁用**；若本次请求确实收到 403，再明确说明「已被拒绝」。
 * 依据 store 记录的真实 HTTP 状态码判断，不靠对错误文案做字符串匹配。
 */
const permissionDenied = computed(() => !canWrite.value || store.errorStatus === 403)
const permissionTitle = computed(() =>
  store.errorStatus === 403
    ? '权限受限：该写操作已被服务端拒绝（403 owner_token_required）'
    : '只读演示面：生成 / 绑定 / 可行性 / 任务书入口已前置禁用（非请求被拒）',
)
const permissionNote = computed(() =>
  store.errorStatus === 403
    ? `${store.activeErrorInfo?.message ?? ''} ${writeDeniedNote.value ?? ''}`.trim()
    : writeDeniedNote.value,
)

onMounted(async () => {
  await store.loadAggregations(session.currentProjectId)
  const latest = store.aggregations[0]
  if (latest) {
    aggregationId.value = latest.id
    await store.loadAggregation(latest.id)
    await store.loadIdeas({ projectId: session.currentProjectId, aggregationId: latest.id })
  } else {
    await store.loadIdeas({ projectId: session.currentProjectId })
  }
})

async function onPickAggregation(): Promise<void> {
  if (!aggregationId.value) return
  await store.loadAggregation(aggregationId.value)
  await store.loadIdeas({ projectId: session.currentProjectId, aggregationId: aggregationId.value })
}

async function onGenerate(): Promise<void> {
  if (!canWrite.value) {
    generateNotice.value = writeDeniedNote.value
    return
  }
  if (!aggregationId.value) {
    generateNotice.value = '请先在上方选择聚合（聚合结果决定可用作素材的空白清单）。'
    return
  }
  if (mode.value !== 'template' && !modelRef.value.trim()) {
    generateNotice.value = `模式 ${mode.value} 必须显式提供真实的 model_ref（provider:model_id）：留空不会回退到模板，请先填写或在「设置页」确认已配置供应商。`
    return
  }
  generateNotice.value = null
  const result = await store.generate({
    aggregationId: aggregationId.value,
    count: count.value,
    projectId: session.currentProjectId,
    mode: mode.value,
    modelRef: mode.value === 'template' ? null : modelRef.value.trim(),
  })
  if (result) {
    const first = result.items[0]
    if (first) await openEvidenceForIdea(first)
  } else {
    // `llm_error` 是结构化对象（后端原样透传），这里序列化后展示，不改写成"成功"
    const llmError = generation.value?.llm_error
    generateNotice.value =
      store.ideasError ??
      (llmError ? JSON.stringify(llmError) : null) ??
      '生成未返回结果：请查看下方「失败原因」区块。'
  }
}

async function onCreateManual(): Promise<void> {
  if (!canWrite.value) {
    manualNotice.value = writeDeniedNote.value
    return
  }
  if (!manualTitle.value.trim() || !manualContent.value.trim()) {
    // 缺字段时给出可见原因，避免「点了没反应」
    manualNotice.value = '手动录入需要同时填写「标题」与「内容」；两者均不允许为空。'
    return
  }
  manualNotice.value = null
  const idea = await store.addManualIdea({
    title: manualTitle.value.trim(),
    content: manualContent.value.trim(),
    projectId: session.currentProjectId,
    aggregationId: aggregationId.value,
    mechanism: manualMechanism.value,
  })
  if (idea) {
    manualTitle.value = ''
    manualContent.value = ''
    await openEvidenceForIdea(idea)
  } else {
    manualNotice.value = store.ideasError ?? '手动创建未返回结果，请查看上方错误信息。'
  }
}

async function onBind(idea: Idea): Promise<void> {
  bindNotice.value = null
  if (!canWrite.value) {
    bindNotice.value = 'public_demo 只读面：绑定证据属写操作，需先在设置页填入 OWNER_TOKEN'
    return
  }
  const candidates: EvidenceCandidate[] = []
  if (bindSpanId.value) {
    candidates.push({
      evidence_type: 'paper_span',
      paper_span_id: Number(bindSpanId.value),
      paper_id: bindPaperId.value ? Number(bindPaperId.value) : null,
    })
  } else if (bindPaperId.value) {
    candidates.push({
      evidence_type: 'card_field',
      paper_id: Number(bindPaperId.value),
      card_field: bindCardField.value,
    })
  }
  if (candidates.length === 0) {
    bindNotice.value = '请至少填写 paper_id（卡片字段证据）或 paper_span_id（原文段落证据）'
    return
  }
  const result = await store.bindEvidence(idea.id, candidates)
  if (result) {
    bindNotice.value = `绑定 ${result.boundCount} 条；被拒 ${result.rejected.length} 条${
      result.rejected.length ? '：' + result.rejected.map((item) => item.code).join(', ') : ''
    }`
    const fresh = store.ideas.find((item) => item.id === idea.id)
    if (fresh) await openEvidenceForIdea(fresh)
  }
}

async function onFeasibility(idea: Idea): Promise<void> {
  if (!canWrite.value) {
    bindNotice.value = 'public_demo 只读面：可行性计算属写操作，需先在设置页填入 OWNER_TOKEN'
    return
  }
  if (!idea.has_evidence) {
    bindNotice.value = '该 idea 无证据：按契约不得进入可行性，请先绑定证据'
    return
  }
  const payload = await store.runFeasibility({
    ideaId: idea.id,
    projectId: session.currentProjectId,
    sampleSize: 20,
    useLlm: useLlmForFeasibility.value,
    modelRef: useLlmForFeasibility.value ? modelRef.value || null : null,
  })
  if (payload) tab.value = 'feasibility'
}

async function onTaskbookCreate(payload: Record<string, unknown>): Promise<void> {
  if (!canWrite.value) {
    bindNotice.value = 'public_demo 只读面：创建任务书属写操作，需先在设置页填入 OWNER_TOKEN'
    return
  }
  await store.submitTaskbook({
    projectId: Number(payload.project_id),
    ideaId: Number(payload.idea_id),
    researchQuestion: String(payload.research_question),
    targetDatasets: (payload.target_datasets as string[]) ?? [],
    baselines: (payload.baselines as string[]) ?? [],
    metrics: (payload.metrics as string[]) ?? [],
    deliverables: (payload.deliverables as string[]) ?? [],
    rounds: (payload.rounds as Record<string, number>) ?? {},
    computeBudget: (payload.compute_budget as Record<string, number>) ?? {},
  })
}

async function openEvidenceForIdea(idea: Idea): Promise<void> {
  drawerTitle.value = `idea #${idea.id} 的证据`
  drawerItems.value = idea.evidences ?? []
  drawerOpen.value = true
  tab.value = 'evidence'
}

async function openDimensionEvidence(label: string, items: EvidenceDetail[]): Promise<void> {
  drawerTitle.value = `维度「${label}」的证据`
  drawerItems.value = items ?? []
  drawerOpen.value = true
}

async function loadDetail(ev: EvidenceDetail): Promise<void> {
  const id = Number(ev.evidence_id)
  if (details.value[id] || detailLoading.value === id) return
  detailLoading.value = id
  try {
    const detail = await getEvidenceDetail(ev.evidence_type, id, { idKind: 'evidence' })
    details.value = { ...details.value, [id]: detail }
  } catch (error) {
    detailErrors.value = { ...detailErrors.value, [id]: error instanceof Error ? error.message : String(error) }
  } finally {
    detailLoading.value = null
  }
}

const modeHint = computed(() => {
  const current = generation.value
  if (!current) return null
  const label = MODE_LABELS[String(current.generation_mode)] ?? String(current.generation_mode)
  const llm = current.llm as Record<string, unknown> | null
  return {
    label,
    provider: llm ? String(llm.provider ?? '') : '',
    modelRef: llm ? String(llm.model_ref ?? '') : '',
    isReplay: llm ? Boolean(llm.is_replay) : false,
    cost: llm ? (llm.cost_usd as number | null) : null,
    error: current.llm_error,
  }
})
</script>

<template>
  <section class="idea-view">

    <header class="idea-view__header">
      <div>
        <h1>研究构思</h1>
      </div>
    </header>

    <!-- 六类状态：loading / error / permission denied / retry（empty 见各面板） -->
    <ViewStatePanel
      :loading="busy"
      loading-text="正在与构思域接口交互（聚合 / idea / 可行性 / 任务书）…"
      :error="store.ideasError ?? store.feasibilityError ?? store.taskbookError"
      :error-code="store.errorCode"
      error-title="构思域请求失败"
      :permission-denied="permissionDenied"
      :permission-note="permissionNote"
      :permission-title="permissionTitle"
      retryable
      retry-label="重试加载构思域"
      :busy="busy"
      @retry="retryIdeas"
    />

    <div class="idea-view__layout">
      <!-- 左栏：生成器 + 列表 -->
      <section class="idea-view__col">
        <section class="panel">
          <h3>数据源</h3>
          <div class="row">
            <label class="field">
              <span>聚合</span>
              <select v-model.number="aggregationId" @change="onPickAggregation">
                <option :value="null">选择聚合…</option>
                <option v-for="item in store.aggregations" :key="item.id" :value="item.id">
                  #{{ item.id }} · {{ item.paper_count }} 篇 · 空白 {{ item.gap_count ?? '-' }}
                </option>
              </select>
            </label>
            <span class="meta">空白 {{ store.gaps.length }} 条可用作素材</span>
          </div>
        </section>

        <section class="panel">
          <h3>生成 idea</h3>
          <div class="row">
            <label class="field">
              <span>模式</span>
              <select v-model="mode">
                <option value="template">template（规则合成，不调用 LLM）</option>
                <option value="llm">llm（必须真调模型，不可用即失败）</option>
                <option value="auto">auto（优先 LLM，失败降级并披露）</option>
              </select>
            </label>
            <label class="field field--narrow">
              <span>条数</span>
              <input v-model.number="count" type="number" min="1" max="20" />
            </label>
          </div>
          <div v-if="mode !== 'template'" class="row">
            <label class="field">
              <span>model_ref（provider:model_id，须为真实配置；留空将被拒绝）</span>
              <input v-model="modelRef" placeholder="例如 deepseek:deepseek-chat（在设置页配置的供应商:模型）" />
            </label>
          </div>
          <div class="row">
            <button type="button" class="btn btn--primary" :disabled="!canWrite || !aggregationId || store.busy === 'generateIdeas'" @click="onGenerate">
              {{ store.busy === 'generateIdeas' ? '生成中…' : '生成 idea' }}
            </button>
            <span class="meta">服务端硬校验：无 Evidence 的 idea 直接丢弃并记 warning</span>
          </div>
          <p v-if="generateNotice" class="warn">{{ generateNotice }}</p>

          <div v-if="modeHint" class="gen">
            <p class="gen__line">
              本次生成方式：<strong>{{ modeHint.label }}</strong>
              <template v-if="modeHint.provider"> · provider {{ modeHint.provider }}</template>
              <template v-if="modeHint.modelRef"> · {{ modeHint.modelRef }}</template>
              <template v-if="modeHint.isReplay"> · <span class="gen__replay">is_replay=true（回放）</span></template>
              <template v-if="modeHint.cost !== null && modeHint.cost !== undefined"> · cost {{ modeHint.cost }} USD</template>
            </p>
            <p v-if="generation" class="gen__line">
              请求 {{ generation.requested_count }} · 输出 {{ generation.generated_count }} ·
              新建 {{ generation.created_count }} · 丢弃 {{ generation.discarded_count }}
            </p>
            <ul v-if="generation?.discarded?.length" class="gen__discarded">
              <li v-for="item in generation.discarded" :key="item.idea_id">
                丢弃 idea #{{ item.idea_id }}：{{ item.message }}
              </li>
            </ul>
            <ul v-if="generation?.evidence_audit?.some((a) => (a.invalid_refs as unknown[])?.length)" class="gen__discarded">
              <li v-for="item in generation.evidence_audit" :key="String(item.idea_id)">
                <template v-if="(item.invalid_refs as unknown[])?.length">
                  idea #{{ item.idea_id }} 存在非法证据引用号（不在引用表内，已丢弃）：
                  {{ JSON.stringify(item.invalid_refs) }}
                </template>
              </li>
            </ul>
            <p v-if="modeHint.error" class="gen__error">
              LLM 调用失败（已按模式处理，未冒充模型结果）：{{ JSON.stringify(modeHint.error) }}
            </p>
          </div>
        </section>

        <section class="panel">
          <h3>手动录入 idea</h3>
          <label class="field">
            <span>标题</span>
            <input v-model="manualTitle" placeholder="例如：用迁移机制解决某空白" />
          </label>
          <label class="field">
            <span>内容</span>
            <textarea v-model="manualContent" rows="3" placeholder="研究问题、做法、验证方式" />
          </label>
          <div class="row">
            <label class="field field--narrow">
              <span>机制</span>
              <select v-model="manualMechanism">
                <option v-for="(label, key) in MECHANISM_LABELS" :key="key" :value="key">{{ label }}（{{ key }}）</option>
              </select>
            </label>
            <button type="button" class="btn" :disabled="!canWrite || store.busy === 'createManualIdea'" @click="onCreateManual">
              手动创建（可后绑证据）
            </button>
          </div>
          <p class="meta">手动 idea 与 AI idea 走同一套证据契约；无证据时不能进入可行性。</p>
          <p v-if="manualNotice" class="warn">{{ manualNotice }}</p>
        </section>

        <section class="panel">
          <h3>idea 列表（{{ ideas.length }}）</h3>
          <el-skeleton v-if="store.busy === 'loadIdeas'" :rows="4" animated />
          <el-empty v-else-if="ideas.length === 0" description="暂无 idea：先选择聚合再生成，或手动录入（未获取 ≠ 0 条）" />
          <IdeaCard
            v-for="item in ideas"
            :key="item.id"
            :idea="item"
            :active="item.id === store.selectedIdeaId"
            :feasibility-ready="feasibility?.idea_id === item.id"
            @select="store.select(item.id)"
            @evidence="(payload) => openEvidenceForIdea(payload.idea)"
            @bind="(payload) => { bindPaperId = payload.idea.evidence_ids[0] ?? bindPaperId; tab = 'evidence' }"
            @feasibility="(payload) => onFeasibility(payload.idea)"
            @select-idea="store.select(item.id)"
          />
        </section>
      </section>

      <!-- 右栏：证据 / 可行性 / 任务书 -->
      <section class="idea-view__col">
        <nav class="tabs">
          <button type="button" :class="['tab', { 'tab--on': tab === 'evidence' }]" @click="tab = 'evidence'">证据</button>
          <button type="button" :class="['tab', { 'tab--on': tab === 'feasibility' }]" @click="tab = 'feasibility'">可行性</button>
          <button type="button" :class="['tab', { 'tab--on': tab === 'taskbook' }]" @click="tab = 'taskbook'">任务书</button>
        </nav>

        <section v-show="tab === 'evidence'" class="panel">
          <h3>证据绑定（走 WP13 哈希优先校验）</h3>
          <p v-if="!selected" class="meta">请先在左侧选中一条 idea。</p>
          <template v-else>
            <p class="meta">
              当前：idea #{{ selected.id }} · {{ selected.title }} · 已绑定 {{ selected.evidence_count }} 条
              （范围 {{ selected.gate?.evidence_scope || '未记录' }}）
            </p>
            <div class="row">
              <label class="field field--narrow">
                <span>paper_id</span>
                <input v-model.number="bindPaperId" type="number" placeholder="如 126" />
              </label>
              <label class="field field--narrow">
                <span>paper_span_id（填了就走原文段落证据）</span>
                <input v-model.number="bindSpanId" type="number" placeholder="如 2284" />
              </label>
              <label class="field field--narrow">
                <span>card_field（未填 span 时用）</span>
                <select v-model="bindCardField">
                  <option v-for="field in CARD_FIELDS" :key="field" :value="field">{{ field }}</option>
                </select>
              </label>
              <button type="button" class="btn" :disabled="!canWrite || store.busy === 'bindEvidence'" @click="onBind(selected)">
                绑定证据
              </button>
            </div>
            <p v-if="bindNotice" class="meta">{{ bindNotice }}</p>
            <ul class="ev-list">
              <li v-for="ev in selected.evidences" :key="ev.evidence_id">
                <div class="ev-row">
                  <span class="ev-kind">{{ evidenceTypeLabel(ev.evidence_type) }}</span>
                  <span class="meta">
                    paper {{ ev.paper_id ?? '—' }} · span {{ ev.span?.paper_span_id ?? '—' }} ·
                    verdict {{ ev.verification?.verdict ?? '未提供' }}
                  </span>
                  <a v-if="ev.jump_url" class="link" :href="ev.jump_url">打开解析页</a>
                  <button type="button" class="btn btn--tiny" :disabled="detailLoading === ev.evidence_id" @click="loadDetail(ev)">
                    解析
                  </button>
                </div>
                <blockquote v-if="ev.quote_text" class="quote">{{ ev.quote_text }}</blockquote>
                <p class="meta">
                  覆盖范围 {{ ev.gate?.evidence_scope ?? '未提供' }} · 覆盖率 {{ ev.gate?.coverage ?? '未提供' }}
                </p>
                <p v-if="details[ev.evidence_id]" class="meta">
                  gate.ok={{ details[ev.evidence_id].gate?.ok ?? '未提供' }} ·
                  {{ details[ev.evidence_id].gate?.coverage_note || details[ev.evidence_id].gate?.reason || '' }}
                </p>
                <p v-if="detailErrors[ev.evidence_id]" class="err">{{ detailErrors[ev.evidence_id] }}</p>
              </li>
            </ul>
            <p v-if="selected.evidences.length === 0" class="warn">
              该 idea 尚无证据：不得输出，也不得进入可行性。
            </p>
          </template>
        </section>

        <section v-show="tab === 'feasibility'" class="panel">
          <h3>可行性四维卡</h3>
          <p v-if="!selected" class="meta">请先在左侧选中一条 idea。</p>
          <template v-else>
            <div class="row">
              <label class="check">
                <input v-model="useLlmForFeasibility" type="checkbox" />
                额外调用 LLM 复核（只写建议分，不参与总分）
              </label>
              <button type="button" class="btn btn--primary" :disabled="!canWrite || !selected.has_evidence || store.busy === 'createFeasibility'" @click="onFeasibility(selected)">
                {{ store.busy === 'createFeasibility' ? '计算中…' : '生成可行性报告' }}
              </button>
            </div>
            <p v-if="!canWrite" class="warn">{{ writeDeniedNote }}</p>

            <template v-if="feasibility">
              <ScoreRadar
                :feasibility="feasibility"
                @evidence="(payload) => openDimensionEvidence(payload.dimension.label, payload.dimension.evidence)"
              />

              <section class="block">
                <h4>风险清单（{{ feasibility.risk_list.length }}）</h4>
                <el-empty v-if="feasibility.risk_list.length === 0" description="未触发任何风险规则" />
                <ul v-else class="risk">
                  <li v-for="risk in feasibility.risk_list" :key="risk.key" :class="['risk__item', `risk__item--${risk.level}`]">
                    <div class="risk__head">
                      <span class="risk__level">风险等级：{{ RISK_LEVEL_LABELS[String(risk.level)] || risk.level }}</span>
                      <span class="meta">{{ risk.level_basis }}</span>
                    </div>
                    <p class="risk__text">{{ risk.risk }}</p>
                    <p class="risk__mitigation">处置建议：{{ risk.mitigation }}</p>
                  </li>
                </ul>
                <p v-if="feasibility.risk_summary?.policy_note" class="meta">{{ feasibility.risk_summary.policy_note }}</p>
              </section>

              <section class="block">
                <h4>最小可行实验（MVE）</h4>
                <p class="mve__obj">{{ feasibility.mve_plan.objective }}</p>
                <p class="meta">
                  模板建议 <strong>{{ feasibility.mve_plan.template_id }}</strong>
                  （白名单 {{ feasibility.mve_plan.template_whitelist.join(' / ') }}）·
                  {{ feasibility.mve_plan.template_rationale }}
                </p>
                <p class="meta">
                  数据集 {{ feasibility.mve_plan.dataset.name || '材料未提供，需研究者指定' }}
                  （confirmed={{ feasibility.mve_plan.dataset.confirmed }}）·
                  sample_size {{ feasibility.mve_plan.sample_size }}（上限 {{ feasibility.mve_plan.sample_size_limit }}）·
                  预期耗时 {{ feasibility.mve_plan.expected_duration_minutes }} 分钟 ·
                  预估成本 {{ feasibility.mve_plan.expected_cost_usd ?? 'null（不编造）' }}
                </p>
                <p v-if="feasibility.mve_plan.dataset.note" class="warn">{{ feasibility.mve_plan.dataset.note }}</p>
                <ol class="mve">
                  <li v-for="step in feasibility.mve_plan.steps" :key="step.step">
                    <div class="mve__action">{{ step.step }}. {{ step.action }}</div>
                    <div class="meta">{{ step.detail }}</div>
                    <div class="meta">{{ step.endpoint || '（人工步骤）' }} → {{ step.expected_output }}</div>
                    <div class="meta">{{ step.duration_minutes }} 分钟 · {{ step.duration_source }}</div>
                  </li>
                </ol>
                <h5>人工核对清单</h5>
                <ul class="mve__check">
                  <li v-for="item in feasibility.mve_plan.human_review_checklist" :key="item.item">
                    <strong>{{ item.blocking ? '[必须]' : '[建议]' }}</strong> {{ item.item }} —— {{ item.why }}
                  </li>
                </ul>
                <h5>护栏说明</h5>
                <ul class="mve__check">
                  <li v-for="note in feasibility.mve_plan.guardrail_notes" :key="note">{{ note }}</li>
                </ul>
                <p class="meta">{{ feasibility.mve_plan.cost_note }}</p>
                <p class="meta">{{ feasibility.mve_plan.duration_note }}</p>
              </section>
            </template>
            <el-empty v-else description="尚未生成可行性报告" />
          </template>
        </section>

        <section v-show="tab === 'taskbook'" class="panel">
          <TaskbookForm
            :taskbook="store.taskbook"
            :feasibility="feasibility"
            :idea-id="selected?.id ?? null"
            :project-id="session.currentProjectId"
            :can-write="canWrite"
            :server-error="store.taskbookError"
            @create="onTaskbookCreate"
            @save="(payload) => store.saveTaskbook(payload.id, payload.patch)"
            @lock="(payload) => store.lockTaskbookById(payload.id)"
          />
        </section>
      </section>
    </div>

    <!-- 证据抽屉（内联，非 owned_paths 组件） -->
    <aside v-if="drawerOpen" class="drawer scroll-y">
      <header class="drawer__head">
        <h3>{{ drawerTitle }}</h3>
        <button type="button" class="btn btn--tiny" @click="drawerOpen = false">关闭</button>
      </header>
      <p class="meta">
        证据解析统一走 WP13 `GET /evidence/{type}/{id}`；不展示未落库的证据，也不补默认值。
      </p>
      <el-empty v-if="drawerItems.length === 0" description="该 idea 尚未绑定证据（不允许输出）" />
      <ul v-else class="ev-list">
        <li v-for="ev in drawerItems" :key="ev.evidence_id">
          <div class="ev-row">
            <span class="ev-kind">{{ evidenceTypeLabel(ev.evidence_type) }}</span>
            <span class="meta">ev#{{ ev.evidence_id }} · verdict {{ ev.verification?.verdict ?? '未提供' }}</span>
            <a v-if="ev.jump_url" class="link" :href="ev.jump_url">跳转到论文解析页</a>
          </div>
          <blockquote v-if="ev.quote_text" class="quote">{{ ev.quote_text }}</blockquote>
          <p class="meta">
            论文 {{ ev.paper?.title || ev.paper_id || '未提供' }} ·
            章节 {{ ev.span?.section_name || '未标注' }} · 页码 {{ ev.span?.page_number ?? '未提供' }}
          </p>
          <p class="meta">
            覆盖范围 {{ ev.gate?.evidence_scope ?? '未提供' }} · fulltext_gate
            {{ ev.gate?.ok === true ? '通过' : '未通过 / 未提供' }} · 覆盖率 {{ ev.gate?.coverage ?? '未提供' }}
          </p>
          <p class="meta">document_version：{{ ev.document_version || '未携带' }}</p>
        </li>
      </ul>
      <p v-if="feasibility" class="meta">当前可行性总分：{{ formatScore(feasibility.total_score) }}</p>
    </aside>
  </section>
</template>

<style scoped>
.idea-view {
  padding: var(--space-4);
}
.idea-view__header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--space-3);
  align-items: flex-start;
}
.idea-view__layout {
  display: grid;
  grid-template-columns: minmax(360px, 1fr) minmax(420px, 1.1fr);
  gap: var(--space-4);
  align-items: start;
}
@media (max-width: 1200px) {
  .idea-view__layout {
    grid-template-columns: 1fr;
  }
}
.idea-view__col {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
}
.panel {
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}
.panel h3 {
  margin: 0 0 var(--space-2);
  font-size: var(--font-size-md);
}
.panel h4 {
  margin: var(--space-3) 0 var(--space-2);
  font-size: var(--font-size-sm);
}
.panel h5 {
  margin: var(--space-2) 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: flex-end;
  margin-bottom: var(--space-2);
}
.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  flex: 1 1 220px;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.field--narrow {
  flex: 0 1 160px;
}
.field input,
.field select,
.field textarea {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font-family: inherit;
  font-size: var(--font-size-sm);
}
.check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.btn {
  padding: var(--space-1) var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  cursor: pointer;
  transition:
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    box-shadow 200ms cubic-bezier(0.4, 0, 0.2, 1),
    transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}
/* 悬停/按压反馈：只改颜色与 1px 位移，不动布局 */
.btn:hover:not(:disabled) {
  border-color: var(--color-brand);
  color: var(--color-brand);
  background: var(--color-brand-soft);
  transform: translateY(-1px);
  box-shadow: var(--shadow-card);
}
.btn:active:not(:disabled) {
  transform: translateY(0);
  box-shadow: none;
}
.btn--primary {
  border-color: var(--color-brand);
  background: var(--color-brand);
  color: var(--color-text-inverse);
}
.btn--primary:hover:not(:disabled) {
  color: var(--color-text-inverse);
  filter: brightness(1.07);
}
.btn--tiny {
  padding: 0 var(--space-2);
  font-size: var(--font-size-xs);
}
.btn:disabled {
  cursor: not-allowed;
  background: var(--color-bg-muted);
  border-color: var(--color-border);
  color: var(--color-text-disabled);
}
/* 标签页：同样给悬停反馈（选中态已有 tab--on） */
.tab {
  transition:
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.tab:hover:not(.tab--on) {
  color: var(--color-brand);
  background: var(--color-bg-muted);
}
.meta {
  margin: 0 0 var(--space-1);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.err {
  margin: 0;
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}
.warn {
  margin: 0;
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}
.gen {
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
}
.gen__line {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
}
.gen__replay {
  color: var(--color-warning);
}
.gen__discarded {
  margin: 0;
  padding-left: var(--space-4);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}
.gen__error {
  margin: var(--space-1) 0 0;
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}
.tabs {
  display: flex;
  gap: var(--space-2);
}
.tab {
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.tab--on {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-weight: 600;
}
.ev-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.ev-list > li {
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-border);
}
.ev-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}
.ev-kind {
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}
.quote {
  margin: var(--space-2) 0;
  padding: var(--space-2);
  border-left: 3px solid var(--color-border-strong);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}
.link {
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  transition: filter 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.link:hover {
  filter: brightness(1.15);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.link:active {
  filter: brightness(0.95);
}
.block {
  margin-top: var(--space-3);
}
.risk {
  margin: 0;
  padding: 0;
  list-style: none;
}
.risk__item {
  padding: var(--space-2);
  margin-bottom: var(--space-2);
  border: 1px solid var(--color-border);
  border-left-width: 3px;
  border-radius: var(--radius-sm);
}
.risk__item--high {
  border-left-color: var(--color-danger);
  background: var(--color-danger-soft);
}
.risk__item--medium {
  border-left-color: var(--color-warning);
  background: var(--color-warning-soft);
}
.risk__item--low {
  border-left-color: var(--color-info);
  background: var(--color-info-soft);
}
.risk__head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
}
.risk__level {
  font-size: var(--font-size-xs);
  font-weight: 600;
}
.risk__text {
  margin: var(--space-1) 0;
}
.risk__mitigation {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.mve__obj {
  margin: 0 0 var(--space-2);
}
.mve {
  margin: 0;
  padding-left: var(--space-4);
}
.mve li {
  margin-bottom: var(--space-2);
}
.mve__action {
  font-size: var(--font-size-sm);
  font-weight: 600;
}
.mve__check {
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.drawer {
  position: fixed;
  top: 0;
  right: 0;
  z-index: var(--z-header);
  width: min(520px, 92vw);
  height: 100vh;
  overflow-y: auto;
  padding: var(--space-4);
  background: var(--color-bg-elevated);
  border-left: 1px solid var(--color-border);
  box-shadow: var(--shadow-popover);
}
.drawer__head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--space-2);
}
</style>
