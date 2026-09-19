<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Claim 草稿阅读器（WP15-T7）。
 *
 * - Markdown 渲染（本地安全渲染：先转义再套用有限语法，禁止 v-html 注入未转义内容）
 * - 引用 [n] 可点击 → 打开证据抽屉（数据来自 GET /evidence/{type}/{id}，WP13）
 * - Claim 三态用 tokens 的 --color-claim-* 配色；**insufficient 段落醒目高亮**
 * - 三件套下载入口（论文草稿 / 代码 / 实验记录）+ Passport 链接
 *
 * 依赖未挂载接口时显示「等待 WPxx 交付」空状态，**不伪造草稿内容**。
 *
 * 说明：WP08 当前未交付独立的 EvidenceDrawer 组件（其 owned_paths 中不含该文件），
 * 因此抽屉在本组件内实现；WP08 交付后可将 `.drawer` 部分替换为对其组件的引用。
 */
import { computed, ref } from 'vue'

import { draftExportUrl, WP13_WAITING, WP14_WAITING, type ClaimStatus, type DraftClaim } from '@/api/workbench'
import { useWorkbenchStore } from '@/stores/workbench'

const emit = defineEmits<{ (event: 'goto-passport', runId: number | null): void }>()

const store = useWorkbenchStore()
const draftId = ref<number | undefined>(undefined)
const drawerOpen = ref(false)

const CLAIM_LABELS: Record<ClaimStatus, string> = {
  supported: 'supported（有证据支持）',
  contradicted: 'contradicted（被证据反驳）',
  insufficient: 'insufficient（证据不足）',
}

const draft = computed(() => (store.draft?.available ? store.draft.data : null))
const draftUnavailable = computed(() => (store.draft && !store.draft.available ? store.draft : null))

const markdown = computed(
  () => draft.value?.content_md ?? draft.value?.content_markdown ?? '',
)

const claims = computed<DraftClaim[]>(() => store.draftClaims)

const claimStats = computed(() => {
  const stats: Record<ClaimStatus, number> = { supported: 0, contradicted: 0, insufficient: 0 }
  for (const claim of claims.value) {
    if (claim.status in stats) stats[claim.status] += 1
  }
  const factual = claims.value.length
  return {
    ...stats,
    factual,
    coverage: factual > 0 ? stats.supported / factual : null,
  }
})

function claimText(claim: DraftClaim): string {
  return claim.claim_text ?? claim.text ?? ''
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function inline(text: string): string {
  let output = escapeHtml(text)
  output = output.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  output = output.replace(/`([^`]+)`/g, '<code>$1</code>')
  output = output.replace(/\[(\d+)\]/g, (_match, index: string) => `<button type="button" class="cite" data-cite="${index}">[${index}]</button>`)
  return output
}

/** 极简 Markdown：标题 / 无序列表 / 段落 + 行内加粗、代码、引用角标 */
function renderMarkdown(source: string): string {
  const lines = source.split(/\r?\n/)
  const output: string[] = []
  let inList = false
  const closeList = () => {
    if (inList) {
      output.push('</ul>')
      inList = false
    }
  }
  for (const line of lines) {
    if (/^\s*$/.test(line)) {
      closeList()
      continue
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      closeList()
      const level = heading[1]?.length ?? 1
      output.push(`<h${level}>${inline(heading[2] ?? '')}</h${level}>`)
      continue
    }
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line)
    if (bullet) {
      if (!inList) {
        output.push('<ul>')
        inList = true
      }
      output.push(`<li>${inline(bullet[1] ?? '')}</li>`)
      continue
    }
    closeList()
    output.push(`<p>${inline(line)}</p>`)
  }
  closeList()
  return output.join('\n')
}

/** Claim 高亮：insufficient 醒目，supported/contradicted 分别配色（文本匹配失败则不标，不编造） */
const renderedHtml = computed(() => {
  let html = renderMarkdown(markdown.value)
  const order: ClaimStatus[] = ['insufficient', 'contradicted', 'supported']
  for (const status of order) {
    for (const claim of claims.value.filter((item) => item.status === status)) {
      const text = claimText(claim)
      if (!text) continue
      const escaped = escapeHtml(text)
      if (!html.includes(escaped)) continue
      html = html
        .split(escaped)
        .join(`<mark class="claim claim--${status}" data-claim="${claim.id}">${escaped}</mark>`)
    }
  }
  return html
})

function onBodyClick(event: MouseEvent): void {
  const target = event.target as HTMLElement | null
  const cite = target?.dataset?.cite
  if (!cite) return
  const index = Number(cite)
  // 引用 [n] → 第 n 条 claim 的首个证据；证据类型/ID 由草稿接口给出，取不到即提示等待 WP13/WP14
  const ordered = claims.value[index - 1]
  const evidence = ordered?.evidence?.[0] as { evidence_type?: string; id?: number } | undefined
  if (!evidence || evidence.id === undefined) {
    store.clearEvidence()
    drawerOpen.value = true
    return
  }
  void store.loadEvidence(evidence.evidence_type ?? 'paper_span', evidence.id)
  drawerOpen.value = true
}

function openClaimEvidence(claim: DraftClaim): void {
  const evidence = claim.evidence?.[0] as { evidence_type?: string; id?: number } | undefined
  store.clearEvidence()
  if (evidence?.id !== undefined) {
    void store.loadEvidence(evidence.evidence_type ?? 'paper_span', evidence.id)
  }
  drawerOpen.value = true
}

function loadDraft(): void {
  if (draftId.value === undefined) return
  void store.loadDraft(draftId.value)
}

const artifacts = computed(() => {
  const list = draft.value?.artifacts ?? []
  return list.map((item) => ({
    name: item.name ?? item.kind ?? '未命名产物',
    kind: item.kind ?? '',
    url: item.url ?? '',
  }))
})

const codeArtifact = computed(() => artifacts.value.find((item) => item.kind === 'code'))
const experimentArtifact = computed(() => artifacts.value.find((item) => item.kind === 'experiment'))

const evidenceData = computed(() => (store.evidence?.available ? store.evidence.data : null))
const evidenceUnavailable = computed(() =>
  store.evidence && !store.evidence.available ? store.evidence : null,
)
</script>

<template>
  <div class="panel">
    <section class="selector">
      <strong>草稿 ID</strong>
      <el-input-number v-model="draftId" size="small" :min="1" :controls="false" class="id-input" />
      <el-button size="small" :disabled="draftId === undefined" :loading="store.draftLoading" @click="loadDraft">
        读取草稿
      </el-button>
    </section>

    <el-alert v-if="draftUnavailable" type="info" :closable="false" show-icon>
      <template #title>草稿数据未就绪：{{ draftUnavailable.waitingFor }}</template>
      <template #default>
        <p class="hint">{{ draftUnavailable.reason }}</p>
      </template>
    </el-alert>

    <template v-if="draft">
      <section class="card">
        <header class="card-head">
          <strong>{{ draft.title ?? '（草稿未命名）' }}</strong>
          <span class="badge">v{{ draft.version ?? '—' }}</span>
          <span class="badge">status {{ draft.status ?? '—' }}</span>
          <span v-if="draft.claim_coverage !== null && draft.claim_coverage !== undefined" class="badge badge--ok">
            claim_coverage {{ draft.claim_coverage }}
          </span>
          <span class="badge" :class="claimStats.coverage !== null && claimStats.coverage < 1 ? 'badge--warn' : 'badge--ok'">
            本地统计 supported/total = {{ claimStats.coverage === null ? '—' : claimStats.coverage.toFixed(2) }}
          </span>
        </header>

        <div class="claim-bar">
          <span class="claim-chip claim-chip--supported">supported {{ claimStats.supported }}</span>
          <span class="claim-chip claim-chip--contradicted">contradicted {{ claimStats.contradicted }}</span>
          <span class="claim-chip claim-chip--insufficient">insufficient {{ claimStats.insufficient }}</span>
          <span class="hint">共 {{ claimStats.factual }} 条事实性 Claim</span>
        </div>

        <div class="body" @click="onBodyClick">
          <p class="compliance">本内容由 AI 辅助生成，需研究者自行核验</p>
          <article class="markdown" v-html="renderedHtml" />
          <p v-if="!markdown" class="hint">接口未返回草稿正文（content_md 为空），不展示占位内容。</p>
        </div>
      </section>

      <section class="card">
        <header class="card-head">
          <strong>Claim 清单（点击查看证据）</strong>
        </header>
        <ul class="claim-list">
          <li v-for="claim in claims" :key="claim.id" class="claim-item" :class="`claim-item--${claim.status}`">
            <div class="claim-item-head">
              <span class="claim-chip" :class="`claim-chip--${claim.status}`">{{ CLAIM_LABELS[claim.status] }}</span>
              <span class="claim-id">#{{ claim.id }}</span>
              <span v-if="claim.section_name" class="hint">{{ claim.section_name }}</span>
              <el-button size="small" text @click="openClaimEvidence(claim)">查看证据</el-button>
            </div>
            <p class="claim-text">{{ claimText(claim) || '（接口未返回 claim 文本）' }}</p>
            <p v-if="claim.status === 'insufficient'" class="claim-warn">
              该句缺乏足够证据支撑，正式投稿前必须补齐或删除。
            </p>
            <p v-if="claim.note" class="hint">{{ claim.note }}</p>
          </li>
        </ul>
        <p v-if="claims.length === 0" class="hint">暂无 Claim 记录</p>
      </section>

      <section class="card">
        <header class="card-head">
          <strong>三件套与 Passport</strong>
          <span class="hint">论文草稿 / 代码 / 实验记录</span>
        </header>
        <div class="artifacts">
          <a class="artifact" :href="draftExportUrl(draft.id, 'md')" target="_blank" rel="noreferrer">
            论文草稿（Markdown）
          </a>
          <a class="artifact" :href="draftExportUrl(draft.id, 'pdf')" target="_blank" rel="noreferrer">
            论文草稿（PDF）
          </a>
          <a v-if="codeArtifact" class="artifact" :href="codeArtifact.url" target="_blank" rel="noreferrer">
            代码（{{ codeArtifact.name }}）
          </a>
          <span v-else class="artifact artifact--disabled" :title="`GET /drafts/{id}/export?format=md|pdf`">
            代码产物未提供（等待 WP11/WP14 的 artifact_manifest）
          </span>
          <a v-if="experimentArtifact" class="artifact" :href="experimentArtifact.url" target="_blank" rel="noreferrer">
            实验记录（{{ experimentArtifact.name }}）
          </a>
          <span v-else class="artifact artifact--disabled">实验记录未提供（等待 WP14 提供产物清单）</span>
          <el-button size="small" @click="emit('goto-passport', store.passportRunId)">
            打开 Passport
          </el-button>
        </div>
      </section>

      <section v-if="artifacts.length" class="card">
        <header class="card-head"><strong>产物清单（artifact_manifest）</strong></header>
        <ul class="artifact-list">
          <li v-for="artifact in artifacts" :key="artifact.name + artifact.url">
            <span class="mono">{{ artifact.kind || '—' }}</span>
            <span>{{ artifact.name }}</span>
            <a v-if="artifact.url" :href="artifact.url" target="_blank" rel="noreferrer">{{ artifact.url }}</a>
          </li>
        </ul>
      </section>
    </template>

    <!-- 证据抽屉 -->
    <el-drawer v-model="drawerOpen" title="证据详情" size="480px">
      <template v-if="evidenceData">
        <el-descriptions :column="1" size="small" border>
          <el-descriptions-item label="evidence_type">
            {{ evidenceData.evidence_type ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="校验结论">
            <span class="verdict" :class="evidenceData.verdict === 'invalid' ? 'verdict--bad' : 'verdict--ok'">
              {{ evidenceData.verdict ?? '—' }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="quote_text">
            <blockquote class="quote">{{ evidenceData.quote_text ?? '—' }}</blockquote>
          </el-descriptions-item>
          <el-descriptions-item label="quote_sha256">
            <code class="mono">{{ evidenceData.quote_sha256 ?? '—' }}</code>
          </el-descriptions-item>
          <el-descriptions-item label="定位">
            {{ evidenceData.section_name ?? '—' }} · 第 {{ evidenceData.page_number ?? '—' }} 页 ·
            char[{{ evidenceData.char_start ?? '—' }}, {{ evidenceData.char_end ?? '—' }}] ·
            document_version {{ evidenceData.document_version ?? '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="覆盖度">
            {{ evidenceData.coverage === null || evidenceData.coverage === undefined ? '—' : `${evidenceData.coverage}` }}
          </el-descriptions-item>
        </el-descriptions>
      </template>
      <el-alert v-else-if="evidenceUnavailable" type="info" :closable="false" show-icon>
        <template #title>{{ WP13_WAITING }}</template>
        <template #default>
          <p class="hint">{{ evidenceUnavailable.reason }}</p>
        </template>
      </el-alert>
    </el-drawer>

    <el-empty v-if="!draft && !draftUnavailable && store.draft === null" description="尚未读取草稿：请输入草稿 ID 后点击「读取草稿」" />
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.selector {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-sm);
}

.id-input {
  width: 120px;
}

.card {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
  font-size: var(--font-size-sm);
}

.badge {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.badge--ok {
  border-color: var(--color-success);
  color: var(--color-success);
}

.badge--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.claim-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.claim-chip {
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.claim-chip--supported {
  border-color: var(--color-claim-supported);
  color: var(--color-claim-supported);
}

.claim-chip--contradicted {
  border-color: var(--color-claim-contradicted);
  color: var(--color-claim-contradicted);
}

.claim-chip--insufficient {
  border-color: var(--color-claim-insufficient);
  color: var(--color-claim-insufficient);
}

.compliance {
  margin: 0 0 var(--space-2);
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-subtle);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.markdown {
  font-size: var(--font-size-md);
  color: var(--color-text-primary);
  line-height: var(--line-height-base);
}

.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3),
.markdown :deep(h4) {
  margin: var(--space-3) 0 var(--space-2);
  line-height: var(--line-height-tight);
}

.markdown :deep(p) {
  margin: 0 0 var(--space-2);
}

.markdown :deep(code) {
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-muted);
  font-family: var(--font-family-mono);
}

.markdown :deep(.cite) {
  border: 1px solid var(--color-brand);
  border-radius: var(--radius-sm);
  background-color: var(--color-brand-soft);
  color: var(--color-brand);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
  cursor: pointer;
  padding: 0 2px;
}

.markdown :deep(.claim--insufficient) {
  display: inline;
  padding: 0 2px;
  border-bottom: 2px solid var(--color-claim-insufficient);
  background-color: var(--color-warning-soft);
  color: var(--color-text-primary);
}

.markdown :deep(.claim--contradicted) {
  border-bottom: 2px solid var(--color-claim-contradicted);
  background-color: var(--color-danger-soft);
}

.markdown :deep(.claim--supported) {
  border-bottom: 2px solid var(--color-claim-supported);
}

.claim-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.claim-item {
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-left-width: 4px;
  border-radius: var(--radius-sm);
  background-color: var(--color-bg-subtle);
}

.claim-item--supported {
  border-left-color: var(--color-claim-supported);
}

.claim-item--contradicted {
  border-left-color: var(--color-claim-contradicted);
}

.claim-item--insufficient {
  border-left-color: var(--color-claim-insufficient);
  background-color: var(--color-warning-soft);
}

.claim-item-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.claim-id {
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.claim-text {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.claim-warn {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-warning);
}

.artifacts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.artifact {
  padding: var(--space-1) var(--space-3);
  border: 1px solid var(--color-brand);
  border-radius: var(--radius-md);
  color: var(--color-brand);
  font-size: var(--font-size-xs);
}

.artifact--disabled {
  border-color: var(--color-border);
  color: var(--color-text-disabled);
}

.artifact-list {
  list-style: none;
  margin: 0;
  padding: 0;
  font-size: var(--font-size-xs);
}

.artifact-list li {
  display: flex;
  gap: var(--space-2);
  padding: 2px 0;
  border-bottom: 1px dashed var(--color-border);
}

.verdict--ok {
  color: var(--color-success);
}

.verdict--bad {
  color: var(--color-danger);
}

.quote {
  margin: 0;
  padding-left: var(--space-2);
  border-left: 3px solid var(--color-border-strong);
  color: var(--color-text-primary);
}

.mono {
  font-family: var(--font-family-mono);
}
</style>
