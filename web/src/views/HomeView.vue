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
import { humanError, writeDenied } from '@/utils/messages'
import { useRoute, useRouter } from 'vue-router'

import { fetchAccessMode, isApprovalCard, setAccessMode, streamApprovalDecision, streamChatHome } from '@/api/chat'
import type {
  ApprovalCard,
  ApprovalDecision,
  ChatBlock,
  StreamDone,
  StreamHandlers,
  TurnRow,
} from '@/api/chat'
import { ApiError } from '@/api/client'
import { getConversation } from '@/api/conversations'
import MarkdownText from '@/components/MarkdownText.vue'
import ConfirmDialog from '@/components/home/ConfirmDialog.vue'
import ResearchFlowDrawer from '@/components/home/ResearchFlowDrawer.vue'
import ProjectCreateDialog from '@/components/ProjectCreateDialog.vue'
import type { CreatedProject } from '@/api/projects'
import { useConversationStore } from '@/stores/conversations'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'
import { consumeEntrance } from '@/utils/pageEntrance'

/** `waiting` = 这一轮没有答完，**在等研究者批准**（不是"已完成"） */
type TurnStatus = 'streaming' | 'waiting' | 'done' | 'interrupted'

interface Turn {
  role: 'user' | 'assistant'
  content: string
  model?: string
  durationMs?: number
  status?: TurnStatus
  /** 结构化块：可点选项（引导词）与结果卡片（本地查询） */
  blocks?: ChatBlock[]
  /** 过程行：节点执行 / 工具调用 / 批准卡 */
  rows?: TurnRow[]
  /** 思考过程：**不是答复**，折叠展示在耗时那一行下面 */
  reasoning?: string
  /** 本轮的判定结果（执行 / 引导 / 查询 / 普通对话），用于角标 */
  routing?: string
  /** 待研究者裁决的批准请求（非空 = 界面要停在这里等人） */
  awaiting?: ApprovalCard | null
  /** 正文是**系统说明**而不是模型说的话（`note_only`）→ 有卡时就不重复渲染 */
  noteOnly?: boolean
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
  // 停在批准上**不能说"已完成"**：这一轮其实还没答完，人没裁决之前它不会往前走
  if (turn.status === 'waiting') return '等待研究者批准'
  if (turn.durationMs) return `已完成 ${fmtDuration(turn.durationMs)}`
  return ''
}

/** 思考过程折叠入口的文案：进行中「思考中…」，结束后「已思考 Ns」 */
function reasonLabel(turn: Turn): string {
  if (turn.status === 'streaming') return '思考中…'
  return turn.durationMs ? `已思考 ${fmtDuration(turn.durationMs)}` : '思考过程'
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
    // 授权是**按对话**的：打开这个对话就把它自己的开关状态读回来
    void loadAccessMode(detail.id)
    turns.value = (detail.turns ?? []).map((turn) => {
      // 磁盘里存的结构化块 / 过程行 / 思考过程要一并还原，
      // 否则刷新后引导词的选项、节点的过程行、思考折叠都会消失。
      const rows = (turn as { rows?: TurnRow[] }).rows
      // 批准卡也在过程行里 —— 所以刷新后它照样在，并且还能接着裁决；
      // 后端才是真正认账的地方（一次性、带有效期），这里只负责显示。
      const pending = (rows ?? []).find(
        (row) => isApprovalCard(row) && row.status === 'pending',
      ) as ApprovalCard | undefined
      const status: TurnStatus = pending
        ? 'waiting'
        : (turn as { interrupted?: boolean }).interrupted
          ? 'interrupted'
          : 'done'
      return {
        role: turn.role,
        content: turn.content,
        model: turn.model_id,
        durationMs: turn.duration_ms,
        status,
        blocks: (turn as { blocks?: ChatBlock[] }).blocks,
        rows,
        reasoning: (turn as { reasoning?: string }).reasoning ?? undefined,
        routing: (turn as { routing?: string }).routing,
        awaiting: pending ?? null,
        noteOnly: (turn as { note_only?: boolean }).note_only === true,
      }
    })
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

/** 同一 request_id 只留一张卡：后端在裁决后会把**同一张卡**以新状态再发一次。 */
function upsertApprovalRow(rows: TurnRow[], card: ApprovalCard): TurnRow[] {  const index = rows.findIndex((row) => isApprovalCard(row) && row.request_id === card.request_id)
  if (index < 0) return [...rows, card]
  const next = [...rows]
  next[index] = card
  return next
}

/**
 * 一轮流式回答的公共处理。
 *
 * 发送 / 编辑重开 / **批准后续答**三处共用同一套口径 —— 三份复制粘贴迟早会分叉，
 * 而"显示与落盘不一致"正是这个项目已经栽过三次的坑。
 *
 * `approvalIndex` = **卡片所在那一轮**的下标。批准/拒绝的后续流渲染在新一轮里，
 * 但卡片必须回到原来那一轮换状态 —— 否则同一张卡会在两轮里各出现一次。
 */
function streamHandlers(assistantIndex: number, approvalIndex = assistantIndex): StreamHandlers {
  const target = (): Turn | undefined => turns.value[assistantIndex]
  const cardOwner = (): Turn | undefined => turns.value[approvalIndex]
  return {
    onMeta: (meta) => {
      conversationId.value = meta.conversation_id
      projectId.value = meta.project_id
      conversationTitle.value = meta.title ?? ''
      if (meta.turn_count === 0) syncStore()
      // 新会话刚拿到 id：如果用户在开聊之前就拨了开关，这里把它补写到这个会话上
      if (pendingFullAccess !== null) void loadAccessMode(meta.conversation_id)
    },
    onDelta: (delta) => {
      const current = target()
      if (!current) return
      current.content += delta
      void scrollToBottom()
    },
    onReasoning: (delta) => {
      // 思考过程**不拼进正文**：单独累积，渲染时折叠在耗时那一行下面
      const current = target()
      if (!current) return
      current.reasoning = (current.reasoning ?? '') + delta
      void scrollToBottom()
    },
    onRow: (row) => {
      const current = target()
      if (!current) return
      // 批准卡也走 row 通道落盘，这里按 id 去重，免得同一张卡出现两次
      current.rows = isApprovalCard(row)
        ? upsertApprovalRow(current.rows ?? [], row)
        : [...(current.rows ?? []), row]
      void scrollToBottom()
    },
    onApproval: (card) => {
      const owner = cardOwner()
      if (!owner) return
      owner.rows = upsertApprovalRow(owner.rows ?? [], card)
      // 只有 pending 才停在等人；批准/拒绝后同一张卡换成终态，不再拦着界面
      owner.awaiting = card.status === 'pending' ? card : null
      void scrollToBottom()
    },
    onBlocks: (blocks) => {
      const current = target()
      if (!current) return
      current.blocks = [...(current.blocks ?? []), ...blocks]
      void scrollToBottom()
    },
    onDone: (done) => {
      const current = target()
      if (!current) return
      if (done.content) current.content = done.content
      if (done.model_id) current.model = done.model_id
      if (done.duration_ms) current.durationMs = done.duration_ms
      if (done.routing) current.routing = done.routing
      // **等批准 ≠ 答完**：这一轮停在人那里，不能标成"已完成"
      current.awaiting = done.awaiting_approval ?? null
      current.status = current.awaiting ? 'waiting' : 'done'
      void scrollToBottom()
      // 后端此时已把这一轮落盘：整表刷新一次，左栏顺序/轮次数与磁盘一致
      void conversations.load()
      maybeAutoContinue(done)
    },
    onTitle: (payload) => {
      conversationTitle.value = payload.title
      syncStore()
      if (payload.title_source === 'fallback' && payload.title_note) {
        errorText.value = payload.title_note
      }
    },
    onError: (streamError) => {
      const current = target()
      if (current) current.status = 'interrupted'
      errorText.value = humanError(streamError)
      void conversations.load()
    },
  }
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
      streamHandlers(assistantIndex),
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
      ? humanError(withCode)
      : error instanceof Error
        ? error.message
        : String(error)
  } finally {
    stopTicker()
    phase.value = 'idle'
  }
}

/**
 * 研究者裁决一次批准请求（点「批准」或「拒绝」）。
 *
 * **批准入口只对 Owner 开放**，与后端 Owner 校验对齐：前端这层是提前拦住，
 * 真正的边界在后端 —— 这里放行不等于后端会放行。
 */
/**
 * 待研究者裁决的那张卡 —— **贴在输入框上方**显示（研究者 2026-09-22：
 * 批准不该出现在内容输出区）。批准/拒绝后它就不再是 pending，这条自然消失。
 */
const pendingApprovalBar = computed<{ turnIndex: number; card: ApprovalCard } | null>(() => {
  for (let index = turns.value.length - 1; index >= 0; index -= 1) {
    const card = (turns.value[index].rows ?? []).find(
      (row) => isApprovalCard(row) && row.status === 'pending',
    )
    if (card && isApprovalCard(card)) return { turnIndex: index, card }
  }
  return null
})

/**
 * 第一行只显示"要执行的东西"本身。
 *
 * 旧记录里存的是 `{"command": "pwd && ls"}` 这种 JSON —— 括号和键名是给机器看的，
 * 研究者只要看到 `pwd && ls`。新记录由服务端直接给命令，这里只是兜底解包。
 */
function approvalCommand(preview: string): string {
  const text = (preview ?? '').trim()
  if (!text.startsWith('{')) return text
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>
    const value = parsed.command ?? parsed.argv
    if (typeof value === 'string') return value
    if (Array.isArray(value)) return value.map((item) => String(item)).join(' ')
  } catch {
    // 不是 JSON 就原样显示（宁可看到原文，也不要因为解析失败而变成空白）
  }
  return text
}

/**
 * 「已经批完/拒完」的过程行 —— 不在正文里显示（研究者 2026-09-22：
 * 批准之后不需要再输出一句"已批准"）。判定只认这几种固定话术，不影响真正的执行结果行。
 */
function isSettledApprovalRow(row: TurnRow): boolean {
  if (isApprovalCard(row)) return false
  const text = (row.text ?? '').trim()
  return (
    text.startsWith('研究者已批准') ||
    text.startsWith('研究者已拒绝') ||
    text.startsWith('已允许在本对话内直接执行')
  )
}

async function decideApprovalCard(card: ApprovalCard, decision: ApprovalDecision): Promise<void> {
  if (phase.value === 'thinking') return
  if (!session.isOwner) {
    const action =
      decision === 'deny'
        ? '拒绝执行'
        : decision === 'approve_conversation'
          ? '允许本对话内直接执行'
          : '批准执行'
    errorText.value = writeDenied(action)
    return
  }
  const id = conversationId.value
  if (!id) {
    errorText.value = '会话还没建立，无法裁决'
    return
  }
  // 拒绝路径不调模型，所以不需要模型可用；批准（含"本对话默认允许"）要续答，必须先有模型
  const needsModel = decision === 'approve' || decision === 'approve_conversation'
  const model = needsModel ? pickedModel() : null
  if (needsModel && !model) return

  errorText.value = ''
  phase.value = 'thinking'
  startTicker()
  const assistantIndex = turns.value.length
  // 卡片所在的那一轮：裁决流渲染在新一轮，但卡片要回到这里换状态
  const cardIndex = turns.value.findIndex((turn) =>
    (turn.rows ?? []).some((row) => isApprovalCard(row) && row.request_id === card.request_id),
  )
  // 裁决的后续动作（执行 / 拒绝说明 / 续答）**作为新一轮**流式回来，
  // 与卡片所在的那一轮分开显示：卡片留在原地换状态，回答新起一条。
  turns.value = [...turns.value, { role: 'assistant', content: '', status: 'streaming', rows: [] }]
  await scrollToBottom()

  try {
    await streamApprovalDecision(
      {
        conversation_id: id,
        request_id: card.request_id,
        decision,
        model_config_id: model?.configId,
        model_id: model?.modelId,
      },
      streamHandlers(assistantIndex, cardIndex >= 0 ? cardIndex : assistantIndex),
    )
  } catch (error) {
    const target = turns.value[assistantIndex]
    const withCode = error as { code?: string; message?: string }
    if (target && !target.content && !(target.rows ?? []).length) {
      turns.value = turns.value.filter((_, index) => index !== assistantIndex)
    } else if (target) {
      target.status = 'interrupted'
    }
    // 409 / 404 = 这条请求已经不在待批状态（批过 / 拒过 / 过期）：如实说出来，
    // 同时**按后端的说法把卡片收掉**，免得界面上留着一张永远点不动的卡
    const code = withCode?.code ?? ''
    if (code.startsWith('approval_')) {
      const closed: ApprovalCard = {
        ...card,
        status: code === 'approval_approved' ? 'approved' : code === 'approval_denied' ? 'denied' : 'expired',
      }
      const owner = turns.value.find((turn) =>
        (turn.rows ?? []).some((row) => isApprovalCard(row) && row.request_id === card.request_id),
      )
      if (owner) {
        owner.rows = upsertApprovalRow(owner.rows ?? [], closed)
        owner.awaiting = null
      }
    }
    errorText.value = withCode?.message
      ? humanError(withCode)
      : error instanceof Error
        ? error.message
        : String(error)
  } finally {
    stopTicker()
    phase.value = 'idle'
  }
}

/** 本轮里是否还有待裁决的卡（有就**不要**显示"正在生成"的省略号） */
function pendingCard(turn: Turn): ApprovalCard | null {
  return turn.awaiting ?? null
}

/** 本轮里有没有批准卡（有卡时，那句"只输出了思考过程"的说明就是多余的噪声） */
function hasApprovalCard(turn: Turn): boolean {
  return (turn.rows ?? []).some((row) => isApprovalCard(row))
}

/**
 * 正文该不该渲染。
 *
 * `note_only` 是**系统说明**（模型没给正文时的如实交代），不是模型说的话；
 * 而这一轮已经有卡了 —— 卡片本身就说明了"停在哪、等谁"，再叠一句说明只是噪声。
 */
function showContent(turn: Turn): boolean {
  if (!turn.content) return false
  return !(turn.noteOnly && hasApprovalCard(turn))
}

/* ------------------------------------------------------------------ *
 * 思考过程的折叠（展示在耗时那一行下面）
 * 它**不是答复**：后端走独立通道送到这里，与正文完全分开。
 * ------------------------------------------------------------------ */
const reasonOpen = ref<Set<number>>(new Set())

function isReasonOpen(index: number): boolean {
  return reasonOpen.value.has(index)
}

function toggleReason(index: number): void {
  const next = new Set(reasonOpen.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  reasonOpen.value = next
}

/* ------------------------------------------------------------------ *
 * 引导词的可点选项：点一下就把它对应的那句话发出去
 * ------------------------------------------------------------------ */
function pickOption(text: string): void {
  if (phase.value === 'thinking') return
  prompt.value = text
  void send()
}

/* ------------------------------------------------------------------ *
 * 完全访问模式（输入栏开关，默认关）
 *
 * 一个开关管两件事：① 节点跑完自动接力下一站；② **普通动手操作不再逐一确认**
 * （跑命令 / 写文件 / 删文件）。高危操作无论开关如何都会先弹批准卡。
 * 授权**按对话**存在后端（`/chat/access-mode`），浏览器本地只是"还没有会话时"的暂存。
 * ------------------------------------------------------------------ */
const FULL_ACCESS_KEY = 'sciloop.fullAccess'
const fullAccess = ref(localStorage.getItem(FULL_ACCESS_KEY) === '1')
/** 后端给的授权状态说明（口径只维护一份，界面直接显示它） */
const accessNote = ref('')
/** 本地暂存过、但还没落到任何会话上的开关值（新建会话后补写） */
let pendingFullAccess: boolean | null = null

function toggleFullAccess(): void {
  fullAccess.value = !fullAccess.value
  localStorage.setItem(FULL_ACCESS_KEY, fullAccess.value ? '1' : '0')
  void syncFullAccess()
}

/**
 * 把开关同步到后端（授权按对话存）。
 *
 * 还没有会话时先记在 `pendingFullAccess` 里 —— 用户是在"开始对话之前"拨的开关，
 * 等第一轮对话拿到会话 id 再补写，不能因此丢了他的选择。
 */
async function syncFullAccess(): Promise<void> {
  const id = conversationId.value
  if (!id) {
    pendingFullAccess = fullAccess.value
    return
  }
  if (!session.isOwner) {
    accessNote.value = '切到研究者身份后才能改这个开关'
    return
  }
  try {
    const state = await setAccessMode(id, fullAccess.value)
    accessNote.value = state.note
    pendingFullAccess = null
  } catch (error) {
    // 如实说：本地拨了但后端没记上，别让界面显示成"已开"
    accessNote.value =
      error instanceof ApiError ? `开关没改成：${error.message}` : '开关没改成（网络异常）'
  }
}

/** 打开某个对话时，读回它自己的授权状态（换对话就要重新决定）。 */
async function loadAccessMode(id: string): Promise<void> {
  try {
    const state = await fetchAccessMode(id)
    if (pendingFullAccess !== null && state.full_access !== pendingFullAccess) {
      // 用户在"还没有会话"时拨过开关 → 把它补写到这个会话上
      await syncFullAccess()
      return
    }
    fullAccess.value = state.full_access
    localStorage.setItem(FULL_ACCESS_KEY, state.full_access ? '1' : '0')
    accessNote.value = state.note
  } catch {
    accessNote.value = ''
  }
}

/**
 * 只在「节点真的跑完，且下一节点真的实装」时才自动继续。
 *
 * 三种情况一律停住：节点没跑完（转人工 / 失败）、没有下一节点、下一节点是占位。
 * 尤其是占位节点——让它们依次「通过」等于伪造「实验做完了、论文写好了」。
 */
function maybeAutoContinue(done: StreamDone): void {
  if (!fullAccess.value) return
  if (done.routing !== 'node') return
  if (done.node_status !== 'done') return
  if (!done.next_node || done.next_node === 'end') return
  if (done.next_implemented === false) return
  setTimeout(() => {
    if (phase.value !== 'idle') return
    prompt.value = '继续'
    void send()
  }, 600)
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
          <!-- 思考过程：折叠在耗时那一行下面，**不是答复** -->
          <div v-if="turn.reasoning" class="reason">
            <button
              class="reason__head"
              type="button"
              :aria-expanded="isReasonOpen(index)"
              @click="toggleReason(index)"
            >
              <svg
                class="reason__caret"
                :class="{ 'reason__caret--open': isReasonOpen(index) }"
                width="10"
                height="10"
                viewBox="0 0 10 10"
                aria-hidden="true"
              >
                <path d="M3 1.5 6.5 5 3 8.5" stroke="currentColor" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <span>{{ reasonLabel(turn) }}</span>
            </button>
            <div class="fold" :class="{ 'fold--open': isReasonOpen(index) }">
              <pre class="reason__body">{{ turn.reasoning }}</pre>
            </div>
          </div>
          <!-- 过程行：节点执行 / 工具调用 / **批准卡**（先于结论出现） -->
          <div v-if="turn.rows?.length" class="rows">
            <template v-for="(row, rowIndex) in turn.rows" :key="rowIndex">
              <!-- 批准卡**不在正文里显示**：它贴在输入框上方（见 .approval-bar）；
                   批准/拒绝的过程也不在正文留痕（研究者 2026-09-22 定的）。 -->
              <div
                v-if="!isApprovalCard(row) && !isSettledApprovalRow(row)"
                class="row"
                :class="`row--${row.tone ?? 'idle'}`"
              >
                <span class="row__kind">{{ row.label }}</span>
                <span class="row__text">{{ row.text }}</span>
              </div>
            </template>
          </div>
          <MarkdownText v-if="showContent(turn)" :content="turn.content" />
          <!-- 等批准时**不要转省略号**：它不是"正在生成"，是停着等人 -->
          <div v-else-if="!pendingCard(turn) && !turn.content" class="dots" aria-label="正在生成">
            <span class="dot" />
            <span class="dot" />
            <span class="dot" />
          </div>
          <!-- 结构化块：引导词的可点出口 / 本地查询的结果卡片 -->
          <div v-if="turn.blocks?.length" class="blocks">
            <template v-for="(block, blockIndex) in turn.blocks" :key="blockIndex">
              <div v-if="block.kind === 'choice'" class="choices">
                <button
                  v-for="option in block.options"
                  :key="option.id"
                  class="choice"
                  :class="`choice--${option.tone ?? 'default'}`"
                  type="button"
                  :disabled="phase === 'thinking'"
                  @click="pickOption(option.send)"
                >
                  {{ option.label }}
                </button>
              </div>
              <div v-else class="rcard">
                <div class="rcard__head">
                  <span class="rcard__name">{{ block.title }}</span>
                  <span class="rcard__count">{{ block.total }} 条</span>
                </div>
                <div v-if="block.columns.length" class="rcard__scroll">
                  <table class="rcard__table">
                    <thead>
                      <tr>
                        <th v-for="(column, columnIndex) in block.columns" :key="columnIndex">{{ column }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(cells, cellIndex) in block.rows" :key="cellIndex">
                        <td v-for="(cell, cellIndex2) in cells" :key="cellIndex2">{{ cell }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </template>
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

    <!-- 研究流程已改为右侧控制台抽屉（见文件末尾 <ResearchFlowDrawer>），
         这里不再占用内容列。 -->

    <!-- 待批准条：贴着输入框上方（研究者 2026-09-22：不放在内容输出区）。
         四行 = 要执行的命令 + 三个选择；批准或拒绝后它自己就没了。 -->
    <div v-if="pendingApprovalBar" class="approval-bar" role="group" aria-label="待批准">
      <code class="approval-bar__cmd">{{ approvalCommand(pendingApprovalBar.card.preview) }}</code>
      <button
        class="approval-bar__action"
        type="button"
        :disabled="phase === 'thinking' || !session.isOwner"
        @click="decideApprovalCard(pendingApprovalBar.card, 'approve')"
      >
        批准
      </button>
      <button
        class="approval-bar__action"
        type="button"
        :disabled="phase === 'thinking' || !session.isOwner"
        @click="decideApprovalCard(pendingApprovalBar.card, 'approve_conversation')"
      >
        此对话默认批准
      </button>
      <button
        class="approval-bar__action"
        type="button"
        :disabled="phase === 'thinking' || !session.isOwner"
        @click="decideApprovalCard(pendingApprovalBar.card, 'deny')"
      >
        拒绝
      </button>
    </div>

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
          <!-- 完全访问模式：默认关。开着时①节点跑完自动接力下一站
               ②普通动手操作（跑命令/写文件）不再逐一确认；**高危操作仍会先弹批准卡**。
               转人工 / 失败 / 下一节点是占位都会自动停住。 -->
          <button
            class="autorun"
            :class="{ 'autorun--on': fullAccess }"
            type="button"
            role="switch"
            :aria-checked="fullAccess ? 'true' : 'false'"
            :title="
              accessNote ||
              (fullAccess
                ? '完全访问模式：已开启。普通操作直接执行，高危操作仍会先问你'
                : '完全访问模式：关闭中。动手前都会先问你，点一下开启')
            "
            @click="toggleFullAccess"
          >
            <span class="autorun__dot" />
            {{ fullAccess ? '完全访问模式' : '逐步确认' }}
          </button>
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

  <!-- 研究流程：右侧控制台抽屉（固定定位挂在 .chat 之外，避免被 .chat 的 overflow/animation 裁剪） -->
  <ResearchFlowDrawer :conversation-id="conversationId" />
</template>

<style scoped>
.chat {
  /* 宽度用 min(880, 100% - 60) 而不是 width:100% + max-width：
     后者配上 margin-left 会让整列超出容器 30px（压到抽屉底下）。 */
  width: min(880px, calc(100% - 60px));
  display: flex;
  flex-direction: column;
  min-height: 100%;
  padding: 64px 32px 0;
  /* 空间够时居中；右侧抽屉越宽容器越窄 → 正文自然往左靠，但**始终保留 30px 左边距** */
  align-self: flex-start;
  margin-left: max(30px, calc((100% - 880px) / 2));
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

/* ------------------------------------------------------------------ *
 * 思考过程折叠：入口在耗时那一行下面，展开后才看内容。
 * 它不是答复，所以**视觉上要明显弱于正文**；展开区用等宽小字 + 左竖线区分。
 * ------------------------------------------------------------------ */
.reason {
  margin-top: 2px;
}

.reason__head {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.reason__head:hover {
  color: var(--h-fg);
}

.reason__caret {
  flex: none;
  transition: transform var(--motion-dur) var(--motion-ease);
}

.reason__caret--open {
  transform: rotate(90deg);
}

.reason__body {
  margin: 6px 0 0;
  padding: 6px 0 6px 10px;
  border-left: 2px solid var(--h-line);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-sm);
  line-height: 1.6;
  color: var(--h-fg-muted);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}

/* ------------------------------------------------------------------ *
 * 节点执行的系统行：紧凑一行，只报过程（进入 / 驳回 / 重试 / 回退 / 迁移）
 * ------------------------------------------------------------------ */
.rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 8px 0 2px;
}

.row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: var(--font-size-sm);
}

.row__kind {
  flex: none;
  padding: 1px 6px;
  border: 1px solid var(--h-line);
  border-radius: 4px;
  font-size: var(--font-size-xs);
  color: var(--h-fg-muted);
  white-space: nowrap;
}

.row__text {
  color: var(--h-fg-muted);
  word-break: break-word;
}

.row--ok .row__kind {
  border-color: var(--h-ok);
  color: var(--h-ok);
}

.row--warn .row__kind {
  border-color: var(--h-warn);
  color: var(--h-warn);
}

.row--err .row__kind {
  border-color: var(--h-err);
  color: var(--h-err);
}

.row--info .row__kind {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

/* ------------------------------------------------------------------ *
 * 批准卡：模型提出了一次写盘/执行请求，等研究者裁决
 * 它是**这一轮的主角**，所以不缩在小字里——要执行什么必须一眼看清。
 * ------------------------------------------------------------------ */
/* ------------------------------------------------------------------ *
 * 待批准条（贴在输入框上方）
 *
 * 研究者 2026-09-22 定的口径：
 * · **只用黑白灰**，按钮不上色（原来主色/危险色齐上，像告警横幅）；
 * · 四行：第一行"要执行什么"（直接给命令本身），后三行是三个选择；
 * · 批完就消失，正文里也不留"已批准"这类话。
 * ------------------------------------------------------------------ */
.approval-bar {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 8px;
  padding: 10px 12px;
  border: 1px solid var(--h-line);
  border-radius: var(--radius-md);
  background: var(--h-surface);
  animation: approval-in 180ms cubic-bezier(0.16, 1, 0.3, 1);
}

.approval-bar__cmd {
  display: block;
  margin-bottom: 6px;
  color: var(--h-fg);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--font-size-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  user-select: text;
}

.approval-bar__action {
  display: block;
  width: 100%;
  padding: 7px 8px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--h-fg);
  font-family: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color 160ms cubic-bezier(0.4, 0, 0.2, 1), transform 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.approval-bar__action:hover:not(:disabled) {
  background: var(--h-hover);
}

.approval-bar__action:active:not(:disabled) {
  transform: scale(0.995);
}

.approval-bar__action:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 2px;
}

.approval-bar__action:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

@keyframes approval-in {
  from {
    opacity: 0;
    transform: translateY(6px) scale(0.985);
  }
  to {
    opacity: 1;
    transform: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .approval-bar {
    animation: none;
  }
}

/* ------------------------------------------------------------------ *
 * 结构化块：引导词的可点出口 + 查询结果卡片
 * ------------------------------------------------------------------ */
.blocks {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.choices {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.choice {
  padding: 6px 12px;
  border: 1px solid var(--h-line);
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg);
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: border-color var(--motion-dur) var(--motion-ease), background var(--motion-dur) var(--motion-ease);
}

.choice:hover:not(:disabled) {
  border-color: var(--h-primary);
}

.choice:active:not(:disabled) {
  transform: translateY(1px);
}

.choice:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.choice--primary {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

.rcard {
  border: 1px solid var(--h-line);
  border-radius: 8px;
  overflow: hidden;
}

.rcard__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--h-line);
}

.rcard__name {
  color: var(--h-fg);
  font-size: var(--font-size-sm);
}

.rcard__count {
  flex: none;
  color: var(--h-fg-muted);
  font-size: var(--font-size-xs);
}

.rcard__scroll {
  max-height: 260px;
  overflow: auto;
}

.rcard__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-sm);
}

.rcard__table th,
.rcard__table td {
  padding: 5px 10px;
  text-align: left;
  border-bottom: 1px solid var(--h-line);
  white-space: nowrap;
}

.rcard__table th {
  color: var(--h-fg-muted);
  font-weight: 400;
}

.rcard__table td {
  color: var(--h-fg);
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

/* 自动连续跑开关：与模型选择器并排，状态一眼可见（圆点=开） */
.autorun {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--h-line);
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
  white-space: nowrap;
  cursor: pointer;
  transition:
    border-color var(--motion-dur) var(--motion-ease),
    color var(--motion-dur) var(--motion-ease);
}

.autorun:hover {
  border-color: var(--h-primary);
}

.autorun__dot {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--h-line-strong);
}

.autorun--on {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

.autorun--on .autorun__dot {
  background: var(--h-primary);
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
