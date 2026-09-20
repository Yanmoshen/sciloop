<!--
  Copyright 2026 SciLoop contributors
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  供应商区（双栏）—— 交互与信息层级照搬 Cherry Studio 的「模型服务」页：
  左栏 = 搜索 + 服务商列表（头像方块 / 名称 / 右侧状态点）+ 底部「+ 添加服务商」；
  右栏 = 名称（可编辑）+ 开关、API 密钥（掩码 + 眼睛 + 保存 + 检测）、
  API 地址 + 类型、模型（搜索 / 获取模型列表 / + 添加模型 / 每行单价与删除）。

  纪律
  ----
  - 只用壳层 `--h-*` 令牌（`styles/home-theme.css`），不引 `tokens.css`，不造 hex。
  - 状态一律 pill / 徽标，**不写说明性灰色小字**；字段标签、placeholder、错误提示、空状态除外。
  - API Key 只上送、**永不回显**：眼睛只切本输入框的明文显示；服务端只回脱敏值。
  - 定价按 Cherry 口径 `pricing.{input,output}.{currency, perMillionTokens}`；
    缺失显示「单价未配置」，非 USD 只加徽标、**不计入任何 USD 小计**（禁止换算、禁止猜价）。
  - 「启用开关」：后端 schema 里没有启用位（只有 `is_default`），因此开关如实绑定
    `is_default` 并标注「默认」，不新造后端口径。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ApiError,
  type ConnectivityResult,
  type ModelConfig,
  type ModelEntry,
  type PricingSide,
  syncModels as syncModelsApi,
  updateModelConfig,
} from '@/api/models'
import PsSelect from '@/components/settings/PsSelect.vue'
import { useSettingsStore } from '@/stores/settings'

const props = defineProps<{
  /** 写操作是否可用（沿用「本机有 OWNER_TOKEN」语义；服务端仍逐条校验） */
  canWrite: boolean
}>()

const emit = defineEmits<{
  (e: 'create'): void
}>()

const store = useSettingsStore()

/* ------------------------------------------------------------------ *
 * 左栏：搜索与选择
 * ------------------------------------------------------------------ */
const query = ref('')
const selectedId = ref<number | null>(null)
/**
 * 右栏涉及的全部草稿 ref —— 必须**先于** `resetDrafts()` 的 immediate watch 声明，
 * 否则首次运行会在 TDZ 里读到未初始化的 ref。
 */
const modelQuery = ref('')
const syncing = ref(false)
const syncNotice = ref('')
const syncError = ref('')
const newModelId = ref('')
const editingModelId = ref<string | null>(null)
const priceInput = ref('')
const priceOutput = ref('')
const priceCurrency = ref('USD')
const pendingDelete = ref(false)

const filtered = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  const items = store.configs
  if (!keyword) return items
  return items.filter(
    (item) =>
      item.name.toLowerCase().includes(keyword) ||
      item.base_url.toLowerCase().includes(keyword) ||
      item.models.some((model) => model.model_id.toLowerCase().includes(keyword)),
  )
})

const current = computed<ModelConfig | null>(
  () => store.configs.find((item) => item.id === selectedId.value) ?? null,
)

/** 首次载入 / 删除后 / 过滤后：保证选中项有效（不自行新增，只做回落） */
watch(
  () => store.configs,
  (list) => {
    if (!list.length) {
      selectedId.value = null
      return
    }
    if (selectedId.value !== null && list.some((item) => item.id === selectedId.value)) return
    selectedId.value = filtered.value[0]?.id ?? list[0].id
  },
  { immediate: true, deep: false },
)

function select(id: number): void {
  selectedId.value = id
  resetDrafts()
}

/** 头像方块：名称首字母（Cherry 用品牌色块，这里只用壳层令牌） */
function initial(name: string): string {
  const text = name.trim()
  return text ? text[0].toUpperCase() : '?'
}

type ProviderState = 'ok' | 'warn' | 'quiet'

/** 状态点：连通性测试结果；无 Key 视为「未就绪」，不假装健康 */
function providerState(config: ModelConfig): ProviderState {
  if (config.test_ok === true) return 'ok'
  if (config.test_ok === false) return 'warn'
  return 'quiet'
}

function stateTitle(config: ModelConfig): string {
  if (config.test_ok === true) return '连通性测试通过'
  if (config.test_ok === false) return '连通性测试失败'
  return ''
}

/* ------------------------------------------------------------------ *
 * 右栏：草稿与写操作
 * ------------------------------------------------------------------ */
const nameDraft = ref('')
const baseUrlDraft = ref('')
const typeDraft = ref('')
const keyDraft = ref('')
/** 眼睛只切本地输入框的明文显示，**不等于**读服务端已存的 Key */
const keyVisible = ref(false)

const busy = ref(false)
const errorNotice = ref('')

const notice = ref('')
const testResult = ref<ConnectivityResult | null>(null)

const hasKey = computed(() => (current.value?.api_key_source ?? 'empty') !== 'empty')

const keyPlaceholder = computed(() =>
  hasKey.value ? '留空表示不修改' : '明文 Key，或 env:LLM_DEFAULT_API_KEY',
)

const TYPE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'openai', label: 'OpenAI 兼容' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google', label: 'Google' },
]

/** 后端 type 不做枚举校验：碰上表外的既有值，原样补一个选项，不静默改写 */
const typeOptions = computed(() => {
  const value = current.value?.type ?? ''
  if (!value || TYPE_OPTIONS.some((option) => option.value === value)) return TYPE_OPTIONS
  return [...TYPE_OPTIONS, { value, label: value }]
})

function resetDrafts(): void {
  const config = current.value
  nameDraft.value = config?.name ?? ''
  baseUrlDraft.value = config?.base_url ?? ''
  typeDraft.value = config?.type ?? ''
  keyDraft.value = ''
  keyVisible.value = false
  errorNotice.value = ''
  notice.value = ''
  testResult.value = null
  modelQuery.value = ''
  editingModelId.value = null
  newModelId.value = ''
  pendingDelete.value = false
}

watch(current, () => resetDrafts(), { immediate: true })

function failure(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}：${err.message}`
  return err instanceof Error ? err.message : String(err)
}

/**
 * 统一写入口：`PATCH /models/configs/{id}`（Owner）。
 * 失败**如实显示**服务端的 code / message，不吞掉、不伪装成功。
 */
async function patch(body: Record<string, unknown>): Promise<boolean> {
  const config = current.value
  if (!config) return false
  // 只读面：**不能静默返回**（否则表现成「点了没反应」）。按钮本身已前置禁用，
  // 这里只是兜底，并把真实原因说出来。
  if (!props.canWrite) {
    errorNotice.value = '只读面：需要 OWNER_TOKEN'
    return false
  }
  busy.value = true
  errorNotice.value = ''
  notice.value = ''
  try {
    await updateModelConfig(config.id, body)
    await store.loadConfigs()
    return true
  } catch (err) {
    errorNotice.value = failure(err)
    return false
  } finally {
    busy.value = false
  }
}

async function saveName(): Promise<void> {
  const value = nameDraft.value.trim()
  if (!current.value || !value || value === current.value.name) {
    nameDraft.value = current.value?.name ?? ''
    return
  }
  if (await patch({ name: value })) notice.value = '名称已保存'
  else nameDraft.value = current.value.name
}

async function saveBaseUrl(): Promise<void> {
  const value = baseUrlDraft.value.trim().replace(/\/$/, '')
  if (!current.value || !value || value === current.value.base_url) {
    baseUrlDraft.value = current.value?.base_url ?? ''
    return
  }
  if (await patch({ base_url: value })) notice.value = 'API 地址已保存'
  else baseUrlDraft.value = current.value.base_url
}

async function saveType(): Promise<void> {
  const value = typeDraft.value
  if (!current.value || value === (current.value.type ?? '')) return
  if (await patch({ type: value })) notice.value = '类型已保存'
  else typeDraft.value = current.value.type ?? ''
}

async function toggleDefault(): Promise<void> {
  const config = current.value
  if (!config) return
  if (await patch({ is_default: !config.is_default })) {
    notice.value = config.is_default ? '已取消默认供应商' : '已设为默认供应商'
  }
}

/** 保存 API Key：明文只上行，成功后立刻清空本地草稿（不回显） */
async function saveKey(): Promise<void> {
  const value = keyDraft.value.trim()
  if (!value) return
  if (await patch({ api_key: value })) {
    keyDraft.value = ''
    keyVisible.value = false
    notice.value = 'API 密钥已保存（仅脱敏值回传，不回显明文）'
  }
}

async function runTest(): Promise<void> {
  const config = current.value
  if (!config) return
  errorNotice.value = ''
  const result = await store.testConnection(config.id)
  testResult.value = result
  if (!result) errorNotice.value = `${store.errorCode ?? 'unknown_error'}：${store.error ?? '检测失败'}`
}

const testPill = computed(() => {
  const result = testResult.value
  if (!result) return null
  if (result.ok) return { cls: 'pill--ok', text: `检测通过 · ${result.latency_ms ?? '—'} ms` }
  return { cls: 'pill--warn', text: `检测失败 · ${result.error_kind ?? 'unknown'}` }
})

/* ------------------------------------------------------------------ *
 * 模型：搜索 / 获取模型列表 / 单价
 * ------------------------------------------------------------------ */
const models = computed(() => current.value?.models ?? [])

const filteredModels = computed(() => {
  const keyword = modelQuery.value.trim().toLowerCase()
  if (!keyword) return models.value
  return models.value.filter(
    (model) =>
      model.model_id.toLowerCase().includes(keyword) ||
      (model.label ?? '').toLowerCase().includes(keyword),
  )
})

const missingPriceCount = computed(
  () => models.value.filter((model) => priceView(model).missing).length,
)

const nonUsdCount = computed(() => models.value.filter((model) => priceView(model).nonUsd).length)

interface PriceView {
  /** 单价未配置（缺任一端或值为 null）——禁止猜价 */
  missing: boolean
  /** 非 USD：只加徽标，不进 USD 小计 */
  nonUsd: boolean
  currency: string
  input: number | null
  output: number | null
}

function priceView(entry: ModelEntry): PriceView {
  const pricing = entry.pricing ?? {}
  const input = (pricing.input ?? null) as PricingSide | null
  const output = (pricing.output ?? null) as PricingSide | null
  const currency = (input?.currency || output?.currency || 'USD').toUpperCase()
  const inputValue = input?.perMillionTokens ?? null
  const outputValue = output?.perMillionTokens ?? null
  return {
    missing: inputValue === null || outputValue === null,
    nonUsd: currency !== 'USD',
    currency,
    input: inputValue,
    output: outputValue,
  }
}

function amount(currency: string, value: number | null): string {
  if (value === null) return '未配置'
  return currency === 'USD' ? `$${value}/M` : `${currency} ${value}/M`
}

function priceLabel(entry: ModelEntry): string {
  const view = priceView(entry)
  if (view.missing) return '单价未配置'
  return `${amount(view.currency, view.input)} 入 · ${amount(view.currency, view.output)} 出`
}

/** 新的 `models[]`：原样保留后端回带的每个键（如 `cacheRead`），只做增删改 */
function withModels(next: ModelEntry[]): Promise<boolean> {
  return patch({ models: next })
}

async function syncModels(): Promise<void> {
  const config = current.value
  if (!config) return
  syncing.value = true
  syncError.value = ''
  syncNotice.value = ''
  errorNotice.value = ''
  try {
    const result = await syncModelsApi(config.id)
    await store.loadConfigs()
    if (result.added.length) {
      syncNotice.value = `上游返回 ${result.fetched} 个模型，新增 ${result.added.length} 个（单价未配置，需手动补齐）`
    } else {
      syncNotice.value = `上游返回 ${result.fetched} 个模型，均已存在，无新增`
    }
  } catch (err) {
    // 失败一律如实显示服务端 code / message（api_key_missing / upstream_* / invalid_response）
    syncError.value = failure(err)
  } finally {
    syncing.value = false
  }
}

async function addModel(): Promise<void> {
  const value = newModelId.value.trim()
  if (!value || !current.value) return
  if (models.value.some((model) => model.model_id === value)) {
    syncError.value = `模型 ${value} 已存在`
    return
  }
  // 与后端 sync-models 同口径：新增项 pricing 留空，前端不猜价
  const next: ModelEntry[] = [...models.value, { model_id: value, label: value, pricing: {} }]
  if (await withModels(next)) {
    newModelId.value = ''
    syncError.value = ''
    syncNotice.value = `已添加模型 ${value}（单价未配置）`
  }
}

async function removeModel(entry: ModelEntry): Promise<void> {
  const next = models.value.filter((model) => model.model_id !== entry.model_id)
  if (await withModels(next)) {
    if (editingModelId.value === entry.model_id) editingModelId.value = null
    syncNotice.value = `已移除模型 ${entry.model_id}`
  }
}

function startEditPrice(entry: ModelEntry): void {
  const view = priceView(entry)
  editingModelId.value = entry.model_id
  priceInput.value = view.input === null ? '' : String(view.input)
  priceOutput.value = view.output === null ? '' : String(view.output)
  priceCurrency.value = view.currency
}

function cancelEditPrice(): void {
  editingModelId.value = null
}

function toNumber(raw: string): number | null {
  const text = raw.trim()
  if (text === '') return null
  const value = Number(text)
  return Number.isFinite(value) && value >= 0 ? value : null
}

/** 单价写回：只覆盖 input/output，保留 pricing 里的其它键（如 cacheRead） */
async function savePrice(entry: ModelEntry): Promise<void> {
  const currency = priceCurrency.value.trim().toUpperCase() || 'USD'
  const side = (value: number | null): PricingSide | null =>
    value === null ? null : { currency, perMillionTokens: value }
  const next: ModelEntry[] = models.value.map((model) =>
    model.model_id === entry.model_id
      ? { ...model, pricing: { ...(model.pricing ?? {}), input: side(toNumber(priceInput.value)), output: side(toNumber(priceOutput.value)) } }
      : model,
  )
  if (await withModels(next)) {
    editingModelId.value = null
    syncNotice.value = `已更新 ${entry.model_id} 的单价`
  }
}

async function removeProvider(): Promise<void> {
  const config = current.value
  if (!config) return
  pendingDelete.value = false
  const ok = await store.removeConfig(config.id)
  if (!ok) errorNotice.value = `${store.errorCode ?? 'unknown_error'}：${store.error ?? '删除失败'}`
  else store.clearMessages()
}
</script>

<template>
  <div class="ps">
    <!-- ============ 左栏：服务商列表 ============ -->
    <aside class="ps__side" aria-label="服务商列表">
      <div class="ps__search">
        <input v-model="query" class="ps__search-input" type="search" placeholder="搜索服务商" aria-label="搜索服务商" />
        <button v-if="query" class="ps__icon-btn" type="button" aria-label="清除搜索" @click="query = ''">
          ×
        </button>
      </div>

      <div class="ps__items">
        <button
          v-for="config in filtered"
          :key="config.id"
          class="ps__item"
          :class="{ 'ps__item--on': config.id === selectedId }"
          type="button"
          :aria-current="config.id === selectedId ? 'true' : undefined"
          @click="select(config.id)"
        >
          <span class="ps__avatar" aria-hidden="true">{{ initial(config.name) }}</span>
          <span class="ps__item-name">{{ config.name }}</span>
          <span
            class="ps__dot"
            :class="`ps__dot--${providerState(config)}`"
            :title="stateTitle(config)"
          />
        </button>
        <p v-if="!filtered.length" class="ps__items-empty">
          {{ store.configs.length ? '没有匹配的服务商' : '尚未配置任何供应商' }}
        </p>
      </div>

      <div class="ps__foot">
        <button class="ps__add" type="button" :disabled="!canWrite" @click="emit('create')">
          + 添加服务商
        </button>
      </div>
    </aside>

    <!-- ============ 右栏：当前服务商详情 ============ -->
    <section v-if="current" class="ps__main">
      <header class="ps__head">
        <input
          v-model="nameDraft"
          class="ps__title"
          type="text"
          placeholder="提供商名称"
          aria-label="提供商名称"
          :disabled="!canWrite"
          @change="saveName"
          @keyup.enter="saveName"
        />
        <div class="ps__head-end">
          <button
            class="sw"
            type="button"
            role="switch"
            aria-label="默认供应商"
            :aria-checked="current.is_default ? 'true' : 'false'"
            :disabled="!canWrite"
            @click="toggleDefault"
          >
            <span class="sw__knob" />
          </button>
        </div>
      </header>

      <div class="ps__body">
        <!-- ---------- API 密钥 ---------- -->
        <section class="sec">
          <div class="sec__head">
            <span class="sec__title">API 密钥</span>
          </div>
          <div class="sec__row">
            <input
              v-model="keyDraft"
              class="input input--mono"
              :type="keyVisible ? 'text' : 'password'"
              autocomplete="off"
              :placeholder="keyPlaceholder"
              aria-label="API 密钥"
              :disabled="!canWrite"
              @keyup.enter="saveKey"
            />
            <button
              class="icon-btn"
              type="button"
              :aria-label="keyVisible ? '隐藏密钥' : '显示密钥'"
              :aria-pressed="keyVisible ? 'true' : 'false'"
              :disabled="!canWrite"
              @click="keyVisible = !keyVisible"
            >
              <svg v-if="keyVisible" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
                <path
                  d="M3 3l18 18M10.6 10.7a2 2 0 002.8 2.8M9.4 5.5A9.6 9.6 0 0112 5.2c4.5 0 8 3.4 9 6.8a10.5 10.5 0 01-2.6 3.8M6.1 7.2A10.9 10.9 0 003 12c1 3.4 4.5 6.8 9 6.8 1 0 2-.2 2.8-.5"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.6"
                  stroke-linecap="round"
                />
              </svg>
              <svg v-else viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
                <path
                  d="M3 12c1-3.4 4.5-6.8 9-6.8s8 3.4 9 6.8c-1 3.4-4.5 6.8-9 6.8S4 15.4 3 12z"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.6"
                />
                <circle cx="12" cy="12" r="2.6" fill="none" stroke="currentColor" stroke-width="1.6" />
              </svg>
            </button>
            <button
              class="btn btn--sm btn--primary"
              type="button"
              :disabled="!canWrite || !keyDraft.trim() || busy"
              @click="saveKey"
            >
              保存
            </button>
            <button class="btn btn--sm" type="button" :disabled="!canWrite || busy" @click="runTest">
              {{ store.testing ? '检测中…' : '检测' }}
            </button>
            <span v-if="testPill" class="pill" :class="testPill.cls">{{ testPill.text }}</span>
          </div>
        </section>

        <!-- ---------- API 地址 ---------- -->
        <section class="sec">
          <div class="sec__head">
            <span class="sec__title">API 地址</span>
          </div>
          <div class="sec__row">
            <input
              v-model="baseUrlDraft"
              class="input input--mono"
              type="text"
              placeholder="https://api.deepseek.com"
              aria-label="API 地址"
              :disabled="!canWrite"
              @change="saveBaseUrl"
              @keyup.enter="saveBaseUrl"
            />
            <PsSelect
              v-model="typeDraft"
              :options="typeOptions"
              :disabled="!canWrite"
              aria-label="类型"
              @change="saveType"
            />
          </div>
        </section>

        <!-- ---------- 模型 ---------- -->
        <section class="sec sec--models">
          <div class="sec__head">
            <span class="sec__title">模型</span>
            <span class="pill pill--quiet">共 {{ models.length }} 个</span>
            <span v-if="nonUsdCount" class="pill pill--warn" title="非 USD 定价不换算、不计入 USD 护栏">
              非 USD，未计入护栏 {{ nonUsdCount }} 个
            </span>
          </div>

          <div class="sec__row">
            <input
              v-model="modelQuery"
              class="input"
              type="search"
              placeholder="筛选模型"
              aria-label="筛选模型"
            />
            <button
              class="btn btn--sm"
              type="button"
              :disabled="!canWrite || !hasKey || syncing"
              @click="syncModels"
            >
              {{ syncing ? '获取中…' : '获取模型列表' }}
            </button>
            <span v-if="!hasKey" class="pill pill--warn">需先配置 API 密钥</span>
            <input
              v-model="newModelId"
              class="input input--mono"
              type="text"
              placeholder="新模型 ID"
              aria-label="新模型 ID"
              :disabled="!canWrite"
              @keyup.enter="addModel"
            />
            <button
              class="btn btn--sm"
              type="button"
              :disabled="!canWrite || !newModelId.trim() || busy"
              @click="addModel"
            >
              + 添加模型
            </button>
          </div>

          <p v-if="syncError" class="state state--error">{{ syncError }}</p>
          <p v-else-if="syncNotice" class="state"><span class="pill pill--ok">{{ syncNotice }}</span></p>

          <ul v-if="filteredModels.length" class="rows">
            <li v-for="entry in filteredModels" :key="entry.model_id" class="row">
              <div class="row__main">
                <span class="row__name" :title="entry.label ?? entry.model_id">
                  {{ entry.label || entry.model_id }}
                </span>
                <span class="tag-mono">{{ entry.model_id }}</span>
                <span class="pill" :class="priceView(entry).missing ? 'pill--warn' : 'pill--quiet'">
                  {{ priceLabel(entry) }}
                </span>
                <span v-if="priceView(entry).nonUsd" class="pill pill--warn">非 USD，未计入护栏</span>
              </div>
              <div class="row__end">
                <button
                  class="icon-btn"
                  type="button"
                  :aria-label="`设置 ${entry.model_id} 的单价`"
                  :disabled="!canWrite"
                  @click="editingModelId === entry.model_id ? cancelEditPrice() : startEditPrice(entry)"
                >
                  <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
                    <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="1.7" />
                    <path
                      d="M19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1A1.7 1.7 0 008.9 19a1.7 1.7 0 00-1.9.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1A1.7 1.7 0 004.5 15a1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1A1.7 1.7 0 004.5 9a1.7 1.7 0 00-.3-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1A1.7 1.7 0 009 4.5h.1A1.7 1.7 0 0010 3V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1A1.7 1.7 0 0019.5 9v.1a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.4"
                    />
                  </svg>
                </button>
                <button
                  class="icon-btn"
                  type="button"
                  :aria-label="`移除模型 ${entry.model_id}`"
                  :disabled="!canWrite || busy"
                  @click="removeModel(entry)"
                >
                  −
                </button>
              </div>

              <div v-if="editingModelId === entry.model_id" class="row__edit">
                <label class="field">
                  <span class="field__label">输入价 / 百万 token</span>
                  <input v-model="priceInput" class="input input--sm" type="number" min="0" step="0.01" placeholder="未配置" />
                </label>
                <label class="field">
                  <span class="field__label">输出价 / 百万 token</span>
                  <input v-model="priceOutput" class="input input--sm" type="number" min="0" step="0.01" placeholder="未配置" />
                </label>
                <label class="field">
                  <span class="field__label">币种</span>
                  <input v-model="priceCurrency" class="input input--sm" type="text" placeholder="USD" />
                </label>
                <div class="row__edit-end">
                  <button class="btn btn--sm" type="button" @click="cancelEditPrice">取消</button>
                  <button
                    class="btn btn--sm btn--primary"
                    type="button"
                    :disabled="!canWrite || busy"
                    @click="savePrice(entry)"
                  >
                    保存单价
                  </button>
                </div>
              </div>
            </li>
          </ul>
          <p v-else class="empty-hint">
            {{ models.length ? '没有匹配的模型' : '点击上方的获取模型列表按钮添加模型' }}
          </p>
        </section>

        <p v-if="errorNotice" class="state state--error">{{ errorNotice }}</p>
        <p v-else-if="notice" class="state"><span class="pill pill--ok">{{ notice }}</span></p>

        <!-- ---------- 危险操作 ---------- -->
        <div class="danger">
          <template v-if="pendingDelete">
            <button class="btn btn--sm btn--danger" type="button" :disabled="busy" @click="removeProvider">
              确认删除
            </button>
            <button class="btn btn--sm" type="button" @click="pendingDelete = false">取消</button>
          </template>
          <button v-else class="btn btn--sm" type="button" :disabled="!canWrite" @click="pendingDelete = true">
            删除服务商
          </button>
        </div>
      </div>
    </section>

    <section v-else class="ps__main ps__main--empty">
      <p class="empty-hint">尚未配置任何供应商</p>
      <button class="ps__add ps__add--inline" type="button" :disabled="!canWrite" @click="emit('create')">
        + 添加服务商
      </button>
    </section>
  </div>
</template>

<style scoped>
/* 版式对齐 Cherry Studio「模型服务」：左栏 248px 固定，右栏内容自滚 */
.ps {
  display: flex;
  gap: 16px;
  height: clamp(480px, 70vh, 660px);
  min-height: 0;
}

/* ---------- 左栏 ---------- */
.ps__side {
  display: flex;
  flex-direction: column;
  width: 248px;
  flex: none;
  padding-right: 12px;
  border-right: 1px solid var(--h-line);
}

.ps__search {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 8px;
  margin-bottom: 8px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: var(--h-surface-input);
}

.ps__search-input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  outline: none;
}

.ps__icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: none;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--h-fg-subtle);
  font: inherit;
  font-size: var(--font-size-sm);
  line-height: 1;
  cursor: pointer;
  transition: background-color 300ms ease, color 300ms ease;
}

.ps__icon-btn:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

.ps__items {  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow-y: auto;
  padding-right: 2px;
}

.ps__item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 32px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color 300ms ease;
}

.ps__item:hover {
  background: var(--h-hover);
}

.ps__item--on {
  background: var(--h-hover);
  border-color: var(--h-line);
}

.ps__avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex: none;
  border-radius: 6px;
  border: 1px solid var(--h-line);
  background: color-mix(in srgb, var(--h-secondary) 14%, transparent);
  color: var(--h-secondary);
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.ps__item-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ps__item--on .ps__item-name {
  font-weight: 600;
}

.ps__dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: 50%;
  background: var(--h-line-strong);
}

.ps__dot--ok {
  background: var(--h-secondary);
}

.ps__dot--warn {
  background: var(--h-primary);
}

.ps__dot--quiet {
  background: var(--h-line-strong);
}

.ps__items-empty {
  margin: 12px 4px;
  font-size: var(--font-size-sm);
  color: var(--h-fg-subtle);
}

.ps__foot {
  padding-top: 10px;
}

.ps__add {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 32px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: background-color 300ms ease, border-color 300ms ease;
}

.ps__add:hover:not(:disabled) {
  background: var(--h-hover);
  border-color: var(--h-line-strong);
}

.ps__add--inline {
  width: auto;
  padding: 0 14px;
}

/* ---------- 右栏 ---------- */
.ps__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.ps__main--empty {
  align-items: center;
  justify-content: center;
  gap: 12px;
}

.ps__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--h-line);
}

.ps__title {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-lg);
  font-weight: 600;
  outline: none;
}

.ps__title:hover:not(:disabled) {
  border-color: var(--h-line);
}

.ps__title:focus {
  border-color: var(--h-line-strong);
  background: var(--h-surface-input);
}

.ps__head-end {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.ps__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
  overflow-y: auto;
  padding: 16px 2px 4px;
}

/* 各分区按自然高度排布（不压缩），超出部分由 .ps__body 滚动 —— 否则区块会被压扁裁切 */
.ps__body > * {
  flex: none;
}

/* ---------- 分区 ---------- */
.sec {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 模型区吃掉剩余高度；保底高度保证列表至少能露出几行，不够时由 .ps__body 滚 */
.sec--models {
  flex: 1 1 auto;
  min-height: 230px;
}

.sec__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.sec__title {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--h-fg);
}

.sec__row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

/* ---------- 模型行 ---------- */
.rows {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 auto;
  overflow-y: auto;
  min-height: 0;
}

.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-height: 40px;
  padding: 6px 10px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  /* 不参与压缩：展开单价编辑时行高要撑开，由 .rows 自己滚 */
  flex: none;
}

.row__main {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.row__name {
  font-size: var(--font-size-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 40%;
}

.row__end {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: none;
}

.row__edit {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 10px;
  width: 100%;
  padding: 10px 2px 4px;
  border-top: 1px dashed var(--h-line);
}

.row__edit-end {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.field__label {
  font-size: var(--font-size-xs);
  color: var(--h-fg-subtle);
}

.empty-hint {
  margin: 0;
  padding: 28px 16px;
  border: 1px dashed var(--h-line-strong);
  border-radius: 12px;
  text-align: center;
  font-size: var(--font-size-sm);
  color: var(--h-fg-subtle);
}

.danger {
  display: flex;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--h-line);
}

/* ---------- 控件（颜色一律 --h-*） ---------- */
.input {
  min-width: 0;
  padding: 0 10px;
  height: 32px;
  border: 1px solid var(--h-line);
  border-radius: 8px;
  background: var(--h-surface-input);
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  outline: none;
  transition: border-color 300ms ease;
}

.input {
  flex: 1;
}

.sec__row > .input[type='search'] {
  flex: 0 1 220px;
}

.sec__row > .input[type='text'] {
  flex: 0 1 200px;
}

.input:focus {
  border-color: var(--h-line-strong);
}

.input:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.input--mono {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}

.input--sm {
  height: 28px;
  width: 140px;
  flex: none;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: none;
  border: 1px solid var(--h-line);
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
  transition: background-color 300ms ease;
}

.icon-btn:hover:not(:disabled) {
  background: var(--h-hover);
  color: var(--h-fg);
}

.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn {
  height: 32px;
  padding: 0 14px;
  flex: none;
  border: 1px solid var(--h-line-strong);
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  white-space: nowrap;
  transition: background-color 300ms ease;
}

.btn:hover:not(:disabled) {
  background: var(--h-hover);
}

.btn--sm {
  height: 30px;
  padding: 0 12px;
}

.btn--primary {
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
  font-weight: 500;
}

.btn--primary:hover:not(:disabled) {
  filter: brightness(1.06);
  background: var(--h-primary);
}

.btn--danger {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn:disabled:hover {
  background: transparent;
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border: 1px solid var(--h-line);
  border-radius: 999px;
  font-size: var(--font-size-xs);
  color: var(--h-fg-muted);
  white-space: nowrap;
}

.pill--ok {
  border-color: transparent;
  background: color-mix(in srgb, var(--h-secondary) 14%, transparent);
  color: var(--h-secondary);
}

.pill--warn {
  border-color: transparent;
  background: var(--h-active);
  color: var(--h-primary);
}

.pill--quiet {
  border-color: transparent;
  background: var(--h-hover);
}

.tag-mono {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
  font-size: var(--font-size-xs);
  padding: 2px 8px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--h-secondary) 14%, transparent);
  color: var(--h-secondary);
  white-space: nowrap;
}

.state {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.state--error {
  color: var(--h-primary);
}

.sw {
  width: 42px;
  height: 24px;
  flex: none;
  position: relative;
  padding: 0;
  border: 1px solid var(--h-line-strong);
  border-radius: 999px;
  background: var(--h-surface-input);
  cursor: pointer;
}

.sw__knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--h-surface-raised);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25); /* ui-polish-allow: 滑块投影色 */
  transition: transform 300ms cubic-bezier(0.4, 0, 0.2, 1);
}

.sw[aria-checked='true'] {
  background: var(--h-primary);
  border-color: var(--h-primary);
}

.sw[aria-checked='true'] .sw__knob {
  transform: translateX(18px);
}

.sw:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
