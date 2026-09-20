<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 任务书表单（WP08-T6 / WP08-A7：可编辑、可锁定，锁定后只读）。
 *
 * 红线在前端也如实体现（服务端仍是唯一权威）：
 * - 成本**双线并列**展示：硬护栏值与演示配额（contracts.ui_mandatory_elements）；
 * - `max_llm_cost_usd` 输入上限 = 硬护栏值，超出即在前端提示「不可放宽」，提交也会被服务端 422 拒；
 * - 锁定后所有输入 disabled，按钮仅剩「已锁定」，并原样展示服务端 409 文案（父组件透传 error）。
 * - 轮次配置四项（max_iterations / score_threshold / marginal_gain_threshold / max_retry）
 *   一律显式呈现，禁止只留一个「轮次」笼统字段。
 */
import { computed, ref, watch } from 'vue'

import { DELIVERABLE_OPTIONS, type Feasibility, type Taskbook } from '@/api/idea'

const props = defineProps<{
  taskbook: Taskbook | null
  feasibility: Feasibility | null
  ideaId: number | null
  projectId: number | null
  busy?: string | null
  /** 服务端返回的错误原文（含 409 已锁定），原样展示 */
  serverError?: string | null
  /** 当前访问面是否可写（public_demo 匿名面为 false：写操作前置禁用并给出原因） */
  canWrite?: boolean
}>()

const emit = defineEmits<{
  (event: 'create', payload: Record<string, unknown>): void
  (event: 'save', payload: { id: number; patch: Record<string, unknown> }): void
  (event: 'lock', payload: { id: number }): void
}>()

const FALLBACK_HARD_LIMIT = 8.0
const FALLBACK_QUOTA = 3.0

const locked = computed(() => props.taskbook?.status === 'locked' || props.taskbook?.read_only === true)
/** 只读判定：锁定（服务端强制）或当前访问面不可写（public_demo 匿名） */
const readOnly = computed(() => locked.value || props.canWrite === false)
const writeDenied = computed(() => props.canWrite === false && !locked.value)
const hardLimit = computed(
  () => props.taskbook?.compute_budget?.guardrail?.hard_limit_usd ?? FALLBACK_HARD_LIMIT,
)
const demoQuota = computed(
  () => props.taskbook?.compute_budget?.guardrail?.demo_quota_usd ?? FALLBACK_QUOTA,
)

const researchQuestion = ref('')
const datasets = ref('')
const baselines = ref('')
const metrics = ref('')
const deliverables = ref<string[]>(['paper_draft', 'experiment_log'])
const maxCost = ref(FALLBACK_HARD_LIMIT)
const quota = ref(FALLBACK_QUOTA)
const stageMinutes = ref(20)
const maxIterations = ref(3)
const scoreThreshold = ref(80)
const marginalGain = ref(2)
const maxRetry = ref(2)
const notice = ref<string | null>(null)

function joinList(values: string[] | null | undefined): string {
  return (values ?? []).join('、')
}

function splitList(value: string): string[] {
  return value
    .split(/[,，、\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

function hydrate(): void {
  const tb = props.taskbook
  if (tb) {
    researchQuestion.value = tb.research_question ?? ''
    datasets.value = joinList(tb.target_datasets)
    baselines.value = joinList(tb.baselines)
    metrics.value = joinList(tb.metrics)
    deliverables.value = [...(tb.deliverables ?? [])]
    maxCost.value = Number(tb.compute_budget?.max_llm_cost_usd ?? hardLimit.value)
    quota.value = Number(tb.compute_budget?.demo_cost_quota_usd ?? demoQuota.value)
    stageMinutes.value = Number(tb.compute_budget?.max_stage_minutes ?? 20)
    maxIterations.value = Number(tb.max_iterations ?? 3)
    scoreThreshold.value = Number(tb.score_threshold ?? 80)
    marginalGain.value = Number(tb.marginal_gain_threshold ?? 2)
    maxRetry.value = Number(tb.max_retry ?? 2)
    return
  }
  const mve = props.feasibility?.mve_plan
  const ideaTitle = props.feasibility?.idea_id ? `验证 idea #${props.feasibility.idea_id} 的核心机制` : ''
  researchQuestion.value =
    mve?.objective ?? (ideaTitle ? `${ideaTitle}：在选定数据集上是否成立？` : '')
  datasets.value = mve?.dataset?.name ? mve.dataset.name : ''
  baselines.value = joinList(mve?.baselines?.values)
  metrics.value = joinList(mve?.metrics?.values)
  maxCost.value = Number(mve?.rounds ? hardLimit.value : hardLimit.value)
  quota.value = demoQuota.value
}

watch(() => props.taskbook?.id, hydrate, { immediate: true })
watch(() => props.feasibility?.feasibility_id, hydrate)

const costTooHigh = computed(() => Number(maxCost.value) > Number(hardLimit.value))
const quotaTooHigh = computed(() => Number(quota.value) > Number(hardLimit.value))

function createPayload(): Record<string, unknown> {
  notice.value = null
  if (costTooHigh.value) {
    notice.value = `max_llm_cost_usd=${maxCost.value} 超过硬护栏 ${hardLimit.value}：护栏不可放宽（服务端会返回 guardrail_relaxed）`
    return {}
  }
  if (quotaTooHigh.value) {
    notice.value = `演示配额 ${quota.value} 不得高于硬护栏 ${hardLimit.value}`
    return {}
  }
  if (!researchQuestion.value.trim()) {
    notice.value = '研究问题不能为空'
    return {}
  }
  return {
    project_id: props.projectId,
    idea_id: props.ideaId,
    research_question: researchQuestion.value.trim(),
    target_datasets: splitList(datasets.value),
    baselines: splitList(baselines.value),
    metrics: splitList(metrics.value),
    deliverables: deliverables.value,
    compute_budget: {
      max_llm_cost_usd: Number(maxCost.value),
      demo_cost_quota_usd: Number(quota.value),
      max_stage_minutes: Number(stageMinutes.value),
    },
    rounds: {
      max_iterations: Number(maxIterations.value),
      score_threshold: Number(scoreThreshold.value),
      marginal_gain_threshold: Number(marginalGain.value),
      max_retry: Number(maxRetry.value),
    },
  }
}

function onCreate(): void {
  const payload = createPayload()
  if (Object.keys(payload).length === 0) return
  emit('create', payload)
}

function onSave(): void {
  const tb = props.taskbook
  if (!tb) return
  const payload = createPayload()
  if (Object.keys(payload).length === 0) return
  const { project_id: _project, idea_id: _idea, ...patch } = payload
  emit('save', { id: tb.id, patch })
}
</script>

<template>
  <section class="tb">
    <header class="tb__head">
      <h3>任务书</h3>
      <span v-if="taskbook" class="tb__status" :class="locked ? 'tb__status--locked' : 'tb__status--draft'">
        {{ locked ? `已锁定（只读）· ${taskbook.locked_at || ''}` : 'draft（可编辑）' }}
      </span>
      <span v-else class="tb__status tb__status--none">尚未创建</span>
    </header>

    <p class="tb__banner">本内容由 AI 辅助生成，需研究者自行核验</p>

    <div class="tb__grid">
      <label class="tb__field tb__field--wide">
        <span>研究问题</span>
        <textarea v-model="researchQuestion" :disabled="readOnly" rows="3" />
      </label>

      <label class="tb__field">
        <span>目标数据集（顿号/逗号分隔）</span>
        <input v-model="datasets" :disabled="readOnly" placeholder="未提供则留空，禁止编造" />
      </label>
      <label class="tb__field">
        <span>Baselines</span>
        <input v-model="baselines" :disabled="readOnly" />
      </label>
      <label class="tb__field">
        <span>评价指标</span>
        <input v-model="metrics" :disabled="readOnly" />
      </label>
      <label class="tb__field">
        <span>交付形态（{{ DELIVERABLE_OPTIONS.join(' / ') }}）</span>
        <span class="tb__checks">
          <label v-for="option in DELIVERABLE_OPTIONS" :key="option" class="tb__check">
            <input v-model="deliverables" type="checkbox" :value="option" :disabled="readOnly" />
            {{ option }}
          </label>
        </span>
      </label>
    </div>

    <fieldset class="tb__group" :disabled="readOnly">
      <legend>成本预算（双线：硬护栏 / 演示配额）</legend>
      <label class="tb__field">
        <span>max_llm_cost_usd（硬护栏 {{ hardLimit }} USD，不可放宽）</span>
        <input v-model.number="maxCost" type="number" step="0.1" min="0.1" :max="hardLimit" />
      </label>
      <label class="tb__field">
        <span>demo_cost_quota_usd（演示配额 {{ demoQuota }} USD，只告警不熔断）</span>
        <input v-model.number="quota" type="number" step="0.1" min="0" :max="hardLimit" />
      </label>
      <label class="tb__field">
        <span>max_stage_minutes（单环节上限，契约 20 分钟）</span>
        <input v-model.number="stageMinutes" type="number" min="1" max="1200" />
      </label>
      <p v-if="costTooHigh" class="tb__warn">已超过硬护栏：护栏阈值不可被参数覆盖，提交会被拒绝。</p>
      <p v-else-if="quotaTooHigh" class="tb__warn">演示配额不得高于硬护栏。</p>
    </fieldset>

    <fieldset class="tb__group" :disabled="readOnly">
      <legend>轮次配置</legend>
      <label class="tb__field">
        <span>max_iterations</span>
        <input v-model.number="maxIterations" type="number" min="1" max="10" />
      </label>
      <label class="tb__field">
        <span>score_threshold</span>
        <input v-model.number="scoreThreshold" type="number" min="0" max="100" step="0.5" />
      </label>
      <label class="tb__field">
        <span>marginal_gain_threshold</span>
        <input v-model.number="marginalGain" type="number" min="0" max="50" step="0.5" />
      </label>
      <label class="tb__field">
        <span>max_retry</span>
        <input v-model.number="maxRetry" type="number" min="0" max="5" />
      </label>
    </fieldset>

    <p v-if="notice" class="tb__warn">{{ notice }}</p>
    <p v-if="serverError" class="tb__error">服务端返回：{{ serverError }}</p>

    <footer class="tb__actions">
      <button
        v-if="!taskbook"
        type="button"
        class="tb__btn tb__btn--primary"
        :disabled="readOnly || !ideaId || !projectId"
        @click="onCreate"
      >
        创建任务书（draft）
      </button>
      <template v-else>
        <button type="button" class="tb__btn" :disabled="readOnly" @click="onSave">保存修改</button>
        <button type="button" class="tb__btn tb__btn--primary" :disabled="readOnly" @click="emit('lock', { id: taskbook.id })">
          锁定为执行依据
        </button>
      </template>
      <span v-if="writeDenied" class="tb__warn">
        当前为浏览模式：生成任务书是写操作，需先在「设置」里启用编辑。
      </span>
    </footer>
  </section>
</template>

<style scoped>
.tb__head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
}
.tb__status {
  padding: 0 var(--space-2);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
}
.tb__status--draft {
  background: var(--color-info-soft);
  color: var(--color-info);
}
.tb__status--locked {
  background: var(--color-success-soft);
  color: var(--color-success);
}
.tb__status--none {
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
}
.tb__banner {
  margin: var(--space-2) 0;
  padding: var(--space-1) var(--space-3);
  border-left: 3px solid var(--color-warning);
  background: var(--color-warning-soft);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.tb__grid,
.tb__group {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-3);
}
.tb__group {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}
.tb__group legend {
  padding: 0 var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.tb__field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.tb__field--wide {
  grid-column: 1 / -1;
}
.tb__field input,
.tb__field textarea {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  font-family: inherit;
  font-size: var(--font-size-sm);
}
.tb__checks {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}
.tb__check {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--color-text-primary);
}
.tb__warn {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}
.tb__error {
  margin: 0 0 var(--space-2);
  padding: var(--space-2);
  border-left: 3px solid var(--color-danger);
  background: var(--color-danger-soft);
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}
.tb__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-2);
}
.tb__btn {
  padding: var(--space-1) var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  cursor: pointer;
}
.tb__btn--primary {
  border-color: var(--color-brand);
  background: var(--color-brand);
  color: var(--color-text-inverse);
}
.tb__btn:disabled {
  cursor: not-allowed;
  background: var(--color-bg-muted);
  border-color: var(--color-border);
  color: var(--color-text-disabled);
}
</style>
