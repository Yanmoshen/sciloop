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
 * 说明（口径纪律）：
 * - 本轮为 **demo 阶段**：点 ↑ 后展示的是**模拟响应**（前端固定文案 + 三点思考动效），
 *   页面会显式标注「模拟响应 · 演示文案，非实时模型输出」，**不冒充真实模型输出**；
 * - 接入真实 LLM 后只需把 `mockAnswer()` 换成对后端接口的调用，动效与布局不变。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { CreatedProject } from '@/api/projects'
import ProjectCreateDialog from '@/components/ProjectCreateDialog.vue'
import { useSessionStore } from '@/stores/session'

const router = useRouter()
const session = useSessionStore()

const prompt = ref('')
const files = ref<Array<{ name: string }>>([])
const fileInput = ref<HTMLInputElement | null>(null)

const phase = ref<'idle' | 'thinking' | 'answered'>('idle')
const dialogOpen = ref(false)
const dialogPrefill = ref('')

const canSend = computed(() => prompt.value.trim().length > 0 || files.value.length > 0)

const PIPELINE = '文献调研 → idea 生成 → 算法生成 → 算法评审 → 自动实验 → 论文写作 → 论文评审'

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

/** 模拟响应：三点跳动约 1.6s 后给出结果（demo 阶段不调真实模型） */
function send(): void {
  if (!canSend.value) return
  if (timer !== null) window.clearTimeout(timer)
  phase.value = 'thinking'
  timer = window.setTimeout(() => {
    phase.value = 'answered'
    timer = null
  }, 1600)
}

function mockAnswer(): string {
  const topic = prompt.value.trim() || '这个课题'
  return `「${topic}」可以拆成三个可验证问题：任务难度的分层定义、上下文长度与答案稳定性的关系、以及对照基线的可比性。建议先用 20–50 条样本做分层对照实验，再决定是否扩展到多语言与多轮问答。`
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

onMounted(() => {
  prompt.value = ''
})
</script>

<template>
  <section class="hero">
    <h1 class="hero__title">使用 AI，体验全新科研工作流</h1>
    <p class="hero__steps">{{ PIPELINE }}</p>

    <div v-if="phase !== 'idle'" class="stream">
      <div v-if="phase === 'thinking'" class="dots" aria-label="正在思考">
        <span class="dot" />
        <span class="dot" />
        <span class="dot" />
      </div>
      <div v-else class="answer">
        <span class="tag">模拟响应</span>
        <p>{{ mockAnswer() }}</p>
        <div class="answer__actions">
          <button class="tool tool--primary" type="button" @click="openCreate(prompt)">
            新建项目并预填
          </button>
          <button class="tool" type="button" @click="goFeed">去文献调研</button>
        </div>
      </div>
    </div>

    <div class="composer">
      <div class="composer__body">
        <textarea
          v-model="prompt"
          rows="2"
          placeholder="例如：为长上下文问答设计一套可复现的评测方案"
          @keydown.enter.exact.prevent="send"
        />
        <div class="composer__tools">
          <button class="tool" type="button" @click="pickFiles">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            </svg>
            上传文件
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
        </div>
      </div>
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

    <section class="cards">
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
</style>
