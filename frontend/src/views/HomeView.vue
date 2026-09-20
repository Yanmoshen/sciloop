<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 总览页（未进入项目时的首页）：一句话入口 + 三张快捷卡。
 *
 * 会话流程：输入需求 → `POST /chat/home` 真调模型（拿到「标题」+「正式回答」）
 * → 用标题建项目（标题即项目名，出现在左栏「最近打开」）→ 就地展示回答。
 * 进入会话后：开场文案 / 流程行 / 三张卡收起，输入框独占最后一行。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { chatHome } from '@/api/chat'
import type { CreatedProject } from '@/api/projects'
import { createProject } from '@/api/projects'
import ProjectCreateDialog from '@/components/ProjectCreateDialog.vue'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'

const router = useRouter()
const session = useSessionStore()
const settings = useSettingsStore()

const prompt = ref('')
const files = ref<Array<{ name: string }>>([])
const fileInput = ref<HTMLInputElement | null>(null)

const phase = ref<'idle' | 'thinking' | 'answered'>('idle')
const dialogOpen = ref(false)
const dialogPrefill = ref('')

const answer = ref('')
const userText = ref('')
const answerTitle = ref('')
const errorText = ref('')
const pickedRef = ref('')
const listening = ref(false)

const canSend = computed(() => prompt.value.trim().length > 0 || files.value.length > 0)
const active = computed(() => phase.value !== 'idle' || answer.value.length > 0)

const PIPELINE = '文献调研 → idea 生成 → 算法生成 → 算法评审 → 自动实验 → 论文写作 → 论文评审'

/** 默认供应商（后端保证互斥唯一） */
const defaultProvider = computed(() => settings.configs.find((config) => config.is_default) ?? null)

/** 模型清单：只列默认供应商的模型；没有默认供应商时只给一个 auto 占位 */
const modelOptions = computed(() => {
  const provider = defaultProvider.value
  if (!provider) return [{ value: 'auto', label: 'auto' }]
  return (provider.models ?? []).map((entry) => ({
    value: `${provider.id}:${entry.model_id}`,
    label: entry.model_id,
  }))
})

const pickerOpen = ref(false)
const pickedLabel = computed(
  () => modelOptions.value.find((item) => item.value === pickedRef.value)?.label ?? 'auto',
)

function pickModel(value: string): void {
  pickedRef.value = value
  pickerOpen.value = false
}

let timer: number | null = null

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

/** 语音输入（浏览器原生识别；不可用时按钮不渲染） */
const speechSupported =
  typeof window !== 'undefined' &&
  Boolean(
    (window as unknown as { SpeechRecognition?: unknown; webkitSpeechRecognition?: unknown })
      .SpeechRecognition ||
      (window as unknown as { webkitSpeechRecognition?: unknown }).webkitSpeechRecognition,
  )

function toggleVoice(): void {
  type Recognizer = {
    lang: string
    interimResults: boolean
    continuous: boolean
    onresult: (event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void
    onend: () => void
    onerror: () => void
    start: () => void
    stop: () => void
  }
  const holder = window as unknown as {
    SpeechRecognition?: new () => Recognizer
    webkitSpeechRecognition?: new () => Recognizer
  }
  const Ctor = holder.SpeechRecognition ?? holder.webkitSpeechRecognition
  if (!Ctor) return
  if (listening.value) {
    listening.value = false
    return
  }
  const recognizer = new Ctor()
  recognizer.lang = 'zh-CN'
  recognizer.interimResults = false
  recognizer.continuous = false
  recognizer.onresult = (event) => {
    const parts: string[] = []
    for (let i = 0; i < event.results.length; i += 1) {
      const first = event.results[i][0]
      if (first) parts.push(first.transcript)
    }
    const text = parts.join('').trim()
    if (text) prompt.value = prompt.value ? `${prompt.value} ${text}` : text
  }
  recognizer.onend = () => {
    listening.value = false
  }
  recognizer.onerror = () => {
    listening.value = false
  }
  listening.value = true
  recognizer.start()
}

/** 发送：真调模型拿标题与回答 → 用标题建项目 → 展示回答 */
async function send(): Promise<void> {
  if (!canSend.value || phase.value === 'thinking') return
  const text = prompt.value.trim() || files.value.map((file) => file.name).join('、')
  if (!text) return

  if (!session.isOwner) {
    errorText.value = '只读面：需要 OWNER_TOKEN'
    return
  }
  if (pickedRef.value === 'auto') {
    errorText.value = '请先到「模型设置」填写模型'
    return
  }
  const [configIdRaw, ...rest] = pickedRef.value.split(':')
  const configId = Number(configIdRaw)
  const modelId = rest.join(':')
  if (!configId || !modelId) {
    errorText.value = '请先到「模型设置」填写模型'
    return
  }

  if (timer !== null) window.clearTimeout(timer)
  errorText.value = ''
  answer.value = ''
  userText.value = text
  phase.value = 'thinking'
  // 发送即清空输入框（不等结果），避免旧文本残留在输入框里
  prompt.value = ''
  files.value = []

  try {
    const result = await chatHome({ text, model_config_id: configId, model_id: modelId })
    answerTitle.value = result.title
    answer.value = result.reply
    phase.value = 'answered'
    const created = await createProject({ name: result.title, note: text, fields: [] })
    await session.loadProjects()
    session.selectProject(created.id)
  } catch (err) {
    phase.value = 'idle'
    // 失败要能重发：把原文放回输入框
    prompt.value = text
    const withCode = err as { code?: string; message?: string }
    errorText.value = withCode?.message
      ? `${withCode.code ?? 'failed'}：${withCode.message}`
      : err instanceof Error
        ? err.message
        : String(err)
  }
}

function openCreate(prefill: string): void {
  dialogPrefill.value = prefill
  dialogOpen.value = true
}

function goFeed(): void {
  void router.push({ path: '/papers/feed' })
}

function goIdeas(): void {
  void router.push({ path: '/ideas' })
}

/** 创建成功：刷新项目列表并直接进入该项目的流水线工作台 */
function onCreated(project: CreatedProject): void {
  void session.loadProjects()
  session.selectProject(project.id)
  void router.push({ name: 'workbench', params: { projectId: String(project.id) } })
}

onMounted(async () => {
  prompt.value = ''
  if (!settings.configs.length) await settings.loadConfigs()
  pickedRef.value = modelOptions.value[0]?.value ?? 'auto'
})
</script>

<template>
  <section class="hero" :class="{ 'hero--active': active }">
    <div class="intro">
      <h1 class="hero__title">使用 AI，体验全新科研工作流</h1>
      <p class="hero__steps">{{ PIPELINE }}</p>
    </div>

    <div v-if="active" class="convo">
      <div class="convo__item">
        <span class="bubble bubble--user">{{ userText }}</span>
      </div>
      <div v-if="phase === 'thinking'" class="dots" aria-label="正在思考">
        <span class="dot" />
        <span class="dot" />
        <span class="dot" />
      </div>
      <div v-else-if="answer" class="convo__item">
        <span v-if="answerTitle" class="pill pill--quiet">{{ answerTitle }}</span>
        <p class="bubble bubble--ai">{{ answer }}</p>
      </div>
    </div>

    <p v-if="errorText" class="state state--error">{{ errorText }}</p>

    <div class="composer">
      <div class="composer__body">
        <textarea
          v-model="prompt"
          rows="2"
          placeholder="例如：为长上下文问答设计一套可复现的评测方案"
          @keydown.enter.exact.prevent="send"
        />
        <div class="composer__tools">
          <button class="icon-btn" type="button" title="上传文件" aria-label="上传文件" @click="pickFiles">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
          </button>
          <span
            v-for="(file, index) in files"
            :key="`${file.name}-${index}`"
            class="file-chip"
            :title="file.name"
            @click="removeFile(index)"
          >
            {{ file.name.length > 22 ? `${file.name.slice(0, 20)}…` : file.name }}
          </span>
          <input ref="fileInput" type="file" multiple hidden @change="onFiles" />

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
                <path d="M2 4l3 3 3-3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
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
        </div>
      </div>
      <button
        v-if="speechSupported"
        class="mic"
        type="button"
        :class="{ 'mic--on': listening }"
        title="语音输入"
        aria-label="语音输入"
        @click="toggleVoice"
      >
        <svg width="15" height="15" viewBox="0 0 18 18" fill="none" aria-hidden="true">
          <rect x="7" y="2.5" width="4" height="8" rx="2" stroke="currentColor" stroke-width="1.5" />
          <path d="M4.5 9a4.5 4.5 0 0 0 9 0M9 13.5V16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        </svg>
      </button>
      <button class="send" type="button" :disabled="!canSend" aria-label="发送" @click="send">
        <svg width="15" height="15" viewBox="0 0 18 18" fill="none" aria-hidden="true">
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

    <section v-if="!active" class="cards">
      <button class="card" type="button" @click="openCreate(prompt)">
        <span class="card__icon">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path d="M9 3v12M3 9h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
        </span>
        <span class="card__title">新建研究项目</span>
        <span class="card__desc">快速开始新的研究</span>
      </button>

      <button class="card" type="button" @click="goFeed">
        <span class="card__icon">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <circle cx="8" cy="8" r="4.5" stroke="currentColor" stroke-width="1.6" />
            <path d="M11.4 11.4 15 15" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
        </span>
        <span class="card__title">文献调研</span>
        <span class="card__desc">聚合检索文献，输出结构化分析总结</span>
      </button>

      <button class="card" type="button" @click="goIdeas">
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

    <ProjectCreateDialog v-model="dialogOpen" :prefill="dialogPrefill" @created="onCreated" />
  </section>
</template>

<style scoped>
.hero {
  width: 100%;
  max-width: 880px;
  padding: 64px 32px 48px;
}
.hero__title {
  margin: 0 0 16px;
  font-family: geomanist, 'Open Sans', ui-sans-serif, system-ui, sans-serif;
  font-size: var(--font-size-4xl);
  line-height: 1.1;
  font-weight: 600;
  letter-spacing: -0.5px;
  color: var(--h-fg);
  transition: color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}
.hero__steps {
  margin: 0 0 40px;
  font-size: var(--font-size-lg);
  color: var(--h-fg-muted);
  transition: color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}

/* ---------- 模拟响应（位于输入框上方） ---------- */
.stream {
  margin-bottom: 24px;
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
.answer {
  animation: rise 300ms cubic-bezier(0.4, 0, 0.2, 1);
}
@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
/* 模拟响应标记：做成 tag 而不是灰色说明小字 */
.tag {
  display: inline-flex;
  align-items: center;
  margin-bottom: 10px;
  padding: 2px 10px;
  border: 1px solid var(--h-line-strong);
  border-radius: 999px;
  color: var(--h-fg-muted);
  font-size: var(--font-size-xs);
  letter-spacing: 0.2px;
}
.answer p {
  margin: 0 0 16px;
  font-size: var(--font-size-md);
  line-height: 1.6;
  color: var(--h-fg-muted);
}
.answer__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* ---------- 输入区 ---------- */
.composer {
  position: relative;
  padding: 16px 12px 16px 16px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line-strong);
  border-radius: 16px;
  transition:
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}
.composer:focus-within {
  border-color: var(--h-primary);
}
.composer__body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.composer textarea {
  width: 100%;
  height: 88px;
  min-height: 88px;
  max-height: 88px;
  padding-right: 12px;
  border: 0;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-lg);
  line-height: 1.5;
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
  background-clip: border-box;
  border-radius: 40px;
}
.composer textarea::-webkit-scrollbar-thumb:hover {
  background: var(--h-fg-subtle);
}
@supports (-moz-appearance: none) {
  .composer textarea {
    scrollbar-width: thin;
    scrollbar-color: var(--h-line-strong) transparent;
  }
}
.composer__tools {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-right: 44px;
}
.tool {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
  transition:
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.tool:hover {
  border-color: var(--h-line-strong);
  color: var(--h-fg);
}
.tool--primary {
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
  font-weight: 600;
}
.tool--primary:hover {
  color: var(--h-primary-fg);
  border-color: var(--h-primary);
}
.file-chip {
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: var(--h-hover);
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
  cursor: pointer;
}
.send {
  position: absolute;
  right: 12px;
  bottom: 12px;
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 40px;
  background: var(--h-primary);
  color: var(--h-primary-fg);
  cursor: pointer;
  transition:
    transform 180ms cubic-bezier(0.4, 0, 0.2, 1),
    opacity 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.send:not(:disabled):hover {
  transform: translateY(-2px);
}

/* ---------- 三张快捷卡 ---------- */
.cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
  margin-top: 40px;
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
  .hero {
    padding: 32px 16px;
  }
  .hero__title {
    font-size: var(--font-size-3xl);
  }
  .cards {
    grid-template-columns: 1fr;
  }
}

/* ---------- 对话态：开场内容收起、输入框独占一行并置底 ---------- */

/* 关键前提：对话态下 hero 必须是「撑满内容区的纵向 flex」。
   否则 .convo{flex:1} 与 .composer 的定位都会失效 —— 输入框停在半空、还会盖住文本。 */
.hero--active {
  display: flex;
  flex-direction: column;
  min-height: 100%;
  padding-bottom: 24px;
}

/* 开场区与三张卡用「收起高度 + 淡出」过渡：输入框随之平滑下移 */
.intro,
.cards {
  max-height: 420px;
  opacity: 1;
  overflow: hidden;
  transition:
    max-height 360ms cubic-bezier(0.4, 0, 0.2, 1),
    opacity 220ms cubic-bezier(0.4, 0, 0.2, 1);
}

.hero--active .intro,
.hero--active .cards {
  max-height: 0;
  opacity: 0;
  margin: 0;
  pointer-events: none;
}

/* 对话区：吃掉剩余高度，把输入框挤到最后一行（与文本上下分开，不互相遮挡） */
.hero--active .convo {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  margin-top: 8px;
}

.convo {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 4px 0 16px;
}

.convo__item {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.convo__item:first-child {
  align-items: flex-end;
}

.bubble {
  max-width: 82%;
  padding: 12px 16px;
  border-radius: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.bubble--user {
  background: var(--h-active);
  color: var(--h-fg);
}

.bubble--ai {
  background: var(--h-surface);
  border: 1px solid var(--h-line);
  color: var(--h-fg);
  max-width: 100%;
}

/* 输入框工具行：右侧留出麦克风 + 发送按钮的位置，避免控件互相压住 */
.composer__tools {
  padding-right: 84px;
}

/* 上传：只有一个加号（无底块、无文字） */
.icon-btn {
  flex: none;
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-subtle);
  cursor: pointer;
  transition: color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.icon-btn:hover {
  color: var(--h-fg);
}

/* ---------- 模型选择：只有「名字 + ⌄」，无外框；浮层平滑展开 ---------- */
.mpick {
  position: relative;
  margin-left: auto;
}

.mpick__trigger {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  padding: 2px 4px;
  border: 0;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
  transition: color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.mpick__trigger:hover {
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
  background: var(--h-surface-raised, var(--h-surface));
  border: 1px solid var(--h-line-strong);
  border-radius: 12px;
  box-shadow: 0 12px 32px rgb(0 0 0 / 24%);
  animation: mpick-in 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes mpick-in {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
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

/* 语音输入：与发送按钮同排（发送按钮是绝对定位，这里跟随其左侧） */
.mic {
  position: absolute;
  right: 56px;
  bottom: 12px;
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  cursor: pointer;
}

.mic:hover {
  border-color: var(--h-line-strong);
  color: var(--h-fg);
}

.mic--on {
  border-color: var(--h-primary);
  color: var(--h-primary);
  box-shadow: 0 0 12px var(--h-primary);
}
</style>
