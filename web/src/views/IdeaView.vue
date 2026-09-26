<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究构想（核心模块 ⑤）：**四个固定创新方向 + 七维可行性分析**。
 *
 * 界面口径（2026-09-26 研究者逐条确认，演示稿 `preview/ideas-demo.html` 定稿）
 * ---------------------------------------------------------------------------
 * - **四个方向固定**：方法迭代型 / 场景迁移型 / 技术融合型 / 范式拓展型，一次各出 1 条；
 *   对某个方向不满意就「再生成一批」，同方向的产出成为**备选 A/B/C** 横向对比；
 * - **输入自动适配**：选 1 篇＝单篇精读发散，选 ≥2 篇＝多篇交叉聚合（界面上直接写明判定）；
 * - **可行性分析七个维度**：0–100 分（**越高越好**）+ **模型生成的约 50 字分析**，
 *   默认全部展开，**研究者不能修改**（这是模型固定生成的结论）；
 * - 界面上**不写解释性小字**（状态用标签、口径用 tooltip）；**不用星级**，难度与评分都是数值。
 *
 * 已下线的旧交互（2026-09-26 研究者口径「只留新流程」）
 * ------------------------------------------------------
 * 手动录入 idea、逐条绑定证据、任务书表单 —— 三块都从界面移除。
 * idea 的出处仍在**后端**留痕（evidence 绑定与 manifest 不变），只是界面不再提供手工动作。
 */
import { computed, onMounted, ref } from 'vue'
import { writeDenied } from '@/utils/messages'

import {
  createAggregation,
  createFeasibility,
  generateIdeas,
  getFeasibilityByIdea,
  IDEA_DIRECTIONS,
  listIdeas,
  MECHANISM_DIFFICULTY,
  MECHANISM_LABELS,
  MECHANISM_LAYERS,
  REVIEW_DIMENSIONS,
  type Feasibility,
  type Idea,
  type IdeaMechanism,
} from '@/api/idea'
import { searchPapers, type PaperSearchItem } from '@/api/papers'

// ---- 输入 ----
const papers = ref<PaperSearchItem[]>([])
const pickedIds = ref<number[]>([])
const pickerOpen = ref(false)
const pickerKeyword = ref('')

// ---- 生成结果 ----
const aggregationId = ref<number | null>(null)
const ideas = ref<Idea[]>([])
const activeDirection = ref<IdeaMechanism>('refinement')
const activeIdeaId = ref<number | null>(null)

// ---- 可行性 ----
const feasibility = ref<Feasibility | null>(null)
const scoresByDirection = ref<Record<string, number | null>>({})

const loading = ref(false)
const busy = ref('')
const notice = ref('')

const DIRECTIONS = IDEA_DIRECTIONS.map((key, index) => ({
  key,
  idx: '①②③④'[index],
  label: MECHANISM_LABELS[key] ?? key,
  layer: MECHANISM_LAYERS[key] ?? '',
  difficulty: MECHANISM_DIFFICULTY[key] ?? 0,
}))

const pickedPapers = computed(() =>
  pickedIds.value
    .map((id) => papers.value.find((paper) => paper.id === id))
    .filter((paper): paper is PaperSearchItem => Boolean(paper)),
)
const inputMode = computed(() =>
  pickedIds.value.length >= 2
    ? '多篇交叉聚合'
    : pickedIds.value.length === 1
      ? '单篇精读发散'
      : '未选择论文',
)
const pickerItems = computed(() => {
  const keyword = pickerKeyword.value.trim().toLowerCase()
  if (!keyword) return papers.value.slice(0, 60)
  return papers.value
    .filter((paper) => (paper.title ?? '').toLowerCase().includes(keyword))
    .slice(0, 60)
})

/** 某个方向的备选：同一 mechanism 的 idea，最新的一版排最前 */
function variantsOf(key: IdeaMechanism): Idea[] {
  return ideas.value
    .filter((idea) => idea.mechanism === key)
    .slice()
    .sort((left, right) => right.id - left.id)
}
const activeVariants = computed(() => variantsOf(activeDirection.value))
const activeIdea = computed(
  () => activeVariants.value.find((idea) => idea.id === activeIdeaId.value) ?? activeVariants.value[0] ?? null,
)
const activeMeta = computed(
  () => DIRECTIONS.find((item) => item.key === activeDirection.value) ?? DIRECTIONS[0],
)

const dimensionRows = computed(() => {
  const rows = feasibility.value?.dimensions ?? []
  return REVIEW_DIMENSIONS.map((key) => {
    const found = rows.find((item) => item.key === key)
    return {
      key,
      label: found?.label ?? key,
      score: found?.score ?? null,
      analysis: found?.analysis ?? '',
    }
  })
})
const totalScore = computed(() => {
  const value = feasibility.value?.total_score
  return typeof value === 'number' ? Math.round(value) : null
})

function scoreClass(score: number | null): string {
  if (score === null) return ''
  if (score >= 75) return 'dim__score--high'
  if (score >= 50) return 'dim__score--mid'
  return 'dim__score--low'
}

function ownerHint(error: unknown): string {
  const status = (error as { status?: number })?.status
  if (status === 403) return writeDenied('生成研究构想')
  return error instanceof Error ? error.message : String(error)
}

function shortTitle(title: string | null | undefined): string {
  const value = title ?? ''
  return value.length > 34 ? `${value.slice(0, 34)}…` : value
}

// ---- 论文选择 ----
async function loadPapers(): Promise<void> {
  try {
    const page = await searchPapers({ pageSize: 100 })
    papers.value = page.items ?? []
  } catch {
    papers.value = []
  }
}

function togglePaper(id: number): void {
  const index = pickedIds.value.indexOf(id)
  if (index >= 0) pickedIds.value.splice(index, 1)
  else pickedIds.value.push(id)
}

// ---- 生成 ----
async function runGenerate(directions: IdeaMechanism[]): Promise<void> {
  if (pickedIds.value.length === 0) {
    notice.value = '先选择至少一篇论文'
    return
  }
  busy.value = directions.length === 1 ? `regen-${directions[0]}` : 'generate'
  notice.value = ''
  try {
    let target = aggregationId.value
    if (target === null) {
      const aggregation = await createAggregation({ paper_ids: [...pickedIds.value] })
      target = Number(aggregation.id ?? (aggregation as { aggregation_id?: number }).aggregation_id)
      if (!Number.isFinite(target)) {
        notice.value = '聚合创建失败：没有返回聚合号'
        return
      }
      aggregationId.value = target
    }
    const result = await generateIdeas({
      aggregation_id: target,
      directions,
      count: 1,
      mode: 'auto',
    })
    if (result.generated_count === 0) {
      notice.value = '这次没有产出可用的 idea：素材里没有足够的证据支撑，换一批论文或稍后重试。'
      return
    }
    await refreshIdeas()
    void collectScores(directions)
  } catch (error) {
    notice.value = ownerHint(error)
  } finally {
    busy.value = ''
  }
}

async function refreshIdeas(): Promise<void> {
  if (aggregationId.value === null) return
  loading.value = true
  try {
    const page = await listIdeas({ aggregationId: aggregationId.value, pageSize: 100 })
    ideas.value = page.items ?? []
    const first = activeVariants.value[0]
    activeIdeaId.value = first?.id ?? null
  } catch (error) {
    notice.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

/** 后台为这些方向各算一次可行性（界面按需拿分，不让用户等） */
async function collectScores(directions: IdeaMechanism[]): Promise<void> {
  await Promise.all(
    directions.map(async (key) => {
      const idea = variantsOf(key)[0]
      if (!idea) return
      try {
        const report = await createFeasibility({ idea_id: idea.id, use_llm: true })
        scoresByDirection.value = { ...scoresByDirection.value, [key]: Math.round(report.total_score) }
      } catch {
        scoresByDirection.value = { ...scoresByDirection.value, [key]: null }
      }
    }),
  )
}

// ---- 选择方向与备选 ----
async function selectDirection(key: IdeaMechanism): Promise<void> {
  activeDirection.value = key
  activeIdeaId.value = variantsOf(key)[0]?.id ?? null
  await loadFeasibility()
}

async function selectVariant(id: number): Promise<void> {
  activeIdeaId.value = id
  await loadFeasibility()
}

async function loadFeasibility(): Promise<void> {
  feasibility.value = null
  const idea = activeIdea.value
  if (!idea) return
  busy.value = 'feasibility'
  try {
    feasibility.value = await getFeasibilityByIdea(idea.id)
  } catch {
    feasibility.value = null
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  await loadPapers()
})
</script>

<template>
  <section class="ideas">
    <header class="ideas__head">
      <h1>研究构想</h1>
      <span class="spacer" />
      <button class="btn btn--ghost" type="button" @click="pickerOpen = true">
        {{ pickedIds.length ? '更换论文' : '选择论文' }}
      </button>
      <button
        class="btn btn--primary"
        type="button"
        :disabled="pickedIds.length === 0 || busy === 'generate'"
        @click="runGenerate([...IDEA_DIRECTIONS])"
      >
        {{ busy === 'generate' ? '生成中…' : '生成 4 个方向' }}
      </button>
    </header>

    <p v-if="notice" class="notice">{{ notice }}</p>

    <article class="panel">
      <header class="panel__head">
        <h2>输入</h2>
      </header>
      <div class="row">
        <span class="tag tag--brand">{{ inputMode }}</span>
        <span v-for="paper in pickedPapers" :key="paper.id" class="tag">
          #{{ paper.id }} · {{ shortTitle(paper.title) }}
        </span>
      </div>
    </article>

    <article class="panel">
      <header class="panel__head">
        <h2>四个创新方向</h2>
      </header>
      <div class="dircards">
        <button
          v-for="item in DIRECTIONS"
          :key="item.key"
          class="dircard"
          :class="{ 'is-on': item.key === activeDirection }"
          type="button"
          @click="selectDirection(item.key)"
        >
          <span class="dircard__top">
            <span class="dircard__name">{{ item.idx }} {{ item.label }}</span>
            <span class="dircard__score">{{ scoresByDirection[item.key] ?? '—' }}</span>
          </span>
          <span class="dircard__meta">
            难度 {{ item.difficulty }} · {{ item.layer }} · {{ variantsOf(item.key).length }} 个备选
          </span>
          <span class="dircard__brief">{{ variantsOf(item.key)[0]?.title ?? '尚未生成' }}</span>
        </button>
      </div>
    </article>

    <article v-if="activeIdea" class="panel">
      <header class="panel__head">
        <h2>{{ activeMeta.idx }} {{ activeMeta.label }}</h2>
        <span class="tag">{{ activeMeta.layer }}</span>
        <span class="tag">难度 {{ activeMeta.difficulty }}</span>
        <span class="spacer" />
        <button
          class="link-btn"
          type="button"
          :disabled="busy === `regen-${activeDirection}`"
          @click="runGenerate([activeDirection])"
        >
          {{ busy === `regen-${activeDirection}` ? '生成中…' : '再生成一批' }}
        </button>
      </header>

      <div v-if="activeVariants.length > 1" class="segmented">
        <button
          v-for="(variant, index) in activeVariants"
          :key="variant.id"
          class="segmented__item"
          :class="{ 'is-on': variant.id === activeIdea.id }"
          type="button"
          @click="selectVariant(variant.id)"
        >
          备选 {{ 'ABCDEF'[index] }}
        </button>
      </div>

      <h3 class="idea__title">{{ activeIdea.title }}</h3>
      <p class="idea__content">{{ activeIdea.content }}</p>
      <p v-if="activeIdea.novelty_note" class="idea__note">{{ activeIdea.novelty_note }}</p>

      <header class="panel__head panel__head--sub">
        <h2>可行性分析</h2>
        <span class="spacer" />
        <span class="total">{{ totalScore ?? '—' }}</span>
      </header>

      <p v-if="busy === 'feasibility'" class="empty">正在分析…</p>
      <div v-else-if="!feasibility" class="empty">这个方向还没有做可行性分析。</div>
      <div v-else class="dims">
        <div v-for="row in dimensionRows" :key="row.key" class="dim">
          <span class="dim__name">{{ row.label }}</span>
          <span class="dim__score" :class="scoreClass(row.score)">{{ row.score ?? '—' }}</span>
          <span class="dim__text">{{ row.analysis }}</span>
        </div>
      </div>
    </article>

    <Teleport to="body">
      <div v-if="pickerOpen" class="overlay" @click.self="pickerOpen = false">
        <section class="dialog">
          <header class="dialog__head">
            <h2>选择论文</h2>
            <span class="pill">{{ inputMode }}</span>
            <span class="spacer" />
            <button class="icon-btn" type="button" aria-label="关闭" @click="pickerOpen = false">✕</button>
          </header>
          <div class="dialog__body">
            <input v-model="pickerKeyword" class="field__input" type="search" placeholder="按标题筛选" />
            <div class="picklist">
              <label v-for="paper in pickerItems" :key="paper.id" class="pickrow">
                <input
                  type="checkbox"
                  :checked="pickedIds.includes(paper.id)"
                  @change="togglePaper(paper.id)"
                />
                <span class="pickrow__title">#{{ paper.id }} · {{ paper.title }}</span>
                <span class="muted">{{ paper.venue ?? paper.source ?? '' }}</span>
              </label>
              <p v-if="pickerItems.length === 0" class="empty">没有匹配的论文。</p>
            </div>
          </div>
          <footer class="dialog__foot">
            <span class="muted">已选 {{ pickedIds.length }} 篇</span>
            <span class="spacer" />
            <button class="btn" type="button" @click="pickedIds = []">清空</button>
            <button class="btn btn--primary" type="button" @click="pickerOpen = false">确定</button>
          </footer>
        </section>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.ideas {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.ideas__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.ideas__head h1 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.spacer {
  flex: 1;
}
.panel {
  padding: var(--space-4);
  background: var(--color-card-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.panel__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin: 0;
}
.panel__head--sub {
  margin-top: var(--space-2);
}
.panel__head h2 {
  margin: 0;
  font-size: var(--font-size-lg);
}
.row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.muted {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

/* 四个方向卡 */
.dircards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  gap: var(--space-2);
}
.dircard {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 12px 14px;
  text-align: left;
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: inherit;
  font: inherit;
  transition: border-color 160ms, background-color 160ms;
}
.dircard:hover {
  border-color: var(--color-border-strong);
}
.dircard.is-on {
  border-color: var(--color-brand);
  background: var(--color-brand-soft);
  box-shadow: inset 2px 0 0 var(--color-brand);
}
.dircard__top {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}
.dircard__name {
  font-size: var(--font-size-sm);
  font-weight: 600;
}
.dircard__score {
  margin-left: auto;
  font-size: var(--font-size-lg);
  font-weight: 600;
  color: var(--color-brand);
}
.dircard__meta {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.dircard__brief {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

/* 备选切换 */
.segmented {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  align-self: flex-start;
}
.segmented__item {
  padding: 4px 12px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.segmented__item.is-on {
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  font-weight: 500;
}

/* idea 正文 */
.idea__title {
  margin: 0;
  font-size: var(--font-size-md);
  font-weight: 600;
}
.idea__content {
  margin: 0;
  font-size: var(--font-size-sm);
  line-height: 1.75;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
}
.idea__note {
  margin: 0;
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
  font-size: var(--font-size-xs);
  line-height: 1.7;
  color: var(--color-text-secondary);
}

/* 可行性分析：每维 0–100 分 + 约 50 字固定分析（只读） */
.total {
  font-size: 20px;
  font-weight: 600;
  color: var(--color-brand);
}
.dims {
  display: flex;
  flex-direction: column;
}
.dim {
  display: grid;
  grid-template-columns: 118px 54px minmax(0, 1fr);
  gap: 14px;
  align-items: baseline;
  padding: 11px 2px;
  border-bottom: 1px solid var(--color-border);
}
.dim:last-child {
  border-bottom: 0;
}
.dim__name {
  font-size: var(--font-size-sm);
}
.dim__score {
  font-size: var(--font-size-md);
  font-weight: 600;
  text-align: right;
}
.dim__score--high {
  color: var(--color-success);
}
.dim__score--mid {
  color: var(--color-warning);
}
.dim__score--low {
  color: var(--color-danger);
}
.dim__text {
  font-size: var(--font-size-sm);
  line-height: 1.75;
  color: var(--color-text-secondary);
}
.empty {
  margin: 0;
  padding: var(--space-4) 0;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
}

/* 选论文弹窗 */
.overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: rgba(15, 23, 42, 0.32);
}
.dialog {
  width: min(680px, 100%);
  max-height: min(78vh, 100%);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
}
.dialog__head,
.dialog__foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
}
.dialog__head {
  border-bottom: 1px solid var(--color-border);
}
.dialog__head h2 {
  margin: 0;
  font-size: var(--font-size-md);
}
.dialog__foot {
  border-top: 1px solid var(--color-border);
}
.dialog__body {
  padding: var(--space-3);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.icon-btn {
  width: 30px;
  height: 30px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
}
.pill {
  padding: 2px 10px;
  border-radius: 999px;
  font-size: var(--font-size-xs);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
}
.picklist {
  display: flex;
  flex-direction: column;
}
.pickrow {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 8px 4px;
  border-bottom: 1px solid var(--color-border);
  font-size: var(--font-size-sm);
  cursor: pointer;
}
.pickrow:last-child {
  border-bottom: 0;
}
.pickrow__title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.field__input {
  height: 34px;
  padding: 0 12px;
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
}
@media (max-width: 900px) {
  .dim {
    grid-template-columns: 100px 46px minmax(0, 1fr);
    gap: 10px;
  }
}
</style>
