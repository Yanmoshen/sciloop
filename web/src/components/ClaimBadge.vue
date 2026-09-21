<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Claim 三态徽标（WP13-T6，contracts.design_tokens.claim_status_colors）。
 *
 * 三态映射（颜色全部走 styles/tokens.css 变量，**组件内零硬编码色值**）：
 *   supported    → var(--color-claim-supported)    成功色（有有效证据且无冲突）
 *   contradicted → var(--color-claim-contradicted) 危险色（引证与实际证据冲突）
 *   insufficient → var(--color-claim-insufficient) 警告色（无有效证据 / 定位失效）
 *
 * 点击行为：向上抛 `select` 事件，由 WP15 的 DraftViewer 打开证据抽屉。
 * 徽标自身不请求任何接口（分层约定：请求一律经 src/api/*.ts）。
 */
import { computed } from 'vue'

import type { ClaimSelectPayload, ClaimStatus } from '@/api/claims'

const props = withDefaults(
  defineProps<{
    /** 三态之一；未知/缺失时按 insufficient 兜底并如实标注 */
    status: ClaimStatus | string | null | undefined
    /** draft_claims.id，随 select 事件回传给父组件 */
    claimId?: number | null
    /** 已绑定证据数（evidence_count） */
    evidenceCount?: number | null
    /** 判定理由（status_reason），作为 tooltip 正文 */
    reason?: string | null
    /** 是否事实性 Claim；false 表示不参与 claim_coverage 分母 */
    factual?: boolean
    /** 段落定位（content_md 原文偏移），供父组件高亮 */
    charStart?: number | null
    charEnd?: number | null
    /** 附加证据引用（点击后父组件据此打开抽屉） */
    evidenceIds?: number[] | null
    size?: 'small' | 'default'
    /** false 时只读展示，不抛 select 事件 */
    clickable?: boolean
  }>(),
  {
    claimId: null,
    evidenceCount: null,
    reason: null,
    factual: true,
    charStart: null,
    charEnd: null,
    evidenceIds: null,
    size: 'default',
    clickable: true,
  },
)

const emit = defineEmits<{
  /** 点击徽标：父组件据此打开证据抽屉（payload 与 draft_claims 行字段一致） */
  (event: 'select', payload: ClaimSelectPayload): void
}>()

const STATUS_META: Record<string, { label: string; tone: string; note: string }> = {
  supported: {
    label: '有证据支撑',
    tone: 'supported',
    note: 'supported：引用的证据全部通过来源/定位/哈希校验（哈希优先于偏移）',
  },
  contradicted: {
    label: '与证据冲突',
    tone: 'contradicted',
    note: 'contradicted：引用了证据但校验失败，或冲突判定报出冲突证据 id',
  },
  insufficient: {
    label: '证据不足',
    tone: 'insufficient',
    note: 'insufficient：无引用、引用键不在池内，或定位失效，需人工复核',
  },
}

const key = computed(() => (props.status ? String(props.status) : 'insufficient'))
const meta = computed(() => STATUS_META[key.value] ?? STATUS_META.insufficient)
const isKnown = computed(() => key.value in STATUS_META)
const evidenceCount = computed(() => Math.max(0, Number(props.evidenceCount ?? 0)))

const label = computed(() => {
  if (!props.factual) return '非事实句（不参与覆盖率）'
  if (!isKnown.value) return `状态未知（${key.value}）`
  return meta.value.label
})

const tone = computed(() => (props.factual && isKnown.value ? meta.value.tone : 'muted'))

const detail = computed(() => {
  const lines: string[] = []
  lines.push(`claim_id=${props.claimId ?? 'null'}｜status=${key.value}`)
  lines.push(`is_factual=${props.factual}｜evidence_count=${evidenceCount.value}`)
  if (props.charStart !== null && props.charEnd !== null) {
    lines.push(`定位：content_md char[${props.charStart}, ${props.charEnd})`)
  }
  lines.push(meta.value.note)
  lines.push('claim_coverage = supported Claim 数 ÷ 事实性 Claim 总数（事实性总数为 0 时无定义，不显示 0）')
  if (props.reason) lines.push(`理由：${props.reason}`)
  if (!isKnown.value) lines.push('后端返回了受控值域外的状态，已按 insufficient 兜底展示')
  return lines.join('\n')
})

const iconName = computed(() => {
  switch (tone.value) {
    case 'supported':
      return '✓'
    case 'contradicted':
      return '×'
    case 'insufficient':
      return '!'
    default:
      return '–'
  }
})

function onSelect(): void {
  if (!props.clickable) return
  emit('select', {
    claim_id: props.claimId ?? null,
    status: key.value,
    is_factual: props.factual,
    evidence_count: evidenceCount.value,
    evidence_ids: props.evidenceIds ? [...props.evidenceIds] : [],
    char_start: props.charStart ?? null,
    char_end: props.charEnd ?? null,
    reason: props.reason ?? null,
  })
}
</script>

<template>
  <el-tooltip
    :content="detail"
    placement="top"
    :show-after="120"
    :enterable="true"
    :hide-after="120"
  >
    <button
      type="button"
      class="claim-badge"
      :class="[`claim-badge--${tone}`, `claim-badge--${size}`]"
      :disabled="!clickable"
      :aria-label="`${label}｜点击查看证据`"
      @click="onSelect"
    >
      <span class="claim-badge__icon" aria-hidden="true">{{ iconName }}</span>
      <span class="claim-badge__text">{{ label }}</span>
      <span v-if="factual && evidenceCount > 0" class="claim-badge__count">证据 {{ evidenceCount }}</span>
    </button>
  </el-tooltip>
</template>

<style scoped>
.claim-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: 1px var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  font-family: var(--font-family-base);
  font-size: var(--font-size-xs);
  line-height: 1.6;
  white-space: nowrap;
  background-color: transparent;
  cursor: pointer;
}

.claim-badge:disabled {
  cursor: default;
}

.claim-badge--small {
  padding: 0 var(--space-1);
  font-size: var(--font-size-xs);
}

.claim-badge__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border-radius: var(--radius-pill);
  font-size: var(--font-size-2xs);
  color: var(--color-text-inverse);
  background-color: var(--color-text-disabled);
}

.claim-badge__count {
  color: var(--color-text-secondary);
}

/* ---- supported → success ---- */
.claim-badge--supported {
  border-color: var(--color-claim-supported, var(--claim-supported));
  color: var(--color-claim-supported, var(--claim-supported));
  background-color: var(--color-success-soft, var(--success-soft));
}

.claim-badge--supported .claim-badge__icon {
  background-color: var(--color-claim-supported, var(--claim-supported));
}

/* ---- contradicted → danger ---- */
.claim-badge--contradicted {
  border-color: var(--color-claim-contradicted, var(--claim-contradicted));
  color: var(--color-claim-contradicted, var(--claim-contradicted));
  background-color: var(--color-danger-soft, var(--danger-soft));
}

.claim-badge--contradicted .claim-badge__icon {
  background-color: var(--color-claim-contradicted, var(--claim-contradicted));
}

/* ---- insufficient → warning ---- */
.claim-badge--insufficient {
  border-color: var(--color-claim-insufficient, var(--claim-insufficient));
  color: var(--color-claim-insufficient, var(--claim-insufficient));
  background-color: var(--color-warning-soft, var(--warning-soft));
}

.claim-badge--insufficient .claim-badge__icon {
  background-color: var(--color-claim-insufficient, var(--claim-insufficient));
}

/* ---- 非事实句 / 未知状态：中性面，避免被误读为三态之一 ---- */
.claim-badge--muted {
  border-color: var(--color-border-strong, var(--border-strong));
  color: var(--color-text-secondary, var(--text-secondary));
  background-color: var(--color-bg-subtle, var(--bg-subtle));
  font-style: italic;
}

.claim-badge:hover:not(:disabled) {
  filter: brightness(0.97);
}

.claim-badge:focus-visible {
  outline: 2px solid var(--color-brand, var(--brand));
  outline-offset: 1px;
}
</style>
