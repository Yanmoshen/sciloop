<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究流程面板（对话页内）：七个研究节点 + 程序校验结果 + 迁移留痕。
 *
 * 它显示的是**程序主控**的过程，不是模型的自我汇报：
 * - 节点状态来自 `research_node_runs`（数据库是唯一依据）；
 * - 「被驳回」显示的是校验器给出的等级（格式 / 质量）与缺项；
 * - 「回退」只在三类闸门通过后才会出现，并带上必带信息；
 * - 「预检」永远是程序真实执行的结果，模型无法代为声明。
 *
 * 交互口径：
 * - 默认「每次执行前确认」，可在本项目切成免确认；装依赖 / 联网始终要显式授权；
 * - 自动判定的节点会在事件流里回显命中词，可随时改成手动指定。
 */
import { computed, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import {
  RESEARCH_TEXT,
  fetchAccessMode,
  fetchChainState,
  fetchNodes,
  fetchPreflights,
  nodeStatusText,
  nodeStatusTone,
  runPreflight,
  setAccessMode,
  streamRunNode,
  type ChainState,
  type NodesCatalog,
  type PreflightRecord,
  type ResearchEvent,
  type ResearchNode,
} from '@/api/research'

const props = defineProps<{ conversationId: string | null }>()

/**
 * 报错文案统一出口：**不把后端实现细节抛给研究者**。
 * 写操作被拒（浏览模式）要说清"现在不能做 + 去哪里开"，而不是回显请求头名字。
 */
function readableError(err: unknown, deniedAction: string): string {
  if (err instanceof ApiError && err.isForbidden) return deniedAction
  if (err instanceof Error) return err.message
  return String(err)
}

const catalog = ref<NodesCatalog | null>(null)
const chain = ref<ChainState | null>(null)
const preflights = ref<PreflightRecord[]>([])
const accessMode = ref<'ask' | 'trusted'>('ask')
const expanded = ref(false)
const busy = ref(false)
const errorText = ref('')
const events = ref<ResearchEvent[]>([])
const promptText = ref('')
const manualNode = ref<string | null>(null)
const preflightCommand = ref('python -c "print(1)"')
const preflightApproved = ref(false)
const abortCtl = ref<AbortController | null>(null)

const nodes = computed<ResearchNode[]>(() => chain.value?.nodes ?? [])
const currentLabel = computed(() => {
  const node = nodes.value.find((item) => item.node === chain.value?.current_node)
  return node?.label ?? '—'
})
const targetLabel = computed(() => {
  if (!manualNode.value) return '按输入自动判定'
  return catalog.value?.nodes.find((n) => n.node === manualNode.value)?.label ?? manualNode.value
})
const selectedRules = computed(() => {
  if (!manualNode.value) return []
  return catalog.value?.nodes.find((n) => n.node === manualNode.value)?.rules ?? []
})

async function loadCatalog(): Promise<void> {
  if (catalog.value) return
  try {
    catalog.value = await fetchNodes()
  } catch (err) {
    errorText.value = err instanceof Error ? err.message : String(err)
  }
}

async function loadChain(): Promise<void> {
  if (!props.conversationId) {
    chain.value = null
    preflights.value = []
    return
  }
  try {
    const [state, records, access] = await Promise.all([
      fetchChainState(props.conversationId),
      fetchPreflights(props.conversationId),
      fetchAccessMode(props.conversationId),
    ])
    chain.value = state
    preflights.value = records
    accessMode.value = access.execution_access
  } catch (err) {
    errorText.value = err instanceof Error ? err.message : String(err)
  }
}

watch(
  () => props.conversationId,
  () => {
    events.value = []
    manualNode.value = null
    errorText.value = ''
    void loadCatalog()
    void loadChain()
  },
  { immediate: true },
)

function toggleExpand(): void {
  expanded.value = !expanded.value
}

async function runNode(): Promise<void> {
  if (!props.conversationId || busy.value) return
  busy.value = true
  errorText.value = ''
  events.value = []
  abortCtl.value = new AbortController()
  try {
    await streamRunNode(
      props.conversationId,
      { node: manualNode.value, text: promptText.value },
      {
        onEvent: (event) => {
          events.value = [...events.value, event]
        },
      },
      abortCtl.value.signal,
    )
  } catch (err) {
    errorText.value = readableError(err, RESEARCH_TEXT.runDenied)
  } finally {
    busy.value = false
    abortCtl.value = null
    await loadChain()
  }
}

function stopRun(): void {
  abortCtl.value?.abort()
}

async function applyAccess(): Promise<void> {
  if (!props.conversationId) return
  const next = accessMode.value === 'trusted' ? 'ask' : 'trusted'
  try {
    await setAccessMode(props.conversationId, next)
    accessMode.value = next
  } catch (err) {
    errorText.value = readableError(err, RESEARCH_TEXT.accessDenied)
  }
}

async function doPreflight(): Promise<void> {
  if (!props.conversationId) return
  busy.value = true
  errorText.value = ''
  try {
    await runPreflight(props.conversationId, {
      command: preflightCommand.value,
      approved: preflightApproved.value,
    })
    await loadChain()
  } catch (err) {
    errorText.value = readableError(err, RESEARCH_TEXT.preflightDenied)
  } finally {
    busy.value = false
  }
}

function eventTitle(event: ResearchEvent): string {
  switch (event.type) {
    case 'validation': {
      const level = (event.payload as { level?: string }).level
      return level === 'L1' ? '产出不合契约，已自动重跑' : '产出被驳回，已要求重做'
    }
    case 'notice':
      return '结构化输出降级为宽松解析'
    case 'revert':
      return '回退（三类闸门已通过）'
    case 'migrated':
      return '通过校验，进入下一节点'
    case 'waiting_human':
      return '多次修复未通过，转人工介入'
    case 'error':
      return '执行失败'
    case 'attempt':
      return '本轮尝试'
    case 'done':
      return '本轮结束'
    default:
      return '已进入节点'
  }
}

function eventTone(event: ResearchEvent): string {
  switch (event.type) {
    case 'validation':
    case 'error':
      return 'err'
    case 'waiting_human':
    case 'notice':
      return 'warn'
    case 'revert':
      return 'info'
    case 'migrated':
    case 'done':
      return 'ok'
    default:
      return 'idle'
  }
}

function itemsOf(event: ResearchEvent): Array<{ rule: string; level: string; message: string }> {
  const payload = event.payload as { items?: Array<{ rule: string; level: string; message: string }> }
  return payload.items ?? []
}

function textOf(event: ResearchEvent, key: string): string {
  const value = (event.payload as Record<string, unknown>)[key]
  return value === undefined || value === null ? '' : String(value)
}
</script>

<template>
  <section class="rf" :class="{ 'rf--busy': busy }">
    <header class="rf__bar">
      <span class="rf__head">研究流程</span>
      <span class="tag tag--info">当前：{{ currentLabel }}</span>
      <ul class="rf__chips">
        <li
          v-for="node in nodes"
          :key="node.node"
          class="rf__chip"
          :class="[`rf__chip--${nodeStatusTone(node)}`, { 'rf__chip--impl': node.implemented }]"
          :title="`${node.label}｜${nodeStatusText(node)}${
            node.implemented ? '｜本版已实装校验' : '｜本版为占位节点'
          }`"
        >
          <span class="rf__chipLabel">{{ node.label }}</span>
          <span v-if="node.retry_count" class="rf__chipRetry">重试 {{ node.retry_count }}</span>
        </li>
      </ul>
      <button class="rf__act press" type="button" @click="toggleExpand">
        {{ expanded ? '收起' : '展开' }}
      </button>
    </header>

    <div class="fold" :class="{ 'fold--open': expanded }">
      <div class="rf__body">
        <div v-if="!props.conversationId" class="rf__empty">{{ RESEARCH_TEXT.needProject }}</div>

        <template v-else>
          <div class="rf__row">
            <label class="rf__field">
              <span class="rf__fieldLabel">给本节点的一句话</span>
              <textarea
                v-model="promptText"
                rows="2"
                class="rf__input"
                placeholder="例如：调研子词分词器目标函数的相关工作"
              />
            </label>
            <label class="rf__field rf__field--narrow">
              <span class="rf__fieldLabel">执行节点</span>
              <select v-model="manualNode" class="rf__input">
                <option :value="null">按输入自动判定</option>
                <option v-for="item in catalog?.nodes ?? []" :key="item.node" :value="item.node">
                  {{ item.label }}
                </option>
              </select>
            </label>
          </div>

          <div class="rf__row rf__row--acts">
            <button
              v-if="!busy"
              class="rf__run press"
              type="button"
              @click="runNode"
            >
              {{ RESEARCH_TEXT.runIdle }}
            </button>
            <button v-else class="rf__run press" type="button" @click="stopRun">
              {{ RESEARCH_TEXT.runBusy }}中止
            </button>
            <span class="tag" :class="accessMode === 'trusted' ? 'tag--ok' : 'tag--idle'">
              {{ accessMode === 'trusted' ? RESEARCH_TEXT.accessTrusted : RESEARCH_TEXT.accessAsk }}
            </span>
            <button class="rf__act press" type="button" @click="applyAccess">
              切换为{{ accessMode === 'trusted' ? '每次确认' : '本项目免确认' }}
            </button>
            <span class="rf__target">目标：{{ targetLabel }}</span>
          </div>

          <ul v-if="selectedRules.length" class="rf__rules">
            <li v-for="rule in selectedRules" :key="rule.rule" class="rf__rule">
              <span class="tag" :class="rule.level === '格式' ? 'tag--idle' : 'tag--warn'">
                {{ rule.level }}
              </span>
              <span class="rf__ruleText">{{ rule.message }}</span>
            </li>
          </ul>

          <div v-if="errorText" class="rf__err">{{ errorText }}</div>

          <h3 class="rf__title">执行过程</h3>
          <ol v-if="events.length" class="rf__events">
            <li
              v-for="(event, index) in events"
              :key="index"
              class="rf__event"
              :class="`rf__event--${eventTone(event)}`"
            >
              <div class="rf__eventHead">
                <span class="tag" :class="`tag--${eventTone(event)}`">{{ eventTitle(event) }}</span>
                <span v-if="event.type === 'validation'" class="rf__eventMeta">
                  第 {{ textOf(event, 'attempt') }} / {{ textOf(event, 'max_attempts') }} 次
                </span>
                <span v-else-if="event.type === 'attempt'" class="rf__eventMeta">
                  命中论文 {{ textOf(event, 'library_hits') }} 篇
                </span>
              </div>
              <ul v-if="itemsOf(event).length" class="rf__items">
                <li v-for="item in itemsOf(event)" :key="item.rule" class="rf__item">
                  <span class="tag tag--err">{{ item.level }}</span>
                  <span>{{ item.message }}</span>
                </li>
              </ul>
              <p
                v-if="
                  event.type === 'revert' ||
                  event.type === 'migrated' ||
                  event.type === 'notice' ||
                  event.type === 'error' ||
                  event.type === 'waiting_human'
                "
                class="rf__eventBody"
              >
                <template v-if="event.type === 'revert'">
                  {{ textOf(event, 'from_label') }} → {{ textOf(event, 'to_label') }}：{{
                    textOf(event, 'reason')
                  }}
                </template>
                <template v-else-if="event.type === 'migrated'">
                  {{ textOf(event, 'from_label') }} → {{ textOf(event, 'to_label') }}
                </template>
                <template v-else>{{ textOf(event, 'message') }}</template>
              </p>
            </li>
          </ol>
          <p v-else class="rf__empty">点上方按钮执行一次，这里会逐条出现程序判定结果。</p>

          <h3 class="rf__title">{{ RESEARCH_TEXT.transitionLabel }}</h3>
          <ul v-if="chain?.transitions.length" class="rf__trans">
            <li v-for="item in chain.transitions" :key="item.id" class="rf__tran">
              <span class="tag" :class="item.kind === 'revert' ? 'tag--warn' : 'tag--ok'">
                {{ item.kind === 'revert' ? '回退' : item.kind === 'stop' ? '停止' : '推进' }}
              </span>
              <span class="rf__tranWho">{{
                item.trigger === 'model' ? '模型建议' : item.trigger === 'researcher' ? '研究者' : '程序'
              }}</span>
              <span class="rf__tranBody">{{ item.reason }}</span>
            </li>
          </ul>
          <p v-else class="rf__empty">还没有迁移记录。回退次数 {{ chain?.total_reverts ?? 0 }} / {{ chain?.max_total_reverts ?? 4 }}</p>

          <h3 class="rf__title">{{ RESEARCH_TEXT.preflightLabel }}</h3>
          <div class="rf__row rf__row--acts">
            <input v-model="preflightCommand" class="rf__input rf__input--cmd" placeholder="python -c 或 pytest 等白名单命令" />
            <button class="rf__act press" type="button" :disabled="busy" @click="doPreflight">
              执行预检
            </button>
            <label class="rf__check">
              <input v-model="preflightApproved" type="checkbox" />
              <span>授权安装依赖 / 联网</span>
            </label>
          </div>
          <ul v-if="preflights.length" class="rf__trans">
            <li v-for="(item, index) in preflights.slice(0, 3)" :key="index" class="rf__tran">
              <span class="tag" :class="item.exit_code === 0 ? 'tag--ok' : 'tag--err'">
                退出码 {{ item.exit_code }}
              </span>
              <span class="rf__tranBody">{{ item.command || '（无命令）' }}</span>
              <span v-if="item.note" class="rf__tranBody">{{ item.note }}</span>
            </li>
          </ul>
          <p v-else class="rf__empty">{{ RESEARCH_TEXT.preflightEmpty }}</p>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.rf {
  border-top: 1px solid var(--h-line);
  background: var(--h-surface);
}

.rf__bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  flex-wrap: wrap;
}

.rf__head {
  font-size: 13px;
  font-weight: 600;
  color: var(--h-fg);
}

.rf__chips {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
  flex-wrap: wrap;
}

.rf__chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--h-line);
  background: var(--h-surface-raised);
  font-size: 12px;
  color: var(--h-fg-muted);
  transition: border-color var(--motion-dur) var(--motion-ease);
}

/* 未实装校验的节点用虚线边框区分，不写成说明小字 */
.rf__chip:not(.rf__chip--impl) {
  border-style: dashed;
}

.rf__chip--ok {
  border-color: var(--h-primary);
  color: var(--h-primary);
}
.rf__chip--info {
  border-color: var(--h-secondary);
  color: var(--h-secondary);
}
.rf__chip--warn {
  border-color: #b8860b;
  color: #b8860b;
}
.rf__chip--err {
  border-color: #c0392b;
  color: #c0392b;
}

.rf__chipRetry {
  font-variant-numeric: tabular-nums;
  opacity: 0.75;
}

.rf__act,
.rf__run {
  margin-left: auto;
  padding: 5px 12px;
  border-radius: 8px;
  border: 1px solid var(--h-line-strong);
  background: var(--h-surface-raised);
  color: var(--h-fg);
  font-size: 12px;
  cursor: pointer;
}

.rf__run {
  margin-left: 0;
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
  font-weight: 600;
}

.rf__act:hover,
.rf__run:hover {
  background: var(--h-hover);
}
.rf__run:hover {
  background: var(--h-primary);
  filter: brightness(1.05);
}

.rf__body {
  padding: 4px 14px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rf__row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-wrap: wrap;
}

.rf__row--acts {
  align-items: center;
}

.rf__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 320px;
}

.rf__field--narrow {
  flex: 0 1 200px;
}

.rf__fieldLabel {
  font-size: 12px;
  color: var(--h-fg-muted);
}

.rf__input {
  width: 100%;
  padding: 6px 8px;
  border-radius: 8px;
  border: 1px solid var(--h-line);
  background: var(--h-surface-input);
  color: var(--h-fg);
  font: inherit;
  font-size: 13px;
  resize: vertical;
}

.rf__input--cmd {
  flex: 1 1 280px;
}

.rf__check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--h-fg-muted);
}

.rf__target {
  font-size: 12px;
  color: var(--h-fg-muted);
  margin-left: auto;
}

.rf__rules,
.rf__items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rf__rule,
.rf__item {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  font-size: 12px;
  color: var(--h-fg-muted);
}

.rf__ruleText {
  line-height: 1.5;
}

.rf__title {
  margin: 6px 0 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--h-fg);
}

.rf__events {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rf__event {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--h-line);
  border-left-width: 3px;
  background: var(--h-surface-raised);
}

.rf__event--ok {
  border-left-color: var(--h-primary);
}
.rf__event--err {
  border-left-color: #c0392b;
}
.rf__event--warn {
  border-left-color: #b8860b;
}
.rf__event--info {
  border-left-color: var(--h-secondary);
}

.rf__eventHead {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rf__eventMeta {
  font-size: 12px;
  color: var(--h-fg-muted);
  font-variant-numeric: tabular-nums;
}

.rf__eventBody {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--h-fg-muted);
  line-height: 1.6;
}

.rf__trans {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rf__tran {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--h-fg-muted);
}

/* 标签与来源不参与换行挤压：否则「回退」这种两字标签会被压成两行 */
.rf__tran > .tag,
.rf__tranWho {
  flex: none;
  white-space: nowrap;
}

.rf__tranWho {
  white-space: nowrap;
  color: var(--h-fg);
}
.rf__tranBody {
  line-height: 1.55;
}

.rf__empty {
  margin: 0;
  font-size: 12px;
  color: var(--h-fg-muted);
}

.rf__err {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid #c0392b;
  color: #c0392b;
  font-size: 12px;
}

.rf--busy .rf__run {
  background: var(--h-fg-subtle);
  border-color: var(--h-fg-subtle);
}
</style>
