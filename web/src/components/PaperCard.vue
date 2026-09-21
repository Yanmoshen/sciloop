<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 论文卡片（WP07-T2）：标题 / 作者 / 时间 / venue（带 venue_source 角标）/ 默认分
 * + 可展开的排序四维与影响力三项 + llm_novelty 区间 + CoverageTag + 来源徽标。
 *
 * 诚实展示纪律：
 * - 缺项一律显示「未获取」（附原因），**禁止显示 0 分**；
 * - 来源徽标只依据真实取数留痕（feed 载荷字段 + GET /papers/{id}/sources），**不臆测来源**；
 * - llm_novelty stable=false 时只展示区间并标 unstable。
 */
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'

import InfluenceBreakdown from '@/components/InfluenceBreakdown.vue'
import CoverageTag from '@/components/CoverageTag.vue'
import { ApiError } from '@/api/client'
import { fetchPaperSources, type FeedItem, type FeedViewName } from '@/api/feed'
import { codeRepoLink, paperSourceLink } from '@/utils/paperLink'
import { RANK_DIMENSION_TEXT } from '@/utils/messages'

const props = withDefaults(
  defineProps<{
    item: FeedItem
    /** 当前视图：决定「当前排序依据」展示哪一列 */
    activeView: FeedViewName
    rankWeights?: Record<string, number>
    influenceWeights?: Record<string, number>
    index?: number
  }>(),
  { rankWeights: () => ({}), influenceWeights: () => ({}), index: 0 },
)

const expanded = ref(false)

const authors = computed(() => {
  const list = (props.item.authors ?? []).filter(
    (author): author is { name?: string | null } => typeof author === 'object' && author !== null,
  )
  const names = list.map((author) => author.name).filter((name): name is string => Boolean(name))
  return { names, hidden: Math.max(names.length - 3, 0) }
})

const publishedLabel = computed(() => {
  if (!props.item.published_at) return '发布时间未获取'
  const parsed = new Date(props.item.published_at)
  if (Number.isNaN(parsed.getTime())) return props.item.published_at
  return parsed.toLocaleDateString()
})

const venueLabel = computed(() => props.item.venue ?? 'venue 未获取')

/** 当前排序依据的取值（latest 视图按时间，不显示分数作为排序依据） */
const sortValue = computed(() => {
  switch (props.activeView) {
    case 'influence':
      return { label: '影响力分（辅助）', value: formatScore(props.item.influence_score) }
    case 'latest':
      return { label: '发表时间（倒序）', value: props.item.published_at ?? '未获取' }
    default:
      return { label: '推荐排序分（四项加权）', value: formatScore(props.item.rank_score) }
  }
})

function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '未获取'
  return String(Math.round(value * 100) / 100)
}

/** 来源链接（arXiv 摘要页 / DOI / PDF）与代码仓库链接：都没有时该行不渲染，不打印占位 */
const sourceLink = computed(() => paperSourceLink(props.item))
const codeLink = computed(() => codeRepoLink(props.item))

/** 展开区：真实取数留痕（懒加载，避免首屏 N 次请求） */
const sources = ref<Record<string, { label: string; http: number | null; fields: number; ok: boolean }>>(
  {},
)
const sourcesLoading = ref(false)
const sourcesError = ref<string | null>(null)
const sourcesLoaded = ref(false)

const sourceRows = computed(() => Object.entries(sources.value))

async function loadSources(): Promise<void> {
  if (sourcesLoaded.value || sourcesLoading.value) return
  sourcesLoading.value = true
  sourcesError.value = null
  try {
    const response = await fetchPaperSources(props.item.id, { page_size: 200 })
    const grouped: Record<
      string,
      { label: string; http: number | null; fields: number; ok: boolean }
    > = {}
    response.items.forEach((record) => {
      const key = record.source
      const entry = grouped[key] ?? {
        label: record.source_label ?? record.source,
        http: record.http_status,
        fields: 0,
        ok: true,
      }
      entry.fields += 1
      entry.http = record.http_status ?? entry.http
      if (record.http_status !== null && record.http_status >= 400) entry.ok = false
      grouped[key] = entry
    })
    sources.value = grouped
    sourcesLoaded.value = true
  } catch (error) {
    sourcesError.value = error instanceof ApiError ? error.message : (error as Error).message
  } finally {
    sourcesLoading.value = false
  }
}

function toggle(): void {
  expanded.value = !expanded.value
  if (expanded.value) void loadSources()
}

defineExpose({ toggle })
</script>

<template>
  <article class="paper-card sl-card" :data-paper-id="item.id">
    <header class="paper-card__head">
      <span class="paper-card__index">#{{ index + 1 }}</span>
      <h3 class="paper-card__title">
        <RouterLink :to="`/papers/parse/${item.id}`" :title="item.title">{{ item.title }}</RouterLink>
      </h3>
      <span class="paper-card__coverage">
        <CoverageTag
          :parse-status="item.fulltext?.parse_status ?? null"
          :coverage="item.fulltext?.coverage ?? null"
          :scope="null"
          :source="item.fulltext?.source ?? null"
          size="small"
        />
      </span>
    </header>

    <p class="paper-card__abstract">{{ item.abstract ?? '摘要未获取' }}</p>

    <dl class="paper-card__meta">
      <div class="meta-item">
        <dt>作者</dt>
        <dd>
          <template v-if="authors.names.length">
            {{ authors.names.slice(0, 3).join('、')
            }}<span v-if="authors.hidden > 0"> 等 {{ authors.hidden }} 人</span>
          </template>
          <span v-else class="missing">未获取</span>
        </dd>
      </div>
      <div class="meta-item">
        <dt>时间</dt>
        <dd>{{ publishedLabel }}</dd>
      </div>
      <div class="meta-item">
        <dt>venue</dt>
        <dd>
          <template v-if="item.venue">
            {{ venueLabel }}
            <span class="venue-source" :title="`venue_source=${item.venue_source ?? '未获取'}`">
              {{ item.venue_source ? `来源 ${item.venue_source}` : 'venue 来源未获取' }}
            </span>
          </template>
          <span v-else class="missing">未获取</span>
        </dd>
      </div>
      <div class="meta-item">
        <dt>venue 等级</dt>
        <dd>
          <span v-if="item.venue_level === null || item.venue_level === undefined" class="missing">
            未获取
          </span>
          <span v-else>{{ item.venue_level }} 级（venue_level）</span>
        </dd>
      </div>
      <div class="meta-item">
        <dt>引用数</dt>
        <dd>
          <span v-if="item.citation_count === null || item.citation_count === undefined" class="missing">
            未获取
          </span>
          <span v-else>{{ item.citation_count }}</span>
        </dd>
      </div>
      <div v-if="sourceLink || codeLink" class="meta-item">
        <dt>链接</dt>
        <dd>
          <a
            v-if="sourceLink"
            class="meta-link"
            :href="sourceLink.href"
            target="_blank"
            rel="noreferrer noopener"
          >{{ sourceLink.text }}</a>
          <template v-if="sourceLink && codeLink"> · </template>
          <a
            v-if="codeLink"
            class="meta-link"
            :href="codeLink.href"
            target="_blank"
            rel="noreferrer noopener"
          >{{ codeLink.text }}</a>
        </dd>
      </div>
    </dl>

    <div class="paper-card__scores">
      <span class="score-box">
        <small>当前排序依据 · {{ sortValue.label }}</small>
        <strong>{{ sortValue.value }}</strong>
      </span>
      <span class="score-box" :title="`推荐理由：${RANK_DIMENSION_TEXT}（逐维明细见展开区）`">
        <small>推荐理由 · 四项加权</small>
        <strong :class="{ missing: item.rank_score === null }">{{ formatScore(item.rank_score) }}</strong>
      </span>
      <span class="score-box score-box--aux" title="辅助分：只在「影响力」视图用于排序，不影响默认推荐">
        <small>影响力分（辅助）</small>
        <strong :class="{ missing: item.influence_score === null }">
          {{ formatScore(item.influence_score) }}
        </strong>
      </span>
      <span class="score-box" title="该论文在四个维度上「有真实取数」的比例；缺数据的维度不参与推断">
        <small>数据完整度</small>
        <strong :class="{ missing: item.score_coverage === null }">
          {{ item.score_coverage === null ? '未获取' : item.score_coverage }}
        </strong>
      </span>
      <el-button size="small" text type="primary" class="paper-card__toggle" @click="toggle">
        {{ expanded ? '收起分项' : '展开排序四维与影响力三项' }}
      </el-button>
    </div>

    <div class="fold" :class="{ 'fold--open': expanded }">
      <section class="paper-card__detail">
      <InfluenceBreakdown
        :rank-breakdown="item.rank_breakdown ?? {}"
        :influence-breakdown="item.score_breakdown ?? {}"
        :rank-weights="rankWeights"
        :influence-weights="influenceWeights"
        :rank-score="item.rank_score"
        :influence-score="item.influence_score"
        :score-coverage="item.score_coverage"
        :influence-coverage="item.influence_coverage"
        :novelty="item.llm_novelty_tag ?? null"
      />

      <div class="paper-card__extra">
        <div>
          <p class="extra-title">展示型元数据（不参与任何分数）</p>
          <p class="extra-body sl-source-tag">
            机构分：{{
              item.display_metadata?.institution_score === null ||
              item.display_metadata?.institution_score === undefined
                ? '未获取'
                : item.display_metadata.institution_score
            }}
            ；作者机构：{{
              item.display_metadata?.affiliations?.length
                ? item.display_metadata.affiliations.join('、')
                : '未获取'
            }}
          </p>
          <p class="extra-body sl-source-tag">
            {{ item.display_metadata?.note ?? '机构分与 LLM 新颖性分不参与任何排序或评分' }}
          </p>
        </div>

        <div>
          <p class="extra-title">取数留痕（GET /papers/{{ item.id }}/sources）</p>
          <el-skeleton v-if="sourcesLoading" :rows="2" animated />
          <p v-else-if="sourcesError" class="extra-body extra-body--error">
            取数留痕加载失败：{{ sourcesError }}（缺失不等于 0，故不显示数值）
          </p>
          <ul v-else-if="sourceRows.length" class="source-list">
            <li v-for="row in sourceRows" :key="`row-${row[0]}`">
              <span class="sl-source-tag">
                {{ row[1].label }} · {{ row[1].fields }} 条字段留痕 · last_http_status={{
                  row[1].http ?? '未获取'
                }}
              </span>
            </li>
          </ul>
          <p v-else class="extra-body">该论文无取数留痕记录（未获取，不推断来源）</p>
        </div>
      </div>
      </section>
    </div>
  </article>
</template>

<style scoped>
.paper-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
}

.paper-card__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.paper-card__index {
  color: var(--color-text-secondary);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
}

.paper-card__title {
  flex: 1;
  margin: 0;
  font-size: var(--font-size-lg);
  line-height: var(--line-height-tight);
}

.paper-card__coverage {
  flex: 0 0 auto;
}

.paper-card__abstract {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.paper-card__meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-1) var(--space-4);
  margin: 0;
  font-size: var(--font-size-xs);
}

.meta-item {
  display: flex;
  gap: var(--space-2);
}

.meta-item dt {
  flex: 0 0 56px;
  color: var(--color-text-secondary);
}

.meta-item dd {
  margin: 0;
  min-width: 0;
}

.meta-link {
  color: var(--color-brand);
  word-break: break-all;
}

.meta-link:hover {
  text-decoration: underline;
}

.venue-source {
  margin-left: var(--space-1);
  padding: 0 var(--space-1);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--font-size-2xs);
}

.missing {
  color: var(--color-warning);
}

.paper-card__scores {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  align-items: center;
}

.score-box {
  display: inline-flex;
  flex-direction: column;
  padding: var(--space-1) var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background-color: var(--color-bg-subtle);
}

.score-box small {
  color: var(--color-text-secondary);
  font-size: var(--font-size-2xs);
}

.score-box strong {
  font-family: var(--font-family-mono);
  font-size: var(--font-size-md);
}

.score-box--aux strong {
  color: var(--color-warning);
}

.paper-card__toggle {
  margin-left: auto;
}

.paper-card__detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
}

.paper-card__extra {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-3);
}

.extra-title {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.extra-body {
  margin: 0;
  font-size: var(--font-size-xs);
}

.extra-body--error {
  color: var(--color-danger);
}

.source-list {
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--font-size-xs);
}
</style>
