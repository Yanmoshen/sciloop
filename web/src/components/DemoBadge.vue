<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * DemoBadge（WP16-T6）——快照 / 回放模式的**常驻醒目标识**。
 *
 * 为什么必须显眼（contracts.ui_mandatory_elements / 计划书 §4.11）
 * --------------------------------------------------------------
 * 演示有三层保险：论文库快照（`paper_feed_snapshots.is_demo=true`）、LLM 响应回放
 * （`demo_fixtures` + `is_replay=true`）、预置示例 Project。**任何一层被启用时，
 * 界面上的结果都不是实时结果**；若不加标识，评委会把固化数据误当成实时产出，
 * 属于诚信红线（contracts.forbidden_actions 第 5 条：禁止把回放结果冒充实时实验）。
 *
 * 三种模式必须视觉可区分（WP16-A7）
 * ---------------------------------
 * - `live`      ：浅绿「实时结果」，说明数据来自本次实时抓取/实时模型调用；
 * - `snapshot`  ：琥珀「演示快照 · 非实时结果」；
 * - `replay`    ：琥珀「演示回放 · 非实时结果」；
 * - 两者同时开：琥珀「演示快照 + 回放 · 非实时结果」。
 *
 * 用法（不依赖 ShellLayout，可直接放进任何产出物容器顶部）
 * -------------------------------------------------------
 * ```vue
 * <!-- 产出物顶部：醒目横幅，滚动时常驻（sticky） -->
 * <DemoBadge variant="bar" context="论文库 / 推荐视图" :data-source="feed.data_source" />
 *
 * <!-- 顶部栏：紧凑徽标（与 OwnerBadge 并排） -->
 * <DemoBadge variant="chip" />
 * ```
 * 显式覆盖优先于服务端状态：`:snapshot="true"` / `:replay="true"` / `:data-source="'replay'"`。
 *
 * 颜色纪律：只用 `styles/tokens.css` 变量（双命名 `var(--color-x, var(--x))` 兜底），
 * 组件内**不出现任何硬编码色值**。
 */
import { computed } from 'vue'

import { useDemoSession, type DataSource } from '@/api/demo'

const props = withDefaults(
  defineProps<{
    /** 产出行自带的数据来源（如 feed 的 `data_source`）；命中 snapshot/replay 即强制常驻 */
    dataSource?: DataSource
    /** 显式覆盖：本次结果是否读快照 */
    snapshot?: boolean | null
    /** 显式覆盖：本次结果是否来自回放 */
    replay?: boolean | null
    /** bar=产出物顶部醒目横幅（sticky 常驻）；chip=顶部栏紧凑徽标 */
    variant?: 'bar' | 'chip'
    /** 上下文说明，例如「论文库 · 推荐视图」「实验 Passport」 */
    context?: string
    /** 未知状态时是否仍渲染中性提示（bar 默认不渲染，避免误报） */
    showWhenUnknown?: boolean | null
    /** 轮询间隔（毫秒）；同页多个实例共享一个轮询源 */
    pollIntervalMs?: number
  }>(),
  {
    dataSource: undefined,
    snapshot: null,
    replay: null,
    variant: 'chip',
    context: '',
    showWhenUnknown: null,
    pollIntervalMs: 30_000,
  },
)

const session = useDemoSession(props.pollIntervalMs)

/** 显式 prop > dataSource 推断 > 服务端状态 */
const effective = computed(() => {
  const fromSource = props.dataSource
  if (props.snapshot !== null || props.replay !== null) {
    const snapshot = props.snapshot === true || fromSource === 'snapshot'
    const replay = props.replay === true || fromSource === 'replay'
    return { known: true, snapshot, replay }
  }
  if (fromSource === 'snapshot') return { known: true, snapshot: true, replay: false }
  if (fromSource === 'replay') return { known: true, snapshot: false, replay: true }
  if (fromSource) return { known: true, snapshot: false, replay: false }
  const status = session.status
  if (!status) return { known: false, snapshot: false, replay: false }
  return { known: true, snapshot: Boolean(status.snapshot), replay: Boolean(status.replay) }
})

const active = computed(() => effective.value.snapshot || effective.value.replay)
const known = computed(() => effective.value.known)
const shouldRender = computed(
  () => active.value || (known.value && props.variant === 'chip') || props.showWhenUnknown === true,
)

/** 一眼分清模式的主文案 */
const headline = computed(() => {
  if (!known.value) return '演示状态未获取'
  if (effective.value.snapshot && effective.value.replay) return '演示快照 + 回放 · 非实时结果'
  if (effective.value.snapshot) return '演示快照 · 非实时结果'
  if (effective.value.replay) return '演示回放 · 非实时结果'
  return props.variant === 'chip' ? '实时结果' : '实时结果（本次真实运行）'
})

/** 补充说明：明确告诉评委数据来自哪里、为什么不等于实时 */
const detail = computed(() => {
  if (!known.value) {
    return session.error ? `演示开关状态读取失败：${session.error}` : '正在读取演示开关状态'
  }
  const parts: string[] = []
  if (effective.value.snapshot) {
    parts.push('论文库读取演示前固化的 paper_feed_snapshots 快照，不是本次实时抓取')
  }
  if (effective.value.replay) {
    parts.push('模型响应来自 demo_fixtures 预录 fixture，标记 is_replay=true，不是本次实时模型输出')
  }
  if (!parts.length) {
    parts.push('数据来自服务端实时请求，未经快照或回放固化')
  }
  if (props.context) parts.unshift(props.context)
  return parts.join('；')
})

const modeClass = computed(() => ({
  'demo-badge--active': active.value,
  'demo-badge--live': known.value && !active.value,
  'demo-badge--unknown': !known.value,
  'demo-badge--bar': props.variant === 'bar',
  'demo-badge--chip': props.variant === 'chip',
}))

/** 库存摘要（可读地展示三层保险当前装载了什么，避免"演示开关打开了但没数据"） */
const inventoryNote = computed(() => {
  const inv = session.status?.inventory
  if (!inv || !active.value) return ''
  const bits: string[] = []
  if (effective.value.snapshot && inv.snapshots) {
    const views = inv.snapshots.views_covered ?? []
    bits.push(`快照视图：${views.length ? views.join('/') : '未固化'}`)
  }
  if (effective.value.replay && inv.fixtures) {
    const stages = inv.fixtures.stages_covered ?? []
    bits.push(`回放覆盖环节：${stages.length ? stages.join('/') : '未录制'}`)
  }
  return bits.join('　')
})

const tooltip = computed(() => [headline.value, detail.value, inventoryNote.value].filter(Boolean).join('\n'))
</script>

<template>
  <div
    v-if="shouldRender"
    class="demo-badge"
    :class="modeClass"
    role="status"
    aria-live="polite"
    :title="tooltip"
    data-testid="demo-badge"
  >
    <span class="demo-badge__dot" aria-hidden="true" />
    <span class="demo-badge__headline">{{ headline }}</span>
    <span v-if="variant === 'bar'" class="demo-badge__detail">{{ detail }}</span>
    <span v-if="variant === 'bar' && inventoryNote" class="demo-badge__inventory">{{ inventoryNote }}</span>
    <span v-if="variant === 'chip' && !known && !session.loading" class="demo-badge__detail">
      演示开关不可读，请勿把当前结果当作实时结果
    </span>
  </div>
</template>

<style scoped>
/* 颜色一律走 tokens.css 变量；双命名兜底，末级回落到基础语义色，绝不硬编码色值 */
.demo-badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2, 8px);
  font-size: var(--font-size-xs, 12px);
  line-height: var(--line-height-tight, 1.3);
  border: 1px solid var(--color-border, var(--border));
  border-radius: var(--radius-pill, 999px);
  padding: 2px var(--space-2, 8px);
  color: var(--color-text-secondary, var(--text-secondary));
  background-color: var(--color-bg-subtle, var(--bg-subtle));
  font-weight: 600;
}

/* ---- 冷色提示：演示固化结果（醒目） ---- */
.demo-badge--active {
  color: var(--color-demo-badge-text, var(--demo-badge-text, var(--color-warning, var(--warning))));
  background-color: var(--color-demo-badge-bg, var(--demo-badge-bg, var(--color-warning-soft, var(--warning-soft))));
  border-color: var(--color-demo-badge-text, var(--demo-badge-text, var(--color-warning, var(--warning))));
}

/* ---- 实时：低调的成功色，与演示态明显区分 ---- */
.demo-badge--live {
  color: var(--color-success, var(--success));
  background-color: var(--color-success-soft, var(--success-soft));
  border-color: var(--color-success, var(--success));
}

.demo-badge--unknown {
  border-style: dashed;
  color: var(--color-text-secondary, var(--text-secondary));
}

/* ---- 产出物顶部横幅：始终可见，不遮挡内容 ---- */
.demo-badge--bar {
  position: sticky;
  top: var(--layout-header-height, 56px);
  z-index: var(--z-popover, 1000);
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-start;
  width: 100%;
  border-radius: var(--radius-md, 6px);
  border-left-width: 4px;
  padding: var(--space-2, 8px) var(--space-3, 12px);
  font-weight: 700;
  font-size: var(--font-size-sm, 13px);
  box-shadow: var(--shadow-card, var(--shadow));
}

.demo-badge__headline {
  white-space: nowrap;
}

.demo-badge__detail {
  color: var(--color-text-secondary, var(--text-secondary));
  font-weight: 400;
}

.demo-badge--active .demo-badge__detail {
  color: var(--color-demo-badge-text, var(--demo-badge-text, var(--color-warning, var(--warning))));
  opacity: 0.9;
}

.demo-badge__inventory {
  color: var(--color-text-secondary, var(--text-secondary));
  font-weight: 400;
  font-family: var(--font-family-mono, monospace);
}

.demo-badge__dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-pill, 999px);
  background-color: currentColor;
  flex: none;
}
</style>
