<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 对话页（`/` 新对话 · `/c/:conversationId` 打开已有对话）。
 *
 * 视觉口径以 Cherry Studio 为准（源码：`components/chat/messages/frame/MessageHeader.tsx`）：
 * - 用户消息**一律右对齐**、带浅底气泡；助手回答**无气泡无边框**直接输出；
 * - 助手顶部两行 = 模型名（上）+ 耗时（下：进行中只显示递增秒数，完成显示「已完成 12s」）；
 * - 助手底部 = 复制按钮；消息区滚动下边界**紧贴输入栏上沿**（输入栏留在文档流里）。
 *
 * 链路：`POST /chat/home/stream`（真流式 SSE）→ 边收边渲染；
 * 六环节流水线那条非流式主链路完全不受影响（见 `app/llm/adapter.py` 的 `chat_stream`）。
 * 中断/出错时**保留已生成部分**并在尾部如实标注，不假装完成。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'
import { useRoute, useRouter } from 'vue-router'

import { streamChatHome } from '@/api/chat'
import { getConversation } from '@/api/conversations'
import MarkdownText from '@/components/MarkdownText.vue'
import ConfirmDialog from '@/components/home/ConfirmDialog.vue'
import ResearchFlowPanel from '@/components/home/ResearchFlowPanel.vue'
import ProjectCreateDialog from '@/components/ProjectCreateDialog.vue'
import type { CreatedProject } from '@/api/projects'
import { useConversationStore } from '@/stores/conversations'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'
import { consumeEntrance } from '@/utils/pageEntrance'

type TurnStatus = 'streaming' | 'done' | 'interrupted'

interface Turn {
  role: 'user' | 'assistant'
  content: string
  model?: string
  durationMs?: number
  status?: TurnStatus
}

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const settings = useSettingsStore()
const conversations = useConversationStore()

const prompt = ref('')
const files = ref<Array<{ name: string }>>([])
const fileInput = ref<HTMLInputElement | null>(null)
const textareaEl = ref<HTMLTextAreaElement | null>(null)
const threadEl = ref<HTMLElement | null>(null)

const turns = ref<Turn[]>([])
const conversationId = ref<string | null>(null)
const conversationTitle = ref('')
const projectId = ref<number | null>(null)
const archived = ref(false)
const phase = ref<'idle' | 'thinking'>('idle')
const errorText = ref('')

const dialogOpen = ref(false)
const dialogPrefill = ref('')

const pickedRef = ref('')
const pickerOpen = ref(false)
const copiedIndex = ref<number | null>(null)

/** 计时：进行中显示递增秒数，完成后换成后端回传的真实耗时 */
const elapsedMs = ref(0)
let tick: number | null = null
let streamStartedAt = 0

/**
 * 首页开场那一行「流程」文案（各阶段之间用 → 连接）。
 * 注意：这是**给研究者看的闭环描述**，与工作台里六环节的机器阶段名（`STAGE_LABELS`：
 * survey/plan/plan_review/experiment/writing/review）是两套口径，改这里不会动工作台。
 */
const PIPELINE =
  '文献调研 → Idea 生成与可行性分析 → 实验准备 → 执行实验 → 结果分析 → 论文写作 → 论文评审'

/**
 * 研究流程面板作用的项目：已打开的对话用它的项目；项目内新建对话用 `?project=` 那个。
 * 两者都没有（真正的空白首页）就不显示可执行按钮 —— 研究链挂在项目上，没有项目就没有链。
 */


const active = computed(() => turns.value.length > 0 || phase.value === 'thinking')
const canSend = computed(() => prompt.value.trim().length > 0 || files.value.length > 0)

const defaultProvider = computed(() => settings.configs.find((config) => config.is_default) ?? null)

const modelOptions = computed(() => {
  const provider = defaultProvider.value
  if (!provider) return [{ value: 'auto', label: 'auto' }]
  return (provider.models ?? []).map((entry) => ({
    value: `${provider.id}:${entry.model_id}`,
    label: entry.model_id,
  }))
})

const pickedLabel = computed(
  () => modelOptions.value.find((item) => item.value === pickedRef.value)?.label ?? 'auto',
)

/** 新对话在哪个项目下创建：`/?project=<id>`（左栏项目行的 ＋ 会带这个参数） */
const pendingProjectId = computed(() => {
  const raw = route.query.project
  const value = Number(Array.isArray(raw) ? raw[0] : raw)
  return Number.isInteger(value) && value > 0 ? value : null
})

/**
 * 开场主标题：在项目内新建对话时点名项目（`在“项目名”中开始对话`）。
 * 名字取不到（项目已删 / 还没加载完）就退回默认文案 —— 不编造项目名。
 */
const heroTitle = computed(() => {
  const name = session.projectName(pendingProjectId.value)
  return name ? `在“${name}”中开始对话` : '使用 AI，体验全新科研工作流'
})

/**
 * 开场逐级入场：只在**从别的页面切回首页类路由**时播（壳层打标，这里消费一次）。
 * 「已经在首页」（点 ＋ 新建对话、首页 ↔ 对话互跳）与硬刷新都不播 —— 那是同页内切换，
 * 再放一遍"自下而上"只会显得啰嗦。
 */
const entranceOn = ref(false)
let entranceTimer: number | null = null

function fmtDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h}h${m}m${s}s`
  if (m > 0) return `${m}m${s}s`
  return `${s}s`
}

function durationText(turn: Turn): string {
  if (turn.role !== 'assistant') return ''
  if (turn.status === 'streaming') return `${Math.floor(elapsedMs.value / 1000)}s`
  if (turn.durationMs) return `已完成 ${fmtDuration(turn.durationMs)}`
  return ''
}

function pickModel(value: string): void {
  pickedRef.value = value
  pickerOpen.value = false
}

function pickFiles(): void {
  fileInput.value?.click()
}

function onFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  const picked = Array.from(input.files ?? [])
  files.value = [...files.value, ...picked.map((f) => ({ name: f.name }))]
  input.value = ''
}

function removeFile(index: number): void {
  files.value = files.value.filter((_, i) => i !== index)
}

async function copyAnswer(index: number, text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    copiedIndex.value = index
    window.setTimeout(() => {
      copiedIndex.value = null
    }, 1200)
  } catch {
    copiedIndex.value = null
  }
}

/** 输入框随内容长高（上限 200px 后自己滚），这是 Cherry 输入栏的手感 */
async function autoGrow(): Promise<void> {
  await nextTick()
  const el = textareaEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`
}

async function scrollToBottom(): Promise<void> {
  await nextTick()
  const el = threadEl.value
  if (el) el.scrollTop = el.scrollHeight
}

function startTicker(): void {
  streamStartedAt = Date.now()
  elapsedMs.value = 0
  if (tick !== null) window.clearInterval(tick)
  tick = window.setInterval(() => {
    elapsedMs.value = Date.now() - streamStartedAt
  }, 200)
}

function stopTicker(): void {
  if (tick !== null) {
    window.clearInterval(tick)
    tick = null
  }
}

/** 把一条会话摘要同步进左栏（新建时先占位，等标题生成后再整表刷新） */
function syncStore(): void {
  const id = conversationId.value
  if (!id) return
  conversations.upsert({
    id,
    title: conversationTitle.value || null,
    model_ref: null,
    project_id: projectId.value,
    created_at: null,
    updated_at: new Date().toISOString(),
    archived: archived.value,
    turn_count: turns.value.filter((turn) => turn.role === 'user').length * 2,
  })
}

// --------------------------------------------------------------------------- //
// 加载已有对话 / 开新对话
// --------------------------------------------------------------------------- //
async function resetToNewConversation(): Promise<void> {
  turns.value = []
  conversationId.value = null
  conversationTitle.value = ''
  projectId.value = pendingProjectId.value
  archived.value = false
  errorText.value = ''
  phase.value = 'idle'
  stopTicker()
  await autoGrow()
}

async function loadConversation(id: string): Promise<void> {
  stopTicker()
  phase.value = 'idle'
  errorText.value = ''
  try {
    const detail = await getConversation(id)
    conversationId.value = detail.id
    conversationTitle.value = detail.title ?? ''
    projectId.value = detail.project_id ?? null
    archived.value = detail.archived
    turns.value = (detail.turns ?? []).map((turn) => ({
      role: turn.role,
      content: turn.content,
      model: turn.model_id,
      durationMs: turn.duration_ms,
      status: turn.interrupted ? 'interrupted' : 'done',
    }))
    await scrollToBottom()
  } catch (error) {
    turns.value = []
    conversationId.value = null
    errorText.value = error instanceof Error ? error.message : String(error)
  }
}

async function init(): Promise<void> {
  const id = route.params.conversationId
  if (typeof id === 'string' && id) await loadConversation(id)
  else await resetToNewConversation()
}

// --------------------------------------------------------------------------- //
// 发送（真流式）
// --------------------------------------------------------------------------- //
interface PickedModel {
  configId: number
  modelId: string
}

/** 解析当前选中的模型；不可用时写 errorText 并返回 null（发送与编辑重开共用同一套校验） */
function pickedModel(): PickedModel | null {
  if (!session.isOwner) {
    errorText.value = writeDenied('发送消息')
    return null
  }
  if (pickedRef.value === 'auto') {
    errorText.value = '请先到「设置 - 模型」填写模型'
    return null
  }
  const [configIdRaw, ...rest] = pickedRef.value.split(':')
  const configId = Number(configIdRaw)
  const modelId = rest.join(':')
  if (!configId || !modelId) {
    errorText.value = '请先到「设置 - 模型」填写模型'
    return null
  }
  return { configId, modelId }
}

/**
 * 跑一轮真流式对话（发送与「编辑重开」共用）。
 *
 * `assistantIndex` = 本地那条等待中的 assistant 轮下标；`replaceFrom` 非空表示编辑重开
 * （后端会先丢弃该条及其后的轮次，再以 `text` 作为新的该条重问）。
 */
async function runStream(
  text: string,
  model: PickedModel,
  assistantIndex: number,
  replaceFrom?: number,
): Promise<void> {
  try {
    await streamChatHome(
      {
        text,
        model_config_id: model.configId,
        model_id: model.modelId,
        conversation_id: conversationId.value ?? undefined,
        project_id: conversationId.value ? undefined : (projectId.value ?? undefined),
        replace_from: replaceFrom,
      },
      {
        onMeta: (meta) => {
          conversationId.value = meta.conversation_id
          projectId.value = meta.project_id
          conversationTitle.value = meta.title ?? ''
          if (meta.turn_count === 0) syncStore()
        },
        onDelta: (delta) => {
          const target = turns.value[assistantIndex]
          if (!target) return
          target.content += delta
          void scrollToBottom()
        },
        onDone: (done) => {
          const target = turns.value[assistantIndex]
          if (!target) return
          if (done.content) target.content = done.content
          target.model = done.model_id
          target.durationMs = done.duration_ms
          target.status = 'done'
          void scrollToBottom()
          // 后端此时已把这一轮落盘：整表刷新一次，左栏顺序/轮次数与磁盘一致
          void conversations.load()
        },
        onTitle: (payload) => {
          conversationTitle.value = payload.title
          syncStore()
          if (payload.title_source === 'fallback' && payload.title_note) {
            errorText.value = payload.title_note
          }
        },
        onError: (streamError) => {
          const target = turns.value[assistantIndex]
          if (target) target.status = 'interrupted'
          errorText.value = `${streamError.code}：${streamError.message}`
          void conversations.load()
        },
      },
    )
  } catch (error) {
    const target = turns.value[assistantIndex]
    if (target) {
      target.status = 'interrupted'
      // 一个字都没生成：普通发送撤掉这条空回答并把原文放回输入框；
      // **编辑重开不回滚** —— 后端已把用户改过的正文落盘，本地回滚反而会和磁盘不一致。
      if (!target.content && replaceFrom === undefined) {
        turns.value = turns.value.filter((_, index) => index !== assistantIndex)
        prompt.value = text
      }
    }
    const withCode = error as { code?: string; message?: string }
    errorText.value = withCode?.message
      ? `${withCode.code ?? 'failed'}：${withCode.message}`
      : error instanceof Error
        ? error.message
        : String(error)
  } finally {
    stopTicker()
    phase.value = 'idle'
  }
}

async function send(): Promise<void> {
  if (!canSend.value || phase.value === 'thinking') return
  const text = prompt.value.trim() || files.value.map((file) => file.name).join('、')
  if (!text) return

  const model = pickedModel()
  if (!model) return

  errorText.value = ''
  turns.value = [
    ...turns.value,
    { role: 'user', content: text, status: 'done' },
    { role: 'assistant', content: '', model: model.modelId, status: 'streaming' },
  ]
  const assistantIndex = turns.value.length - 1
  phase.value = 'thinking'
  prompt.value = ''
  files.value = []
  void autoGrow()
  startTicker()
  await scrollToBottom()
  await runStream(text, model, assistantIndex)
}

// --------------------------------------------------------------------------- //
// 编辑某条用户消息 → 从此处重开
// --------------------------------------------------------------------------- //
const editingIndex = ref<number | null>(null)
const editingText = ref('')
const editConfirmOpen = ref(false)
const editPendingIndex = ref<number | null>(null)

/** 编辑框上限高度（超过就内部滚动），与 autoGrow 的 200px 分开：编辑框通常更长 */
const EDIT_MAX_PX = 320

function sizeEditBox(el: HTMLTextAreaElement): void {
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, EDIT_MAX_PX)}px`
}

/** 输入时自适应高度（拿事件目标即可，不需要 ref） */
function onEditInput(event: Event): void {
  const el = event.target as HTMLTextAreaElement
  sizeEditBox(el)
}

function startEdit(index: number, content: string): void {
  if (phase.value === 'thinking') return
  editingIndex.value = index
  editingText.value = content
  editConfirmOpen.value = false
  editPendingIndex.value = null
  errorText.value = ''
  void nextTick(() => {
    const el = threadEl.value?.querySelector<HTMLTextAreaElement>('.editbox__input')
    if (!el) return
    sizeEditBox(el)
    el.focus()
    // 光标落到末尾，符合"改一改再说"的习惯
    el.setSelectionRange(el.value.length, el.value.length)
  })
}

function cancelEdit(): void {
  editingIndex.value = null
  editingText.value = ''
  editPendingIndex.value = null
  editConfirmOpen.value = false
}

/** 这条消息后面还有轮次吗？有就先弹一次确认（截断会丢弃它们） */
function turnsAfterEdit(index: number): number {
  return Math.max(0, turns.value.length - index - 1)
}

function submitEdit(): void {
  const index = editingIndex.value
  if (index === null) return
  const text = editingText.value.trim()
  if (!text || phase.value === 'thinking') return
  if (turnsAfterEdit(index) > 0) {
    editPendingIndex.value = index
    editConfirmOpen.value = true
    return
  }
  void performEdit(index, text)
}

async function confirmEditRestart(): Promise<void> {
  const index = editPendingIndex.value
  const text = editingText.value.trim()
  editConfirmOpen.value = false
  editPendingIndex.value = null
  if (index === null || !text) return
  await performEdit(index, text)
}

async function performEdit(index: number, text: string): Promise<void> {
  const model = pickedModel()
  if (!model) return

  errorText.value = ''
  // 本地先按同一口径截断：保留 index 之前的轮次，第 index 条换成新正文，再挂一条等待中的回答
  turns.value = [
    ...turns.value.slice(0, index),
    { role: 'user', content: text, status: 'done' },
    { role: 'assistant', content: '', model: model.modelId, status: 'streaming' },
  ]
  const assistantIndex = index + 1
  editingIndex.value = null
  editingText.value = ''
  phase.value = 'thinking'
  startTicker()
  await scrollToBottom()
  await runStream(text, model, assistantIndex, index)
}

// --------------------------------------------------------------------------- //
// 其它入口
// --------------------------------------------------------------------------- //
function goFeed(): void {
  void router.push({ path: '/papers/feed' })
}

function goIdeas(): void {
  void router.push({ path: '/ideas' })
}


function openCreate(prefill: string): void {
  dialogPrefill.value = prefill
  dialogOpen.value = true
}

function onCreated(project: CreatedProject): void {
  void session.loadProjects()
  session.selectProject(project.id)
  void router.push({ name: 'workbench', params: { projectId: String(project.id) } })
}

watch(
  () => route.fullPath,
  () => {
    void init()
  },
)

watch(prompt, () => {
  void autoGrow()
})

onMounted(async () => {
  entranceOn.value = consumeEntrance()
  if (entranceOn.value) {
    // 动画跑完就把标记收掉：类留着虽然不会重播（animation 只跑一次），
    // 但它会一直声称"正在入场"，与真实状态不符；也让后续任何重挂载都不会意外补播。
    entranceTimer = window.setTimeout(() => {
      entranceOn.value = false
    }, 600)
  }
  if (!settings.configs.length) await settings.loadConfigs()
  // 项目名要用来拼开场标题（`/?project=<id>`）：列表空的就先拉一次，避免标题闪成默认文案
  if (!session.projects.length) void session.loadProjects()
  pickedRef.value = modelOptions.value[0]?.value ?? 'auto'
  await init()
})

onUnmounted(() => {
  stopTicker()
  if (entranceTimer !== null) window.clearTimeout(entranceTimer)
})
</script>

<template>
  <section class="chat" :class="{ 'chat--active': active, 'chat--enter': entranceOn }">
    <!-- 开场区展开/收起统一走 .fold（双向高度过渡），替掉原来的 max-height 硬编码
         —— 内容不足 420px 时旧写法会"空跑"一段，收展节奏不匀。 -->
    <div class="fold" :class="{ 'fold--open': !active }">
      <div class="chat__intro">
        <h1 class="chat__title rise-in rise-step-1">{{ heroTitle }}</h1>
        <p class="chat__steps rise-in rise-step-2">{{ PIPELINE }}</p>
      </div>
    </div>

    <!-- 对话态不再显示标题栏：标题在左栏会话行上（已高亮），「流水线工作台」入口移到左栏项目行的 ⋯ 菜单，
         消息区因此直接顶上，可用高度更大。 -->
    <div v-if="active" ref="threadEl" class="thread scroll-y">
      <template v-for="(turn, index) in turns" :key="index">
        <div v-if="turn.role === 'user'" class="turn turn--user">
          <!-- 编辑态：原地把这条消息换成编辑框（不另开弹窗），改完直接从这里重开 -->
          <div v-if="editingIndex === index" class="editbox">
            <textarea
              v-model="editingText"
              rows="1"
              class="editbox__input"
              aria-label="修改这条消息"
              @input="onEditInput"
              @keydown.esc="cancelEdit"
            />
            <p class="editbox__impact">
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <circle cx="8" cy="8" r="6.2" stroke="currentColor" stroke-width="1.3" />
                <path d="M8 7.2v4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
                <circle cx="8" cy="4.9" r="0.85" fill="currentColor" />
              </svg>
              编辑后将从此处重新开始对话，后续问答会被丢弃，已有产物不受影响
            </p>
            <div class="editbox__acts">
              <button class="ghost" type="button" @click="cancelEdit">取消</button>
              <button
                class="editbox__send"
                type="button"
                :disabled="!editingText.trim() || phase === 'thinking'"
                @click="submitEdit"
              >
                发送
              </button>
            </div>
          </div>

          <template v-else>
            <div class="bubble">{{ turn.content }}</div>
            <div class="acts acts--right">
              <button
                class="icon-btn"
                type="button"
                :title="copiedIndex === index ? '已复制' : '复制'"
                :aria-label="copiedIndex === index ? '已复制' : '复制'"
                @click="copyAnswer(index, turn.content)"
              >
                <svg
                  v-if="copiedIndex !== index"
                  width="14"
                  height="14"
                  viewBox="0 0 16 16"
                  fill="none"
                  aria-hidden="true"
                >
                  <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.3" />
                  <path
                    d="M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"
                    stroke="currentColor"
                    stroke-width="1.3"
                    stroke-linecap="round"
                  />
                </svg>
                <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path
                    d="M3 8.5 6.5 12 13 4.5"
                    stroke="currentColor"
                    stroke-width="1.6"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  />
                </svg>
              </button>
              <button
                class="icon-btn"
                type="button"
                title="编辑"
                aria-label="编辑"
                @click="startEdit(index, turn.content)"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path
                    d="M11.4 2.6a1.35 1.35 0 0 1 1.9 1.9l-7.6 7.6-2.7.8.8-2.7 7.6-7.6Z"
                    stroke="currentColor"
                    stroke-width="1.3"
                    stroke-linejoin="round"
                  />
                  <path d="M10.3 3.7 12.3 5.7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </template>
        </div>
        <div v-else class="turn turn--assistant">
          <div class="meta">
            <span class="meta__model">{{ turn.model }}</span>
            <span class="meta__time">{{ durationText(turn) }}</span>
          </div>
          <MarkdownText v-if="turn.content" :content="turn.content" />
          <div v-else class="dots" aria-label="正在生成">
            <span class="dot" />
            <span class="dot" />
            <span class="dot" />
          </div>
          <div v-if="turn.status === 'interrupted'" class="cut">已中断 / 出错</div>
          <div v-if="turn.content" class="acts">
            <button
              class="icon-btn"
              type="button"
              :title="copiedIndex === index ? '已复制' : '复制回答'"
              :aria-label="copiedIndex === index ? '已复制' : '复制回答'"
              @click="copyAnswer(index, turn.content)"
            >
              <svg v-if="copiedIndex !== index" width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" stroke="currentColor" stroke-width="1.3" />
                <path
                  d="M10.5 5.5V4a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"
                  stroke="currentColor"
                  stroke-width="1.3"
                  stroke-linecap="round"
                />
              </svg>
              <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M3 8.5 6.5 12 13 4.5"
                  stroke="currentColor"
                  stroke-width="1.6"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </button>
          </div>
        </div>
      </template>
    </div>

    <p v-if="errorText" class="state state--error">{{ errorText }}</p>

    <!-- 研究流程（七节点 + 程序校验 + 迁移留痕）。放在输入栏上方：
         项目内不论是新对话还是打开已有对话，它都在同一条内容列上。 -->
    <ResearchFlowPanel :conversation-id="conversationId" />

    <div class="composer rise-in rise-step-3" :class="{ 'composer--hero': !active }">
      <textarea
        ref="textareaEl"
        v-model="prompt"
        rows="1"
        placeholder="描述你的研究需求，例如：为长上下文问答设计一套可复现的评测方案"
        @keydown.enter.exact.prevent="send"
      />

      <div class="bar">
        <div class="bar__left">
          <button class="icon-btn" type="button" title="上传文件" aria-label="上传文件" @click="pickFiles">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
          </button>
          <input ref="fileInput" type="file" multiple hidden @change="onFiles" />
          <button
            v-for="(file, index) in files"
            :key="`${file.name}-${index}`"
            class="chip"
            type="button"
            :title="`移除 ${file.name}`"
            @click="removeFile(index)"
          >
            {{ file.name.length > 20 ? `${file.name.slice(0, 18)}…` : file.name }}
          </button>
        </div>

        <div class="bar__right">
          <div class="mpick">
            <button
              class="mpick__trigger"
              type="button"
              aria-haspopup="listbox"
              :aria-expanded="pickerOpen ? 'true' : 'false'"
              @click="pickerOpen = !pickerOpen"
            >
              <span class="mpick__value">{{ pickedLabel }}</span>
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden="true">
                <path d="M2 4l3 3 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
            <ul v-if="pickerOpen" class="mpick__panel" role="listbox">
              <li
                v-for="option in modelOptions"
                :key="option.value"
                class="mpick__option"
                :class="{ 'mpick__option--on': option.value === pickedRef }"
                role="option"
                :aria-selected="option.value === pickedRef ? 'true' : 'false'"
                @click="pickModel(option.value)"
              >
                <span class="mpick__label">{{ option.label }}</span>
                <svg
                  v-if="option.value === pickedRef"
                  width="12"
                  height="12"
                  viewBox="0 0 12 12"
                  fill="none"
                  aria-hidden="true"
                >
                  <path
                    d="M2.5 6.5 5 9l4.5-6"
                    stroke="currentColor"
                    stroke-width="1.6"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  />
                </svg>
              </li>
            </ul>
          </div>
          <button
            class="send press"
            type="button"
            :disabled="!canSend || phase === 'thinking'"
            :title="phase === 'thinking' ? '正在生成' : '发送'"
            aria-label="发送"
            @click="send"
          >
            <svg width="16" height="16" viewBox="0 0 18 18" fill="none" aria-hidden="true">
              <path
                d="M9 15V4M9 4 4.5 8.5M9 4l4.5 4.5"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <div class="fold" :class="{ 'fold--open': !active }">
      <section class="cards rise-in rise-step-4">
        <button class="card press" type="button" @click="openCreate(prompt)">
        <span class="card__icon">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path d="M9 3v12M3 9h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
        </span>
        <span class="card__title">新建研究项目</span>
        <span class="card__desc">快速开始新的研究</span>
      </button>

      <button class="card press" type="button" @click="goFeed">
        <span class="card__icon">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <circle cx="8" cy="8" r="4.5" stroke="currentColor" stroke-width="1.6" />
            <path d="M11.4 11.4 15 15" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
        </span>
        <span class="card__title">文献调研</span>
        <span class="card__desc">聚合检索文献，输出结构化分析总结</span>
      </button>

      <button class="card press" type="button" @click="goIdeas">
        <span class="card__icon">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path
              d="M6 3.5h6l1.5 4-4.5 2.5L4.5 7.5 6 3.5Z"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linejoin="round"
            />
            <path d="M9 10v5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
        </span>
        <span class="card__title">idea 生成</span>
        <span class="card__desc">生成带证据和可行性评估的研究构想</span>
      </button>
      </section>
    </div>

    <ProjectCreateDialog v-model="dialogOpen" :prefill="dialogPrefill" @created="onCreated" />
    <ConfirmDialog
      v-model="editConfirmOpen"
      title="从这条消息重新开始"
      :message="`编辑后将从这条消息处重新开始对话，它后面的 ${turnsAfterEdit(editPendingIndex ?? 0)} 条消息（含问答）会被丢弃。已有产物不受影响。`"
      confirm-text="重新发送"
      @confirm="confirmEditRestart"
    />
  </section>
</template>

<style scoped>
.chat {
  width: 100%;
  max-width: 880px;
  display: flex;
  flex-direction: column;
  min-height: 100%;
  padding: 64px 32px 0;
}

/* 有对话时：hero 收起，thread 吃掉剩余高度，输入栏留在文档流最后一行
   —— 这样滚动区的下边界天然就是输入栏上沿，中间不留空档。 */
.chat--active {
  height: 100%;
  min-height: 0;
  padding-bottom: 0;
  overflow: hidden;
}

/* 开场区与三张卡各自套一个 .fold：两个包装器都是 `.chat` 的 flex 子项，必须 flex:none ——
   默认可收缩会把它们压到比内容矮，配合折叠裁剪就表现为「输入框盖住开场文案」（2026-09-20 实测踩过）。 */
.chat > .fold {
  flex: none;
}

/* 只做「入场」：进入首页时逐级自下而上淡入（@keyframes rise-in 在 styles/motion.css，
   全站共用一份）。非入场场景（点 ＋ 新建、硬刷新）把 .rise-in 关掉，元素就是静态的。 */
.chat:not(.chat--enter) .rise-in {
  animation: none;
}

.chat__title {
  margin: 0 0 16px;
  font-family: geomanist, 'Open Sans', ui-sans-serif, system-ui, sans-serif;
  font-size: var(--font-size-4xl);
  line-height: 1.1;
  font-weight: 600;
  letter-spacing: -0.5px;
  color: var(--h-fg);
}

.chat__steps {
  margin: 0 0 40px;
  font-size: var(--font-size-lg);
  color: var(--h-fg-muted);
}

.ghost {
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.ghost:hover {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

/* ---------- 消息区 ---------- */
.thread {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 22px;
  padding: 18px 0 6px;
}

.turn {
  display: flex;
  flex-direction: column;
}

/* 用户消息：一律右对齐（此前用 :first-child 控制，只有第一条生效） */
.turn--user {
  align-items: flex-end;
}

.bubble {
  max-width: 76%;
  padding: 10px 16px;
  border-radius: 12px;
  background: var(--h-active);
  color: var(--h-fg);
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 助手回答：无气泡、无边框，直接输出 */
.turn--assistant {
  align-items: stretch;
  gap: 8px;
}

.meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.meta__model {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--h-fg);
}

.meta__time {
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.acts {
  display: flex;
  gap: 6px;
  margin-top: 2px;
}

/* 用户消息的操作行：与气泡同侧（右对齐）。
   助手那条保持左对齐 —— 两侧各自贴着自己的内容，视线不用横跳。 */
.acts--right {
  justify-content: flex-end;
  gap: 4px;
}

/* ---------- 就地编辑某条用户消息 ---------- */
.editbox {
  /* 编辑态切到"整列宽 + 左对齐文字"：既贴近原文幅面，又够地方改长文；
     底色沿用用户气泡色，一眼看出"还是我那条消息"。 */
  width: 100%;
  display: flex;
  flex-direction: column;
  padding: 12px 14px 10px;
  border: 1px solid var(--h-line-strong);
  border-radius: 14px;
  background: var(--h-active);
}

.editbox__input {
  width: 100%;
  min-height: 44px;
  max-height: 320px;
  overflow-y: auto;
  resize: none;
  border: 0;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  line-height: 1.65;
  outline: none;
}

/* 「后续问答会被丢弃」是真实后果，不是装饰性小字 —— 用警示色，别混在灰字里被忽略 */
.editbox__impact {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 0 0;
  color: var(--color-warning);
  font-size: var(--font-size-xs);
}

.editbox__acts {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
}

.editbox__send {
  height: 30px;
  padding: 0 16px;
  border: 0;
  border-radius: 40px;
  background: var(--h-fg);
  /* 底色是 --h-fg：反色文字必须用 --h-page-bg。
     ⚠️ 写成 var(--h-bg) 会因该令牌不存在而在计算时整条失效 → 退化成继承色，黑底黑字看不见。 */
  color: var(--h-page-bg);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    opacity var(--motion-dur-fast) var(--motion-ease),
    background-color var(--motion-dur-fast) var(--motion-ease);
}

.editbox__send:hover:not(:disabled) {
  opacity: 0.88;
}

.editbox__send:disabled {
  opacity: 0.4;
  cursor: default;
}

.cut {
  align-self: flex-start;
  padding: 2px 10px;
  border: 1px solid var(--h-line-strong);
  border-radius: 999px;
  color: var(--h-fg-muted);
  font-size: var(--font-size-xs);
}

.dots {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 24px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--h-fg-subtle);
  animation: dot-bounce 1.1s cubic-bezier(0.4, 0, 0.2, 1) infinite;
}

.dot:nth-child(2) {
  animation-delay: 0.15s;
}

.dot:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes dot-bounce {
  0%,
  80%,
  100% {
    transform: translateY(0);
    opacity: 0.45;
  }
  40% {
    transform: translateY(-5px);
    opacity: 1;
  }
}

.state--error {
  flex: none;
  margin: 8px 0 0;
  padding: 8px 12px;
  border-left: 3px solid var(--h-primary);
  border-radius: 8px;
  background: var(--h-hover);
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
}

/* ---------- 输入栏（Cherry 口径：整体一块圆角容器，上输入下工具栏） ---------- */
.composer {
  flex: none;
  /* 与上方标题 / 流程行 / 三张卡**同宽同左边界**：它们都占满 816px 的内容列，
     输入框若收窄居中就会左右各缩进 48px，看起来"和上面的字对不齐"（2026-09-21 修）。 */
  width: 100%;
  padding: 12px 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line-strong);
  border-radius: 18px;
  transition:
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.composer--hero {
  margin-bottom: 40px;
}

.composer:focus-within {
  border-color: var(--h-primary);
}

.composer textarea {
  width: 100%;
  height: 26px;
  max-height: 200px;
  padding: 0 4px;
  border: 0;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-lg);
  line-height: 1.6;
  resize: none;
  outline: none;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.composer textarea::placeholder {
  color: var(--h-fg-subtle);
}

.composer textarea::-webkit-scrollbar {
  width: 6px;
}

.composer textarea::-webkit-scrollbar-track {
  background: transparent;
}

.composer textarea::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}

.composer textarea::-webkit-scrollbar-thumb {
  background: var(--h-line-strong);
  border: 0;
  border-radius: 40px;
}

.composer textarea::-webkit-scrollbar-thumb:hover {
  background: var(--h-fg-subtle);
}

.bar {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
}

.bar__left,
.bar__right {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.bar__left {
  flex: 1;
  flex-wrap: wrap;
}

.bar__right {
  flex: none;
  margin-left: auto;
}

.chip {
  max-width: 180px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: var(--h-hover);
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.chip:hover {
  border-color: var(--h-line-strong);
  color: var(--h-fg);
}

.icon-btn {
  flex: none;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--h-fg-subtle);
  cursor: pointer;
  transition:
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.icon-btn:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

.send {
  flex: none;
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 50%;
  background: var(--h-primary);
  color: var(--h-primary-fg);
  cursor: pointer;
  transition:
    transform 180ms cubic-bezier(0.4, 0, 0.2, 1),
    opacity 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.send:not(:disabled):hover {
  transform: translateY(-1px);
}

/* 模型选择：只有「名字 + ⌄」，无外框；浮层向上展开 */
.mpick {
  position: relative;
}

.mpick__trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 200px;
  height: 28px;
  padding: 0 8px;
  border: 0;
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition:
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.mpick__trigger:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

.mpick__value {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mpick__panel {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 40;
  min-width: 200px;
  max-width: 320px;
  max-height: 260px;
  overflow-y: auto;
  margin: 0;
  padding: 6px;
  list-style: none;
  background: var(--h-surface-raised);
  border: 1px solid var(--h-line-strong);
  border-radius: 12px;
  box-shadow: 0 12px 32px rgb(0 0 0 / 24%); /* ui-polish-allow: 浮层投影色 */
  /* 浮层入场走全站统一的 .pop-in（styles/motion.css），本地不再自定义 keyframes */
  animation: pop-in var(--motion-dur-fast) var(--motion-ease-out);
}

.mpick__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  color: var(--h-fg);
  cursor: pointer;
  transition: background-color 140ms cubic-bezier(0.4, 0, 0.2, 1);
}

.mpick__option:hover {
  background: var(--h-hover);
}

.mpick__option--on {
  color: var(--h-primary);
}

.mpick__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 三张快捷卡 ---------- */
.cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  /* padding-top 是给 hover 抬升留的余量：外层 .fold 会给直接子元素加 overflow:hidden，
     卡片 hover 上移 2px 会被裁掉顶上一条，留 2px 正好；margin 相应减 2px，版面不变。 */
  padding-top: 2px;
  margin-bottom: 46px;
}

.card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 24px;
  border: 1px solid var(--h-line);
  border-radius: 16px;
  background: var(--h-surface-raised);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    transform 180ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}

.card:hover {
  transform: translateY(-2px);
  border-color: var(--h-line-strong);
}

.card__icon {
  width: 36px;
  height: 36px;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--h-active);
  color: var(--h-primary);
}

.card__title {
  margin-bottom: 8px;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.card__desc {
  font-size: var(--font-size-md);
  line-height: 1.5;
  color: var(--h-fg-subtle);
}

@media (max-width: 900px) {
  .chat {
    padding: 32px 16px 0;
  }

  .chat__title {
    font-size: var(--font-size-3xl);
  }

  .cards {
    grid-template-columns: 1fr;
  }
}
</style>
