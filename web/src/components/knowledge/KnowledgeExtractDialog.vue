<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 从**论文库**或**研究构思**提取信息加入知识库。
 *
 * 只读原件、不写入原件：左侧选一条，右侧勾选要带走的内容（论文按解析卡片的 8 字段；
 * idea 取标题 / 内容 / 生成机制 / 新颖性说明），确认后生成一个 **`.md` 文件**，
 * 并留下指回原件的链接（`source_route`），知识库里永远看得到"这是从哪来的"。
 *
 * 没有解析卡片时**不编造字段**：如实说明该论文暂无卡片，只带标题与摘要。
 */
import { computed, ref, watch } from 'vue'

import { createEntry, type KnowledgeDraft, type KnowledgeEntry } from '@/api/knowledge'
import { listIdeas, type Idea } from '@/api/idea'
import { CARD_FIELDS, fetchCard, type CardEntry, type CardResponse } from '@/api/parse'
import { searchPapers, type PaperSearchItem } from '@/api/papers'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

const props = defineProps<{
  modelValue: boolean
  /** 提取来源 */
  source: 'paper' | 'idea'
  tagPool: string[]
  projects: Array<{ id: number; name: string }>
  currentFolder: string[]
  canWrite: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'added', entry: KnowledgeEntry): void
}>()

const keyword = ref('')
const loading = ref(false)
const listError = ref('')
const papers = ref<PaperSearchItem[]>([])
const ideas = ref<Idea[]>([])
const activePaperId = ref<number | null>(null)
const activeIdeaId = ref<number | null>(null)

const card = ref<CardResponse | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
/** 该论文还没有解析卡片（不是错误，是一种正常状态：只带标题与摘要即可） */
const cardMissing = ref(false)

const pickedFields = ref<string[]>([])
const tags = ref<string[]>([])
const projectId = ref<number | null>(null)
const saving = ref(false)

const sourceLabel = computed(() => (props.source === 'paper' ? '从论文库提取' : '从研究构思提取'))

const activePaper = computed(() => papers.value.find((item) => item.id === activePaperId.value) ?? null)
const activeIdea = computed(() => ideas.value.find((item) => item.id === activeIdeaId.value) ?? null)

/** 字段摘要：数组取要点、实验设置取三元组，与知识资产页同一口径 */
function summarize(value: unknown): string {
  if (Array.isArray(value)) {
    return (value as CardEntry[])
      .map((entry) => entry.point ?? entry.conclusion ?? entry.limitation ?? entry.step ?? entry.description ?? '')
      .filter(Boolean)
      .join('；')
  }
  if (value && typeof value === 'object') {
    const setup = value as { metrics?: string[]; datasets?: string[]; baselines?: string[] }
    const parts: string[] = []
    if (setup.metrics?.length) parts.push(`指标 ${setup.metrics.join('/')}`)
    if (setup.datasets?.length) parts.push(`数据集 ${setup.datasets.join('/')}`)
    if (setup.baselines?.length) parts.push(`基线 ${setup.baselines.join('/')}`)
    return parts.join(' · ')
  }
  return value === null || value === undefined ? '' : String(value)
}

/** 卡片字段的可勾选项（只做展示与挑选，不落库） */
interface FieldOption {
  key: string
  label: string
  value: string
}

const cardOptions = computed<FieldOption[]>(() => {
  const content = card.value?.card as Record<string, unknown> | undefined
  if (!content) return []
  return CARD_FIELDS.map((def) => ({
    key: def.key as string,
    label: def.label,
    value: summarize(content[def.key as string]),
  })).filter((field) => field.value.trim().length > 0)
})

async function runSearch(): Promise<void> {
  loading.value = true
  listError.value = ''
  try {
    const payload = await searchPapers({
      q: keyword.value.trim(),
      sort: 'published',
      pageSize: 12,
    })
    papers.value = payload.items ?? []
    if (papers.value.length && activePaperId.value === null) {
      await pickPaper(papers.value[0]!.id)
    }
  } catch (error) {
    papers.value = []
    listError.value = (error as { message?: string }).message ?? '论文库读取失败'
  } finally {
    loading.value = false
  }
}

async function loadIdeas(): Promise<void> {
  loading.value = true
  listError.value = ''
  try {
    const payload = await listIdeas({ pageSize: 20 })
    ideas.value = payload.items ?? []
    if (ideas.value.length && activeIdeaId.value === null) {
      activeIdeaId.value = ideas.value[0]!.idea_id
    }
  } catch (error) {
    ideas.value = []
    listError.value = (error as { message?: string }).message ?? '研究构思读取失败'
  } finally {
    loading.value = false
  }
}

async function pickPaper(id: number): Promise<void> {
  activePaperId.value = id
  card.value = null
  detailError.value = ''
  cardMissing.value = false
  pickedFields.value = []
  detailLoading.value = true
  try {
    card.value = await fetchCard(id)
    pickedFields.value = cardOptions.value.map((field) => field.key)
  } catch {
    cardMissing.value = true
  } finally {
    detailLoading.value = false
  }
}

/** 列表副行只列真实存在的项，缺的一律不占位（避免成片的「未标注」） */
function paperMeta(paper: PaperSearchItem): string {
  const parts = [`#${paper.id}`]
  if (paper.published_at) parts.push(paper.published_at.slice(0, 10))
  if (paper.venue) parts.push(paper.venue)
  if (paper.citation_count !== null && paper.citation_count !== undefined) {
    parts.push(`引用 ${paper.citation_count}`)
  }
  return parts.join(' · ')
}

function ideaMeta(idea: Idea): string {
  const parts = [`idea #${idea.idea_id}`, idea.origin === 'ai_generated' ? '模型生成' : '手工录入']
  if (idea.evidence_count) parts.push(`证据 ${idea.evidence_count}`)
  return parts.join(' · ')
}

/** 文件名里不能出现的字符换成下划线，并补上 .md（知识库条目一律是带扩展名的文件） */
function mdName(title: string): string {
  return `${title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 80)}.md`
}

async function submit(): Promise<void> {
  if (!props.canWrite || saving.value) return
  saving.value = true

  let draft: KnowledgeDraft
  if (props.source === 'paper') {
    const paper = activePaper.value
    if (!paper) {
      saving.value = false
      return
    }
    const lines: string[] = [`# ${paper.title}`, '']
    for (const field of cardOptions.value.filter((item) => pickedFields.value.includes(item.key))) {
      lines.push(`## ${field.label}`, '', field.value, '')
    }
    if (paper.abstract?.trim()) lines.push('## 摘要', '', paper.abstract.trim(), '')
    lines.push(`> 摘录自论文库 #${paper.id} 的解析卡片。`)
    const content = lines.join('\n')
    draft = {
      name: mdName(paper.title),
      bucket: 'literature',
      content,
      size: content.length,
      folder: [...props.currentFolder],
      tags: tags.value,
      project_id: projectId.value,
      source_label: `论文库 · #${paper.id}`,
      source_route: `/papers/parse/${paper.id}`,
      file_url: null,
    }
  } else {
    const idea = activeIdea.value
    if (!idea) {
      saving.value = false
      return
    }
    const lines: string[] = [`# ${idea.title}`, '', idea.content]
    if (idea.novelty_note?.trim()) lines.push('', `## 新颖性`, '', idea.novelty_note.trim())
    if (idea.mechanism) lines.push('', `- 生成机制：${idea.mechanism}`)
    lines.push(`- 证据：${idea.evidence_count} 条`, '', `> 摘录自研究构思 idea #${idea.idea_id}。`)
    const content = lines.join('\n')
    draft = {
      name: mdName(idea.title),
      bucket: 'idea',
      content,
      size: content.length,
      folder: [...props.currentFolder],
      tags: tags.value,
      project_id: projectId.value,
      source_label: `研究构思 · idea #${idea.idea_id}`,
      source_route: '/ideas',
      file_url: null,
    }
  }

  try {
    const saved = await createEntry(draft)
    emit('added', saved)
    close()
  } catch (error) {
    console.error('[knowledge] 加入知识库失败', error)
    detailError.value = '加入知识库失败，请重试'
  } finally {
    saving.value = false
  }
}

function close(): void {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    keyword.value = ''
    papers.value = []
    ideas.value = []
    activePaperId.value = null
    activeIdeaId.value = null
    card.value = null
    detailError.value = ''
    cardMissing.value = false
    pickedFields.value = []
    tags.value = []
    projectId.value = session.currentProjectId
    if (props.source === 'paper') void runSearch()
    else void loadIdeas()
  },
)
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="kbe-overlay" @click.self="close">
      <section class="kbe" role="dialog" aria-modal="true" :aria-label="sourceLabel">
        <header class="kbe__head">
          <h2 class="kbe__title">{{ sourceLabel }}</h2>
          <button class="kbe__close" type="button" aria-label="关闭" @click="close">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
              <path d="M3.5 3.5l7 7M10.5 3.5l-7 7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
        </header>

        <div class="kbe__bar">
          <template v-if="source === 'paper'">
            <el-input
              v-model="keyword"
              size="small"
              class="kbe__search"
              placeholder="在论文库里搜标题 / 摘要"
              clearable
              @keyup.enter="runSearch"
            />
            <el-button size="small" :loading="loading" @click="runSearch">检索</el-button>
          </template>
          <template v-else>
            <el-button size="small" :loading="loading" @click="loadIdeas">刷新列表</el-button>
          </template>
          <span class="kbe__count">
            {{ source === 'paper' ? `${papers.length} 篇` : `${ideas.length} 条` }}
          </span>
        </div>

        <div class="kbe__split">
          <ul class="kbe__list scroll-y">
            <li v-if="listError" class="kbe__list-error">{{ listError }}</li>
            <template v-else-if="source === 'paper'">
              <li
                v-for="paper in papers"
                :key="paper.id"
                class="kbe__pick"
                :class="{ 'kbe__pick--on': paper.id === activePaperId }"
                @click="pickPaper(paper.id)"
              >
                <p class="kbe__pick-title">{{ paper.title }}</p>
                <p class="kbe__pick-meta">{{ paperMeta(paper) }}</p>
              </li>
            </template>
            <template v-else>
              <li
                v-for="idea in ideas"
                :key="idea.idea_id"
                class="kbe__pick"
                :class="{ 'kbe__pick--on': idea.idea_id === activeIdeaId }"
                @click="activeIdeaId = idea.idea_id"
              >
                <p class="kbe__pick-title">{{ idea.title }}</p>
                <p class="kbe__pick-meta">{{ ideaMeta(idea) }}</p>
              </li>
            </template>
            <li v-if="!listError && !loading && !papers.length && !ideas.length" class="kbe__empty">
              <el-empty description="没有可提取的内容" />
            </li>
          </ul>

          <div class="kbe__detail scroll-y">
            <el-skeleton v-if="detailLoading" :rows="5" animated />

            <template v-else-if="source === 'paper'">
              <template v-if="activePaper">
                <h3 class="kbe__detail-title">{{ activePaper.title }}</h3>

                <div v-if="cardOptions.length" class="kbe__fields">
                  <p class="kbe__block-label">要带走的内容</p>
                  <el-checkbox-group v-model="pickedFields" class="kbe__checks">
                    <el-checkbox v-for="field in cardOptions" :key="field.key" :value="field.key">
                      {{ field.label }}
                    </el-checkbox>
                  </el-checkbox-group>
                </div>
                <p v-else-if="cardMissing" class="kbe__hint-line">
                  这篇论文还没有解析卡片
                  <RouterLink class="kbe__link" :to="`/papers/parse/${activePaper.id}`">去建卡</RouterLink>
                  ，现在带走的是标题与摘要。
                </p>
              </template>
              <el-empty v-else description="左侧选一篇论文" />
            </template>

            <template v-else>
              <template v-if="activeIdea">
                <h3 class="kbe__detail-title">{{ activeIdea.title }}</h3>
                <p v-if="activeIdea.mechanism" class="kbe__detail-meta">
                  生成机制 {{ activeIdea.mechanism }} · 证据 {{ activeIdea.evidence_count }} 条
                </p>
                <p class="kbe__detail-body">{{ activeIdea.content }}</p>
                <p v-if="activeIdea.novelty_note" class="kbe__detail-body">{{ activeIdea.novelty_note }}</p>
              </template>
              <el-empty v-else description="左侧选一个方案" />
            </template>

            <div v-if="activePaper || activeIdea" class="kbe__form">
              <div class="kbe__form-row">
                <span class="kbe__block-label">标签</span>
                <el-select
                  v-model="tags"
                  size="small"
                  class="kbe__form-control"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  placeholder="回车确认，可直接新建"
                >
                  <el-option v-for="tag in tagPool" :key="tag" :label="tag" :value="tag" />
                </el-select>
              </div>
              <div class="kbe__form-row">
                <span class="kbe__block-label">归属项目</span>
                <el-select
                  v-model="projectId"
                  size="small"
                  class="kbe__form-control"
                  clearable
                  placeholder="不归属任何项目"
                >
                  <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
                </el-select>
              </div>
            </div>
          </div>
        </div>

        <footer class="kbe__foot">
          <button class="kbe__btn" type="button" @click="close">取消</button>
          <button
            class="kbe__btn kbe__btn--primary"
            type="button"
            :disabled="!canWrite || saving || !(activePaper || activeIdea)"
            @click="submit"
          >
            {{ saving ? '加入中…' : '加入知识库' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.kbe-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-5);
  background: rgba(0, 0, 0, 0.35); /* ui-polish-allow: 遮罩色与主题解耦 */
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .kbe-overlay {
  background: rgba(0, 0, 0, 0.55); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.kbe {
  display: flex;
  flex-direction: column;
  width: min(900px, 100%);
  height: min(700px, 100%);
  overflow: hidden;
  background: var(--color-card-bg);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.kbe__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.kbe__title {
  margin: 0;
  flex: 1;
  font-size: var(--font-size-lg);
}

.kbe__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition:
    color var(--motion-dur-fast) var(--motion-ease),
    border-color var(--motion-dur-fast) var(--motion-ease);
}

.kbe__close:hover {
  border-color: var(--color-border);
  color: var(--color-text-primary);
}

.kbe__close:active {
  transform: scale(var(--motion-press));
}

.kbe__close:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbe__bar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--color-border);
}

.kbe__search {
  width: 320px;
}

.kbe__count {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbe__split {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(0, 1.2fr);
  flex: 1;
  min-height: 0;
}

.kbe__list {
  margin: 0;
  padding: var(--space-2);
  list-style: none;
  border-right: 1px solid var(--color-border);
  overflow-y: auto;
}

.kbe__list-error {
  padding: var(--space-3);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}

.kbe__pick {
  padding: var(--space-2);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition:
    background-color var(--motion-dur-fast) var(--motion-ease),
    border-color var(--motion-dur-fast) var(--motion-ease);
}

.kbe__pick:hover {
  background-color: var(--color-bg-subtle);
}

.kbe__pick--on {
  border-color: var(--color-brand);
  background-color: var(--color-brand-soft);
}

.kbe__pick-title {
  margin: 0;
  font-size: var(--font-size-sm);
}

.kbe__pick-meta {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbe__empty {
  list-style: none;
}

.kbe__detail {
  padding: var(--space-3) var(--space-4);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.kbe__detail-title {
  margin: 0;
  font-size: var(--font-size-md);
}

.kbe__detail-meta,
.kbe__hint-line {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbe__hint-line {
  color: var(--color-warning);
}

.kbe__link {
  color: var(--color-brand);
  transition: filter var(--motion-dur-fast) var(--motion-ease);
}

.kbe__link:hover {
  filter: brightness(1.1);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.kbe__link:active {
  filter: brightness(0.95);
}

.kbe__link:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbe__detail-body {
  margin: 0;
  font-size: var(--font-size-sm);
  line-height: var(--line-height-base);
  white-space: pre-wrap;
}

.kbe__block-label {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kbe__checks {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-2);
}

.kbe__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: auto;
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

.kbe__form-row {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  align-items: center;
  gap: var(--space-2);
}

.kbe__form-control {
  min-width: 0;
}

.kbe__foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-border);
}

.kbe__btn {
  height: 30px;
  padding: 0 var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease);
}

.kbe__btn:hover {
  border-color: var(--color-border-strong);
  color: var(--color-text-primary);
}

.kbe__btn:active {
  transform: scale(var(--motion-press));
}

.kbe__btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kbe__btn--primary {
  border-color: var(--color-brand);
  background-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.kbe__btn--primary:hover {
  border-color: var(--color-brand-hover);
  background-color: var(--color-brand-hover);
  color: var(--color-text-inverse);
}

.kbe__btn--primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 900px) {
  .kbe__split {
    grid-template-columns: 1fr;
  }

  .kbe__list {
    border-right: none;
    border-bottom: 1px solid var(--color-border);
    max-height: 200px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .kbe,
  .kbe__close,
  .kbe__pick,
  .kbe__btn {
    transition: none;
    animation: none;
  }
}
</style>
