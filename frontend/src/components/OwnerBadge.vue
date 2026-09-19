<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * OwnerBadge（WP16-T6）——**当前访问面**常驻标识。
 *
 * 为什么必须常驻（计划书 §4.11 / contracts.api_contract）
 * -----------------------------------------------------
 * 在线链接默认运行 `public_demo`：匿名用户只能读预置成果 + 限额回放，
 * 不能改模型配置、切换全局演示模式、重置数据或触发付费实时任务。
 * 评委需要立刻知道"我现在是只读演示面还是可写研究者面"，否则会以为按钮坏了。
 *
 * 令牌纪律（红线）
 * ----------------
 * - 令牌只由 `api/client.ts` 从 `sessionStorage` 注入 `X-Owner-Token`；
 *   本组件**从不读取令牌内容、不显示、不写入任何持久化产物**；
 * - 访问面以 `GET /owner/session` 的服务端判定为准，前端不做"本地认为自己可写"的推断。
 *
 * 用法
 * ----
 * ```vue
 * <OwnerBadge />                              <!-- 顶部栏紧凑徽标 -->
 * <OwnerBadge variant="bar" />                <!-- 只读面提示横幅 -->
 * <OwnerBadge :access-mode="'owner_mode'" :is-owner="true" />  <!-- 显式覆盖 -->
 * ```
 * 颜色纪律：只用 `styles/tokens.css` 变量（双命名兜底），不出现硬编码色值。
 */
import { computed } from 'vue'

import { useDemoSession, type AccessMode } from '@/api/demo'

const props = withDefaults(
  defineProps<{
    accessMode?: AccessMode | null
    isOwner?: boolean | null
    variant?: 'bar' | 'chip'
    /** 轮询间隔（毫秒）；同页多个实例共享一个轮询源 */
    pollIntervalMs?: number
  }>(),
  { accessMode: null, isOwner: null, variant: 'chip', pollIntervalMs: 30_000 },
)

const session = useDemoSession(props.pollIntervalMs)

const mode = computed<AccessMode>(() => props.accessMode ?? session.accessMode)
const owner = computed(() => (props.isOwner === null ? session.isOwner : props.isOwner === true))
const unknown = computed(() => props.accessMode === null && session.status === null && !session.loading)

const headline = computed(() => {
  if (unknown.value) return '访问面未获取'
  return owner.value ? 'owner_mode · 研究者可写' : 'public_demo · 只读演示面'
})

const detail = computed(() => {
  if (unknown.value) {
    return session.error ? `访问面读取失败：${session.error}` : '正在读取访问面'
  }
  if (owner.value) {
    return mode.value === 'owner_mode'
      ? '已携带有效 X-Owner-Token：写操作与实时运行可用（服务端仍逐条校验并留审计日志）'
      : '已携带 X-Owner-Token，但服务端当前访问面仍为 public_demo：写操作会被拒绝'
  }
  const reason = session.ownerSession?.permissions?.reason
  return reason ?? '匿名会话只读：写操作返回 403 owner_token_required；令牌只由服务端环境变量提供'
})

const modeClass = computed(() => ({
  'owner-badge--owner': !unknown.value && owner.value,
  'owner-badge--public': !unknown.value && !owner.value,
  'owner-badge--unknown': unknown.value,
  'owner-badge--bar': props.variant === 'bar',
  'owner-badge--chip': props.variant === 'chip',
}))

const tooltip = computed(() =>
  [
    headline.value,
    detail.value,
    `请求头：${session.ownerSession?.owner_header ?? 'X-Owner-Token'}`,
    session.ownerSession?.owner_token_source ?? '',
  ]
    .filter(Boolean)
    .join('\n'),
)
</script>

<template>
  <div
    class="owner-badge"
    :class="modeClass"
    role="status"
    aria-live="polite"
    :title="tooltip"
    data-testid="owner-badge"
    :data-access-mode="unknown ? 'unknown' : mode"
  >
    <span class="owner-badge__dot" aria-hidden="true" />
    <span class="owner-badge__headline">{{ headline }}</span>
    <span v-if="variant === 'bar'" class="owner-badge__detail">{{ detail }}</span>
    <span v-else-if="!unknown && !owner" class="owner-badge__detail">只读</span>
    <span v-else-if="!unknown && owner" class="owner-badge__detail">可写</span>
  </div>
</template>

<style scoped>
/* 颜色一律走 tokens.css 变量；双命名兜底，绝不硬编码色值 */
.owner-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2, 8px);
  font-size: var(--font-size-xs, 12px);
  line-height: var(--line-height-tight, 1.3);
  font-weight: 600;
  padding: 2px var(--space-2, 8px);
  border: 1px solid var(--color-border, var(--border));
  border-radius: var(--radius-pill, 999px);
  color: var(--color-text-secondary, var(--text-secondary));
  background-color: var(--color-bg-subtle, var(--bg-subtle));
}

/* ---- public_demo：中性只读面，虚线边框与可写面区分 ---- */
.owner-badge--public {
  border-style: dashed;
  color: var(--color-text-secondary, var(--text-secondary));
  background-color: var(--color-bg-subtle, var(--bg-subtle));
}

/* ---- owner_mode：品牌色，明确"可写" ---- */
.owner-badge--owner {
  color: var(--color-brand, var(--brand));
  background-color: var(--color-brand-soft, var(--brand-soft));
  border-color: var(--color-brand, var(--brand));
}

.owner-badge--unknown {
  border-style: dotted;
  color: var(--color-text-secondary, var(--text-secondary));
}

.owner-badge--bar {
  display: flex;
  width: 100%;
  border-radius: var(--radius-md, 6px);
  border-left-width: 4px;
  padding: var(--space-2, 8px) var(--space-3, 12px);
  font-size: var(--font-size-sm, 13px);
  font-weight: 700;
  box-shadow: var(--shadow-card, var(--shadow));
}

.owner-badge__headline {
  white-space: nowrap;
}

.owner-badge__detail {
  font-weight: 400;
  opacity: 0.9;
}

.owner-badge__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-pill, 999px);
  background-color: currentColor;
  flex: none;
}
</style>
