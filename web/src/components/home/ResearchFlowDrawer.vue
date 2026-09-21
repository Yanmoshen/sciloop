<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究流程 · 右侧控制台抽屉（2026-09-21 由对话页内的横条面板搬来并重做视觉）。
 *
 * 只呈现**七节点的推进状态**：
 * - 未开始 = 空心灰圆；进行中 = 空心圆 + 沿连线流向它的流光（只要该节点没完成就一直循环播）；
 * - 完成 = 绿色；失败 / 被阻塞 / 等待人工 = 红色。
 * 校验明细、迁移留痕、预检这些程序过程不在这里堆，走「更多 → 展开详情」看原面板。
 *
 * 数据仍以数据库为唯一依据（`fetchChainState`），打开抽屉时每 4 秒对齐一次，
 * 这样自动连续跑推进节点时左栏不必手动刷新也能看到状态变化。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  RESEARCH_TEXT,
  fetchAccessMode,
  fetchChainState,
  setAccessMode,
  streamRunNode,
  type ChainState,
} from '@/api/research'
import ResearchFlowPanel from '@/components/home/ResearchFlowPanel.vue'
import { usePipelineDrawerStore } from '@/stores/pipelineDrawer'

const props = defineProps<{ conversationId: string | null }>()
const ui = usePipelineDrawerStore()

const chain = ref<ChainState | null>(null)
const accessMode = ref<'ask' | 'trusted'>('ask')
const busy = ref(false)
const menuOpen = ref(false)
const showDetail = ref(false)
const errorText = ref('')
const abortCtl = ref<AbortController | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

type Visual = 'idle' | 'running' | 'done' | 'bad'

function visualOf(status: string): Visual {
  if (status === 'done') return 'done'
  if (status === 'running') return 'running'
  if (status === 'failed' || status === 'blocked' || status === 'waiting_human') return 'bad'
  return 'idle'
}

const nodes = computed(() => chain.value?.nodes ?? [])
const visuals = computed<Visual[]>(() => nodes.value.map((item) => visualOf(item.status)))
const doneCount = computed(() => visuals.value.filter((item) => item === 'done').length)
const runningIndex = computed(() => visuals.value.indexOf('running'))
const badIndex = computed(() => visuals.value.indexOf('bad'))

const headline = computed(() => {
  const total = nodes.value.length
  if (!total) return '未开始'
  if (badIndex.value >= 0) return `${nodes.value[badIndex.value].label} · 需人工介入`
  if (doneCount.value === total) return `已完成 · ${total}/${total}`
  if (runningIndex.value >= 0) {
    return `${nodes.value[runningIndex.value].label} · ${doneCount.value}/${total}`
  }
  return `未开始 · 0/${total}`
})

const modeLabel = computed(() => (accessMode.value === 'trusted' ? '免确认' : '每次确认'))

async function loadChain(): Promise<void> {
  if (!props.conversationId) {
    chain.value = null
    return
  }
  try {
    const [state, access] = await Promise.all([
      fetchChainState(props.conversationId),
      fetchAccessMode(props.conversationId),
    ])
    chain.value = state
    accessMode.value = access.execution_access
    errorText.value = ''
  } catch (error) {
    errorText.value = error instanceof Error ? error.message : String(error)
  }
}

async function run(): Promise<void> {
  if (busy.value) {
    stop()
    return
  }
  if (!props.conversationId) return
  busy.value = true
  errorText.value = ''
  abortCtl.value = new AbortController()
  try {
    await streamRunNode(
      props.conversationId,
      { node: null, text: '' },
      { onEvent: () => undefined },
      abortCtl.value.signal,
    )
  } catch (error) {
    if (!abortCtl.value?.signal.aborted) {
      errorText.value = error instanceof Error ? error.message : String(error)
    }
  } finally {
    busy.value = false
    abortCtl.value = null
    await loadChain()
  }
}

function stop(): void {
  abortCtl.value?.abort()
}

async function toggleMode(): Promise<void> {
  if (!props.conversationId) return
  const next = accessMode.value === 'trusted' ? 'ask' : 'trusted'
  try {
    await setAccessMode(props.conversationId, next)
    accessMode.value = next
  } catch (error) {
    errorText.value = error instanceof Error ? error.message : String(error)
  }
  menuOpen.value = false
}

const resizing = ref(false)
let dragFrom = 0
let dragWidth = 0

function startDrag(event: MouseEvent): void {
  event.preventDefault()
  resizing.value = true
  dragFrom = event.clientX
  dragWidth = ui.width
}

function onMove(event: MouseEvent): void {
  if (!resizing.value) return
  ui.setWidth(dragWidth + (dragFrom - event.clientX))
}

function endDrag(): void {
  resizing.value = false
}

watch(
  () => props.conversationId,
  () => {
    showDetail.value = false
    void loadChain()
  },
  { immediate: true },
)

watch(
  () => chain.value?.has_chain,
  (has) => {
    if (has) ui.autoShow()
  },
)

onMounted(() => {
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', endDrag)
  timer = setInterval(() => {
    if (ui.open) void loadChain()
  }, 4000)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousemove', onMove)
  document.removeEventListener('mouseup', endDrag)
  if (timer) clearInterval(timer)
  timer = null
  abortCtl.value?.abort()
})
</script>

<template>
  <aside
    class="rfd"
    :class="{ 'rfd--open': ui.open, 'rfd--resizing': resizing }"
    :style="{ '--rfd-w': `${ui.width}px` }"
    aria-label="研究流程"
  >
    <div class="rfd__resizer" role="separator" aria-orientation="vertical" @mousedown="startDrag" />

    <header class="rfd__head">
      <div class="rfd__title">研究流程</div>
      <button class="rfd__icon" type="button" title="收起" aria-label="收起" @click="ui.close()">
        <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path
            d="M6 3.5 10.5 8 6 12.5"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </button>
      <p class="rfd__meta">{{ headline }}</p>
    </header>

    <div class="rfd__tools">
      <button
        class="rfd__btn"
        type="button"
        :disabled="!conversationId"
        @click="run"
      >
        {{ busy ? '中止' : '执行节点' }}
      </button>
      <span class="rfd__spacer" />
      <div class="rfd__more">
        <button
          class="rfd__icon"
          type="button"
          title="更多"
          aria-label="更多"
          aria-haspopup="menu"
          :aria-expanded="menuOpen ? 'true' : 'false'"
          @click="menuOpen = !menuOpen"
        >
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <circle cx="3.4" cy="8" r="1.3" fill="currentColor" />
            <circle cx="8" cy="8" r="1.3" fill="currentColor" />
            <circle cx="12.6" cy="8" r="1.3" fill="currentColor" />
          </svg>
        </button>
        <div v-if="menuOpen" class="rfd__menu" role="menu">
          <button class="rfd__menuitem" type="button" role="menuitem" @click="toggleMode">
            改为{{ modeLabel === '免确认' ? '每次确认' : '免确认' }}
          </button>
          <button
            class="rfd__menuitem"
            type="button"
            role="menuitem"
            @click="showDetail = !showDetail; menuOpen = false"
          >
            {{ showDetail ? '收起详情' : '展开详情' }}
          </button>
        </div>
      </div>
    </div>

    <div class="rfd__body scroll-y">
      <p v-if="!conversationId" class="rfd__empty">{{ RESEARCH_TEXT.needProject }}</p>
      <ol v-else class="rflow">
        <li
          v-for="(node, index) in nodes"
          :key="node.node"
          class="rflow__node"
          :class="`rflow__node--${visuals[index]}`"
        >
          <span class="rflow__mark">
            <span class="rflow__dot" />
            <span class="rflow__sweep" />
          </span>
          <span class="rflow__dash" />
          <span class="rflow__name">{{ node.label }}</span>
        </li>
      </ol>

      <p v-if="errorText" class="rfd__err">{{ errorText }}</p>

      <div v-if="showDetail && conversationId" class="rfd__detail">
        <ResearchFlowPanel :conversation-id="conversationId" />
      </div>
    </div>
  </aside>
</template>

<style scoped>
.rfd {
  position: fixed;
  top: 0;
  right: 0;
  z-index: 30;
  width: var(--rfd-w);
  height: 100%;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--h-line);
  background: var(--h-surface-raised);
  color: var(--h-fg);
  transform: translateX(100%);
  transition: transform 340ms var(--motion-ease-out);
}

.rfd--open {
  transform: none;
}

.rfd--resizing {
  transition: none;
}

.rfd--resizing .rfd__resizer {
  user-select: none;
}

.rfd__resizer {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 7px;
  cursor: col-resize;
  z-index: 2;
}

.rfd__resizer::after {
  content: '';
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 1.5px;
  background: transparent;
  transition: background-color 160ms var(--motion-ease);
}

.rfd__resizer:hover::after,
.rfd--resizing .rfd__resizer::after {
  background: var(--h-primary);
}

.rfd__head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 16px 16px 12px;
  border-bottom: 1px solid var(--h-line);
}

.rfd__title {
  font-size: var(--font-size-lg);
  font-weight: 600;
}

.rfd__meta {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
}

.rfd__icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  cursor: pointer;
  transition: background-color 160ms var(--motion-ease), color 160ms var(--motion-ease);
}

.rfd__icon:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

.rfd__tools {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--h-line);
}

.rfd__btn {
  height: 30px;
  padding: 0 14px;
  border: 1px solid var(--h-primary);
  border-radius: 999px;
  background: var(--h-primary);
  color: var(--h-primary-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.rfd__btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.rfd__spacer {
  flex: 1;
}

.rfd__more {
  position: relative;
}

.rfd__menu {
  position: absolute;
  right: 0;
  top: 34px;
  z-index: 4;
  min-width: 150px;
  padding: 5px;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--h-line-strong);
  border-radius: 11px;
  background: var(--h-surface-raised);
  box-shadow: 0 18px 40px rgba(0, 0, 0, 0.18);
}

.rfd__menuitem {
  padding: 8px 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
}

.rfd__menuitem:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

.rfd__body {
  flex: 1;
  min-height: 0;
  padding: 18px 16px 24px;
}

.rfd__empty,
.rfd__err {
  margin: 0;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
}

.rfd__err {
  margin-top: 12px;
  color: var(--color-danger);
}

.rfd__detail {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--h-line);
}

.rflow {
  position: relative;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rflow::before {
  content: '';
  position: absolute;
  left: 7px;
  top: 12px;
  bottom: 12px;
  width: 1px;
  background: var(--h-line);
}

.rflow__node {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 0;
}

.rflow__mark {
  position: relative;
  width: 15px;
  height: 15px;
  flex: none;
  display: grid;
  place-items: center;
}

.rflow__dot {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 1.5px solid var(--h-line-strong);
  background: var(--h-surface-raised);
  transition: border-color 240ms var(--motion-ease), background-color 240ms var(--motion-ease),
    box-shadow 240ms var(--motion-ease);
}

.rflow__dash {
  width: 9px;
  height: 1.5px;
  flex: none;
  border-radius: 1px;
  background: var(--h-line-strong);
  transition: background-color 240ms var(--motion-ease);
}

.rflow__name {
  color: var(--h-fg-muted);
  font-size: var(--font-size-md);
  transition: color 240ms var(--motion-ease);
}

.rflow__node--done .rflow__dot {
  border-color: var(--color-success);
  background: var(--color-success-soft);
}

.rflow__node--done .rflow__dash {
  background: var(--color-success);
}

.rflow__node--done .rflow__name {
  color: var(--h-fg);
}

.rflow__node--bad .rflow__dot {
  border-color: var(--color-danger);
  background: var(--color-danger-soft);
}

.rflow__node--bad .rflow__dash {
  background: var(--color-danger);
}

.rflow__node--bad .rflow__name {
  color: var(--h-fg);
  font-weight: 500;
}

.rflow__node--running .rflow__dot {
  border-color: var(--h-fg);
  box-shadow: 0 0 0 3px var(--h-hover);
  animation: rfdPulse 1.5s ease-in-out infinite;
}

.rflow__node--running .rflow__dash {
  background: var(--h-fg-muted);
}

.rflow__node--running .rflow__name {
  color: var(--h-fg);
  font-weight: 500;
}

@keyframes rfdPulse {
  50% {
    transform: scale(1.16);
    box-shadow: 0 0 0 5px var(--h-hover);
  }
}

/* 流光：一声接一声地循环，只要该节点没完成就不停
   （浅色主题用品牌色，深色主题用纯白 —— 白底上看不见白流光） */
.rflow__sweep {
  display: none;
  position: absolute;
  left: 5px;
  bottom: 50%;
  width: 5px;
  height: 42px;
  border-radius: 3px;
  background: linear-gradient(180deg, transparent, var(--rfd-sweep), transparent);
  filter: drop-shadow(0 0 6px var(--rfd-sweep-glow));
  animation: rfdSweep 1.5s cubic-bezier(0.4, 0, 0.2, 1) infinite;
  pointer-events: none;
  --rfd-sweep: var(--h-primary);
  --rfd-sweep-glow: rgba(238, 79, 39, 0.35);
}

:global(:root[data-theme='dark']) .rflow__sweep {
  --rfd-sweep: #ffffff;
  --rfd-sweep-glow: rgba(255, 255, 255, 0.5);
}

.rflow__node--running .rflow__sweep {
  display: block;
}

@keyframes rfdSweep {
  0% {
    transform: translateY(-46px) scaleY(0.7);
    opacity: 0;
  }
  18% {
    opacity: 1;
  }
  82% {
    opacity: 1;
  }
  100% {
    transform: translateY(10px) scaleY(1);
    opacity: 0;
  }
}
</style>
