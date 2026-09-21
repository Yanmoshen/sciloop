<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 聚合对比页（WP08-T7 / 附录 C.1：对比矩阵 + 方法演进时间线 + 空白清单）。
 *
 * 覆盖入口：路由 `/papers/aggregate/:id`（WP01 已注册）。整页覆盖 WP01 占位实现。
 *
 * 说明：证据抽屉（EvidenceDrawer）**不在 WP08 的 owned_paths** 内（见
 * assets/tasks/index.json -> ownership_map.WP08），因此本页把抽屉内联实现，
 * 证据解析复用统一的证据接口（不另写一套）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'
import { useRoute, useRouter } from 'vue-router'

import { getEvidenceDetail, type EvidenceDetail } from '@/api/claims'
import {
  evidenceTypeLabel,
  type CellEvidence,
  type EvidenceCandidate,
} from '@/api/idea'
import EvolutionTimeline from '@/components/EvolutionTimeline.vue'
import GapList from '@/components/GapList.vue'
import MatrixTable from '@/components/MatrixTable.vue'
import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { useIdeaStore } from '@/stores/idea'
import { useSessionStore } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const store = useIdeaStore()
const session = useSessionStore()

/** 创建聚合是写操作：浏览模式下前置禁用并说明怎么启用 */
const canWrite = computed(() => session.isOwner)
const writeDeniedNote = computed(() =>
  canWrite.value
    ? null
    : writeDenied('创建对比分析') + '浏览模式下仍可在左侧「已有聚合」查看真实结果。',
)

/**
 * 权限态：只读面下写入口已**前置禁用**；若本次请求确实收到 403，再明确说明「已被拒绝」。
 * 依据 store 记录的真实 HTTP 状态码判断，不靠对错误文案做字符串匹配。
 */
const permissionDenied = computed(() => !canWrite.value || store.errorStatus === 403)
const permissionTitle = computed(() =>
  store.errorStatus === 403
    ? writeDenied('该写操作')
    : '只读演示面：创建聚合入口已前置禁用（非请求被拒）',
)
const permissionNote = computed(() => {
  if (store.errorStatus === 403) {
    return `${store.aggregationError ?? ''} ${writeDeniedNote.value ?? ''}`.trim()
  }
  return writeDeniedNote.value
})

const busy = computed(() =>
  ['createAggregation', 'loadAggregation', 'loadAggregations'].includes(store.busy ?? ''),
)

/** 降级：矩阵声明了取不到卡片的论文，或存在仅摘要级证据的行 */
const matrixMissing = computed(() => store.aggregation?.comparison_matrix?.missing ?? [])
const abstractOnlyRows = computed(
  () =>
    (store.aggregation?.comparison_matrix?.rows ?? []).filter((row) => row.scope === 'abstract_only'),
)

const paperInput = ref('')
const tab = ref<'matrix' | 'evolution' | 'gaps'>('matrix')
const activePaperId = ref<number | null>(null)
/** 动作反馈：输入不合法 / 写操作被拒 / 创建失败时给出可见原因，杜绝「点了没反应」 */
const actionNotice = ref<string | null>(null)

// ---- 证据抽屉（内联实现，数据来自统一证据契约） ----
const drawerOpen = ref(false)
const drawerTitle = ref('')
const drawerItems = ref<CellEvidence[]>([])
const detailLoading = ref<string | null>(null)
const details = ref<Record<string, EvidenceDetail>>({})
const detailErrors = ref<Record<string, string>>({})

function parsePaperIds(value: string): number[] {
  return value
    .split(/[,，\s]+/)
    .map((part) => Number(part.trim()))
    .filter((value) => Number.isInteger(value) && value > 0)
}

/**
 * 可一键填入的论文 id：只取**库内真实存在的聚合**回读到的 `paper_ids`。
 * 这里刻意不硬编码任何 id——写死一串"样本 id"既可能不在库中（点下去只会 404/降级），
 * 也会把编造的取值伪装成真实数据。
 */
const knownPaperIds = computed(() =>
  Array.from(new Set(store.aggregations.flatMap((item) => item.paper_ids ?? []))),
)

function fillKnownPapers(): void {
  if (knownPaperIds.value.length === 0) return
  paperInput.value = knownPaperIds.value.slice(0, 20).join(',')
}

async function createAggregation(): Promise<void> {
  if (!canWrite.value) {
    actionNotice.value = writeDeniedNote.value
    return
  }
  const ids = parsePaperIds(paperInput.value)
  if (ids.length < 2) {
    // 输入不合法时给出明确反馈，避免出现「点了没反应」的入口
    actionNotice.value = `需要 2–20 个有效论文 id（当前解析到 ${ids.length} 个）。请输入库内真实 paper_id，用逗号分隔。`
    return
  }
  actionNotice.value = null
  const payload = await store.createAggregationForPapers(ids, session.currentProjectId)
  if (payload) {
    tab.value = 'matrix'
    await router.replace({ name: 'aggregate', params: { id: String(payload.id) } })
  } else {
    actionNotice.value = store.aggregationError
  }
}

/** 可重试动作：重拉聚合列表与当前聚合详情（失败时不清空已有结果） */
async function retryAggregation(): Promise<void> {
  await store.loadAggregations(session.currentProjectId)
  if (store.aggregation) await store.loadAggregation(store.aggregation.id)
}

async function loadFromRoute(): Promise<void> {
  const raw = route.params.id
  if (typeof raw === 'string' && raw.trim()) {
    await store.loadAggregation(raw)
    if (store.aggregation?.paper_ids?.length) {
      paperInput.value = store.aggregation.paper_ids.join(',')
    }
  }
}

onMounted(async () => {
  await store.loadAggregations(session.currentProjectId)
  await loadFromRoute()
})

watch(
  () => route.params.id,
  async () => {
    await loadFromRoute()
  },
)

function openEvidence(payload: { paperId: number; label?: string; dimension?: string; items: CellEvidence[] }): void {
  drawerTitle.value =
    payload.label ?? `论文 #${payload.paperId} · 维度 ${payload.dimension ?? '—'} 的证据`
  drawerItems.value = payload.items ?? []
  drawerOpen.value = true
}

function candidateKey(candidate: EvidenceCandidate): string {
  return JSON.stringify(candidate)
}

async function loadDetail(ev: CellEvidence): Promise<void> {
  const key = candidateKey(ev.candidate)
  if (details.value[key] || detailLoading.value === key) return
  const spanId = ev.candidate.paper_span_id ?? ev.paper_span_id ?? null
  if (!spanId) {
    // card_field 候选的 native id 是 paper_cards.id（矩阵响应未携带），
    // 因此这里如实告知可用的核验路径，**不伪造 card_id 去糊一个能过的请求**。
    detailErrors.value = {
      ...detailErrors.value,
      [key]:
        '卡片字段证据的 native id 是 paper_cards.id（本响应未携带）：请用「跳转论文解析页」核验原文，' +
        '或先把该 candidate 绑定到 idea，再用证据行 id 走 id_kind=evidence 解析',
    }
    return
  }
  detailLoading.value = key
  try {
    const detail = await getEvidenceDetail('paper_span', Number(spanId), { idKind: 'native' })
    details.value = { ...details.value, [key]: detail }
  } catch (error) {
    detailErrors.value = {
      ...detailErrors.value,
      [key]: error instanceof Error ? error.message : String(error),
    }
  } finally {
    detailLoading.value = null
  }
}

const aggregation = computed(() => store.aggregation)
</script>

<template>
  <section class="agg">

    <header class="agg__header">
      <div>
        <h1>聚合对比</h1>
      </div>
      <div class="agg__head-side">
        <span class="agg__meta">
          {{ aggregation ? `聚合 #${aggregation.id}` : '尚未选择聚合' }}
        </span>
      </div>
    </header>

    <!-- 六类状态：loading / error / permission denied / retry（empty 见各 Tab） -->
    <ViewStatePanel
      :loading="busy"
      loading-text="正在聚合论文（矩阵 / 演进 / 空白），长任务请稍候…"
      :error="store.aggregationError"
      :error-code="store.errorCode"
      error-title="聚合失败"
      :permission-denied="permissionDenied"
      :permission-note="permissionNote"
      :permission-title="permissionTitle"
      retryable
      retry-label="重试聚合请求"
      :busy="busy"
      @retry="retryAggregation"
    />

    <section class="agg__panel">
      <div class="agg__row">
        <label class="agg__field">
          <span>论文 id（2–20 篇，逗号分隔；必须是真实库中已有解析卡片的论文）</span>
          <input v-model="paperInput" placeholder="例如 123,124,125" />
        </label>
        <button
          type="button"
          class="agg__btn agg__btn--ghost"
          :disabled="knownPaperIds.length === 0"
          :title="
            knownPaperIds.length === 0
              ? '库内尚无已落库的聚合可回读 paper_ids，无法提供真实样本'
              : `取自库内已有聚合的 paper_ids（共 ${knownPaperIds.length} 篇）`
          "
          @click="fillKnownPapers"
        >
          填入库内已有聚合用过的论文 id（{{ knownPaperIds.length }}）
        </button>
        <button
          type="button"
          class="agg__btn agg__btn--primary"
          :disabled="!canWrite || store.busy === 'createAggregation'"
          :title="canWrite ? 'POST /aggregations' : '浏览模式下不可写：需先在「设置」启用编辑'"
          @click="createAggregation"
        >
          {{ store.busy === 'createAggregation' ? '聚合中…' : '创建聚合' }}
        </button>
        <span v-if="!canWrite" class="agg__denied">浏览模式：创建聚合已禁用</span>
      </div>

      <el-alert
        v-if="actionNotice"
        class="agg__notice"
        type="warning"
        :closable="false"
        show-icon
        :title="actionNotice"
      />

      <div class="agg__row">
        <label class="agg__field">
          <span>已有聚合</span>
          <select
            :value="aggregation?.id ?? ''"
            @change="(event) => store.loadAggregation((event.target as HTMLSelectElement).value)"
          >
            <option value="">选择聚合…</option>
            <option v-for="item in store.aggregations" :key="item.id" :value="item.id">
              #{{ item.id }} · {{ item.paper_count }} 篇 · 空白 {{ item.gap_count ?? '-' }}
            </option>
          </select>
        </label>
        <span v-if="aggregation" class="agg__meta">
          聚合 #{{ aggregation.id }} · 矩阵 {{ aggregation.comparison_matrix?.row_count ?? '-' }} 行 ·
          演进 {{ aggregation.method_evolution?.relation_count ?? 0 }} 条 ·
          空白 {{ aggregation.gap_count ?? 0 }} 条
        </span>
      </div>

      <el-empty
        v-if="!aggregation && !busy"
        description="尚未选择或创建聚合：请选择左侧「已有聚合」，或（Owner 面）输入论文 id 创建"
      />
    </section>

    <nav class="agg__tabs">
      <button type="button" :class="['agg__tab', { 'agg__tab--on': tab === 'matrix' }]" @click="tab = 'matrix'">
        对比矩阵
      </button>
      <button type="button" :class="['agg__tab', { 'agg__tab--on': tab === 'evolution' }]" @click="tab = 'evolution'">
        方法演进（{{ aggregation?.method_evolution?.relation_count ?? 0 }}）
      </button>
      <button type="button" :class="['agg__tab', { 'agg__tab--on': tab === 'gaps' }]" @click="tab = 'gaps'">
        空白清单（{{ store.gaps.length }}）
      </button>
    </nav>

    <section v-show="tab === 'matrix'" class="agg__body">
      <MatrixTable
        v-if="aggregation?.comparison_matrix"
        :matrix="aggregation.comparison_matrix"
        :active-paper-id="activePaperId"
        @evidence="openEvidence"
        @paper="(payload) => (activePaperId = payload.paperId)"
      />
      <el-empty v-else description="尚无对比矩阵：选择或创建聚合后展示（不渲染占位行）" />
    </section>

    <section v-show="tab === 'evolution'" class="agg__body">
      <EvolutionTimeline
        v-if="aggregation?.method_evolution"
        :evolution="aggregation.method_evolution"
        @evidence="(payload) => openEvidence({ paperId: payload.paperId, label: payload.label, items: payload.items })"
      />
      <el-empty v-else description="尚无方法演进关系：需论文集合产出可匹配的机制线索（不编造关系）" />
    </section>

    <section v-show="tab === 'gaps'" class="agg__body">
      <GapList :gaps="store.gaps" />
    </section>

    <!-- 证据抽屉（内联，非 owned_paths 组件） -->
    <aside v-if="drawerOpen" class="drawer scroll-y">
      <header class="drawer__head">
        <h3>{{ drawerTitle }}</h3>
        <button type="button" class="drawer__close" @click="drawerOpen = false">关闭</button>
      </header>
      <el-empty v-if="drawerItems.length === 0" description="该单元格未挂载证据" />
      <ul v-else class="drawer__list">
        <li v-for="(ev, index) in drawerItems" :key="index" class="drawer__item">
          <div class="drawer__row">
            <span class="drawer__kind">{{ evidenceTypeLabel(ev.kind) }}</span>
            <span class="drawer__label">{{ ev.label }}</span>
            <span v-if="ev.scope" class="drawer__scope">{{ ev.scope === 'fulltext' ? '全文可用' : '证据覆盖范围：仅摘要' }}</span>
          </div>
          <blockquote v-if="ev.quote_text" class="drawer__quote">{{ ev.quote_text }}</blockquote>
          <p class="drawer__meta">
            章节 {{ ev.section_name || '未标注' }} · 定位 {{ ev.locator_kind || '未记录' }}
            · document_version {{ ev.document_version ? '已携带' : '未携带（仅摘要级）' }}
          </p>
          <div class="drawer__actions">
            <button type="button" class="drawer__btn" :disabled="detailLoading === candidateKey(ev.candidate)" @click="loadDetail(ev)">
              {{ detailLoading === candidateKey(ev.candidate) ? '解析中…' : '解析证据' }}
            </button>
            <a class="drawer__link" :href="`/papers/parse/${ev.paper_id}`">跳转论文解析页 /papers/parse/{{ ev.paper_id }}</a>
          </div>
          <div v-if="details[candidateKey(ev.candidate)]" class="drawer__detail">
            <p>
              判定 verdict：<strong>{{ details[candidateKey(ev.candidate)].verification?.verdict ?? '未提供' }}</strong>
              （哈希 {{ details[candidateKey(ev.candidate)].verification?.hash_match === true ? '匹配' : '未匹配 / 未提供' }}）
            </p>
            <p>
              证据范围：
              {{ details[candidateKey(ev.candidate)].gate?.evidence_scope ?? '未提供' }}
              · 覆盖率 {{ details[candidateKey(ev.candidate)].gate?.coverage ?? '未提供' }}
            </p>
            <p>{{ details[candidateKey(ev.candidate)].gate?.coverage_note || details[candidateKey(ev.candidate)].gate?.reason || '' }}</p>
            <p class="drawer__quote-line">{{ details[candidateKey(ev.candidate)].quote_text || '（无引用文本）' }}</p>
          </div>
          <p v-if="detailErrors[candidateKey(ev.candidate)]" class="drawer__err">
            解析失败：{{ detailErrors[candidateKey(ev.candidate)] }}
          </p>
        </li>
      </ul>
    </aside>
  </section>
</template>

<style scoped>
.agg {
  padding: var(--space-4);
}
.agg__header {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--space-3);
  align-items: flex-start;
}
.agg__panel {
  padding: var(--space-3);
  margin-bottom: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}
.agg__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: flex-end;
  margin-bottom: var(--space-3);
}
.agg__field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  flex: 1 1 320px;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.agg__field input,
.agg__field select {
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-bg-page);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
}
.agg__btn {
  padding: var(--space-1) var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
  color: var(--color-text-primary);
  cursor: pointer;
}
.agg__btn--primary {
  border-color: var(--color-brand);
  background: var(--color-brand);
  color: var(--color-text-inverse);
}
.agg__btn--ghost {
  color: var(--color-brand);
}
.agg__btn:disabled {
  cursor: not-allowed;
  color: var(--color-text-disabled);
  background: var(--color-bg-muted);
}
.agg__meta {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.agg__head-side {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.agg__denied {
  padding: 1px var(--space-2);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.agg__notice {
  margin-bottom: var(--space-3);
}
.agg__tabs {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.agg__tab {
  padding: var(--space-2) var(--space-4);
  border: 1px solid var(--color-border);
  border-bottom: none;
  border-radius: var(--radius-md) var(--radius-md) 0 0;
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.agg__tab:hover {
  color: var(--color-brand);
  border-color: var(--color-brand);
}
.agg__tab--on {
  background: var(--color-card-bg);
  color: var(--color-brand);
  font-weight: 600;
}
.agg__body {
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-card-bg);
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
.drawer__close,
.drawer__btn {
  padding: 1px var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-pill);
  background: var(--color-bg-subtle);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.drawer__list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.drawer__item {
  padding: var(--space-3) 0;
  border-bottom: 1px solid var(--color-border);
}
.drawer__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: baseline;
}
.drawer__kind {
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-brand-soft);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}
.drawer__label {
  font-size: var(--font-size-sm);
}
.drawer__scope {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.drawer__quote {
  margin: var(--space-2) 0;
  padding: var(--space-2);
  border-left: 3px solid var(--color-border-strong);
  background: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  line-height: var(--line-height-base);
}
.drawer__meta {
  margin: 0 0 var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}
.drawer__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}
.drawer__link {
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}
.drawer__detail {
  margin-top: var(--space-2);
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-subtle);
  font-size: var(--font-size-xs);
}
.drawer__detail p {
  margin: 0 0 var(--space-1);
}
.drawer__quote-line {
  color: var(--color-text-secondary);
}
.drawer__err {
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}
</style>
