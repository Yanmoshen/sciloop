<!--
  Copyright 2026 SciLoop contributors
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  设置页（WP02）：供应商管理 / 连通性测试 / 按环节路由 / 成本双线 / 数据源健康入口。

  约定：
  - 颜色一律走 CSS 变量（styles/tokens.css），组件内不硬编码色值
  - api_key 只上送不回显，列表展示脱敏值
  - Owner 令牌由用户在页面输入，仅存 sessionStorage
-->
<template>
  <section class="settings">

    <header class="page-head">
      <div>
        <h1 class="page-title">模型与成本设置</h1>
      </div>
      <div class="page-actions">
        <el-button :loading="store.loading" @click="refresh">刷新</el-button>
      </div>
    </header>

    <!-- 六类状态：loading / error / permission denied / retry（empty 见各表格 empty-text） -->
    <ViewStatePanel
      :loading="store.loading"
      loading-text="正在读取供应商 / 环节路由 / 成本 / 隔离状态 / 数据源健康…"
      :error="store.error"
      :error-code="store.errorCode"
      error-title="设置项读取失败"
      :permission-denied="permissionDenied"
      :permission-note="permissionNote"
      :permission-title="permissionTitle"
      retryable
      retry-label="重试读取设置"
      :busy="store.loading"
      @retry="refresh"
    />

    <el-alert v-if="store.notice" type="success" :closable="true" class="mb" :title="store.notice" @close="store.clearMessages()" />

    <!-- ============ 访问面 ============ -->
    <el-card class="card" shadow="never">
      <template #header>
        <div class="card-head">
          <span class="card-title">访问面（Owner 模式）</span>
          <el-button text size="small" :loading="demoSession.loading" @click="demoSession.refresh()">
            重新校验访问面
          </el-button>
        </div>
      </template>
      <div class="row">
        <el-input
          v-model="ownerTokenDraft"
          type="password"
          show-password
          clearable
          placeholder="在此输入 OWNER_TOKEN 后才能修改配置（仅存本机 sessionStorage）"
          class="grow"
        />
        <el-button type="primary" :disabled="!ownerTokenDraft" @click="applyOwnerToken">应用令牌</el-button>
      </div>
      <el-alert
        class="mt"
        :type="demoSession.isOwner ? 'success' : 'warning'"
        :closable="false"
        show-icon
        :title="
          demoSession.isOwner
            ? 'owner_mode：写操作可用（服务端仍逐条校验并留审计日志）'
            : 'public_demo：匿名只读面，写操作会被拒绝（403 owner_token_required）'
        "
        :description="
          demoSession.isOwner
            ? ''
            : demoSession.ownerSession?.permissions?.reason ??
              demoSession.error ??
              '访问面未获取：请点击「重新校验访问面」'
        "
      />
    </el-card>

    <!-- ============ 供应商 ============ -->
    <el-card class="card" shadow="never">
      <template #header>
        <div class="card-head">
          <span class="card-title">供应商</span>
          <div class="card-actions">
            <el-button type="primary" :disabled="!canWrite" @click="openCreateDialog">新增供应商</el-button>
            <span v-if="!canWrite" class="denied">只读面：写操作已禁用</span>
          </div>
        </div>
      </template>

      <el-table :data="store.configs" size="small" empty-text="尚未配置任何供应商">
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="base_url" label="Base URL" min-width="220" show-overflow-tooltip />
        <el-table-column label="模型" min-width="160">
          <template #default="{ row }">
            <el-tag v-for="model in row.models" :key="model.model_id" size="small" class="tag">
              {{ model.model_id }}
            </el-tag>
            <span v-if="!row.models.length" class="muted">未登记</span>
          </template>
        </el-table-column>
        <el-table-column label="API Key" min-width="170">
          <template #default="{ row }">
            <span class="mono">{{ row.api_key_masked || '—' }}</span>
            <el-tag size="small" type="info" class="tag">{{ keySourceLabel(row.api_key_source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="单价" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.pricing_complete" size="small" type="success">完整</el-tag>
            <el-tag v-else size="small" type="warning">缺失</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="连通性" width="140">
          <template #default="{ row }">
            <el-tag v-if="row.test_ok === true" size="small" type="success">通过</el-tag>
            <el-tag v-else-if="row.test_ok === false" size="small" type="danger">失败</el-tag>
            <span v-else class="muted">未测试</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" :disabled="!canWrite" :loading="store.testing" @click="store.testConnection(row.id)">测试</el-button>
            <el-button size="small" :disabled="!canWrite" @click="openEditDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" plain :disabled="!canWrite" @click="removeConfig(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-alert
        v-if="store.pricingIncomplete.length"
        type="warning"
        :closable="false"
        class="mt"
        title="存在未填写单价的模型"
        description="这些模型的 cost_usd 会记为 null（禁止估算），成本护栏无法精确判定，请在编辑弹窗补齐单价。"
      />

      <div v-if="store.lastTestResult" class="test-result mt">
        <strong>最近一次连通性测试：</strong>
        <span>{{ store.lastTestResult.model_ref ?? '—' }}</span>
        <el-tag :type="store.lastTestResult.ok ? 'success' : 'danger'" size="small" class="tag">
          {{ store.lastTestResult.ok ? '成功' : store.lastTestResult.error_kind ?? '失败' }}
        </el-tag>
        <span class="muted">
          {{ store.lastTestResult.latency_ms ?? '—' }} ms ·
          {{ store.lastTestResult.ok ? store.lastTestResult.reply_preview : store.lastTestResult.message }}
        </span>
        <span class="muted">（已写入 llm_call_logs，purpose=connectivity_test）</span>
      </div>
    </el-card>

    <!-- ============ 环节路由 ============ -->
    <el-card class="card" shadow="never">
      <template #header>
        <div class="card-head">
          <span class="card-title">按环节路由</span>
          <div class="card-actions">
            <el-button :disabled="!canWrite" :loading="store.saving" type="primary" @click="saveRouting">保存路由</el-button>
            <el-button :loading="store.loading" @click="store.loadIsolation()">隔离自检</el-button>
            <span v-if="!canWrite" class="denied">只读面：保存已禁用</span>
          </div>
        </div>
      </template>

      <el-alert
        v-if="store.isolation"
        :type="store.isolation.isolated ? 'success' : 'error'"
        :closable="false"
        class="mb"
        :title="
          store.isolation.isolated
            ? `盲评隔离成立：${store.isolation.generator?.model_ref} ≠ ${store.isolation.reviewer?.model_ref}`
            : `盲评隔离不成立：plan 与 plan_review 为同一模型，plan_review 环节将直接失败`
        "
        :description="store.isolation.isolated ? '' : store.isolation.message ?? ''"
      />

      <el-table :data="routingRows" size="small">
        <el-table-column label="环节" width="150">
          <template #default="{ row }">{{ STAGE_LABELS[row.stage] ?? row.stage }}</template>
        </el-table-column>
        <el-table-column label="供应商" min-width="170">
          <template #default="{ row }">
            <el-select v-model="row.model_config_id" placeholder="选择供应商" size="small" class="grow" @change="onProviderChange(row)">
              <el-option v-for="config in store.configs" :key="config.id" :label="config.name" :value="config.id" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="模型" min-width="190">
          <template #default="{ row }">
            <el-select v-model="row.model_id" placeholder="选择模型" size="small" class="grow" :disabled="!row.model_config_id">
              <el-option
                v-for="model in modelsOf(row.model_config_id)"
                :key="model.model_id"
                :label="model.label || model.model_id"
                :value="model.model_id"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="temperature" width="130">
          <template #default="{ row }">
            <el-input-number v-model="row.temperature" :min="0" :max="2" :step="0.1" size="small" controls-position="right" />
          </template>
        </el-table-column>
        <el-table-column label="max_tokens" width="140">
          <template #default="{ row }">
            <el-input-number v-model="row.max_tokens" :min="1" :step="128" size="small" controls-position="right" />
          </template>
        </el-table-column>
        <el-table-column label="生效来源" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.configured ? 'success' : 'info'">{{ sourceLabel(row.source) }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ============ 成本双线 ============ -->
    <el-card class="card" shadow="never">
      <template #header>
        <div class="card-head">
          <span class="card-title">成本双线</span>
          <el-button @click="store.loadCost()">刷新成本</el-button>
        </div>
      </template>

      <div v-if="store.costSummary" class="cost">
        <div class="cost-lines">
          <div class="cost-line">
            <span class="cost-label">护栏值（硬熔断）</span>
            <span class="cost-value">{{ fmt(store.costSummary.limit_usd) }} USD</span>
          </div>
          <div class="cost-line">
            <span class="cost-label">演示配额（只告警）</span>
            <span class="cost-value">{{ fmt(store.costSummary.quota_usd) }} USD</span>
          </div>
          <div class="cost-line">
            <span class="cost-label">已用（实时）</span>
            <span class="cost-value">{{ fmt(store.costSummary.used_usd) }} USD</span>
          </div>
          <div class="cost-line">
            <span class="cost-label">回放已省</span>
            <span class="cost-value">{{ fmt(store.costSummary.replay_saved_usd) }} USD</span>
          </div>
        </div>

        <div class="bar-wrap" role="img" :aria-label="`已用 ${fmt(store.costSummary.used_usd)} USD`">
          <div class="bar-track">
            <div
              class="bar-used"
              :class="{ 'bar-used--warn': store.costSummary.quota_exceeded && !store.costSummary.limit_exceeded, 'bar-used--danger': store.costSummary.limit_exceeded }"
              :style="{ width: `${Math.min(100, store.quotaRatio * 100)}%` }"
            />
            <div class="bar-marker" :style="{ left: `${quotaPosition}%` }" />
          </div>
          <div class="bar-legend">
            <span>0</span>
            <span class="bar-legend-quota" :style="{ left: `${quotaPosition}%` }">
              配额 {{ fmt(store.costSummary.quota_usd) }}
            </span>
            <span>{{ fmt(store.costSummary.limit_usd) }}（护栏）</span>
          </div>
        </div>

        <el-alert
          v-if="store.costSummary.limit_exceeded"
          type="error"
          :closable="false"
          class="mt"
          title="已超过成本护栏，自动模式将被熔断（由 WP10 护栏判定）"
        />
        <el-alert
          v-else-if="store.costSummary.quota_exceeded"
          type="warning"
          :closable="false"
          class="mt"
          title="已超过演示配额：仅告警，不影响运行"
        />
        <el-alert
          v-if="store.costSummary.warning"
          type="info"
          :closable="false"
          class="mt"
          :title="store.costSummary.warning"
        />

        <div class="cost-meta">
          <span>调用 {{ store.costSummary.calls }} 次（回放 {{ store.costSummary.replay_calls }} 次）</span>
          <span>失败 {{ store.costSummary.failed_calls }} 次</span>
          <span>缺价调用 {{ store.costSummary.unknown_price_calls }} 次</span>
          <span>数据源：GET /costs/summary</span>
        </div>

        <el-table :data="stageCostRows" size="small" class="mt" empty-text="暂无调用记录">
          <el-table-column label="环节" width="150">
            <template #default="{ row }">{{ STAGE_LABELS[row.stage] ?? row.stage }}</template>
          </el-table-column>
          <el-table-column prop="stage" label="stage 标识" min-width="120" />
          <el-table-column label="成本（USD）" width="140">
            <template #default="{ row }">{{ fmt(row.cost) }}</template>
          </el-table-column>
        </el-table>
      </div>
      <el-skeleton v-else-if="store.loading" :rows="3" animated />
      <el-empty
        v-else
        description="成本摘要未获取（GET /costs/summary 失败或未返回）：不显示 0 值冒充已用成本，可点「重试读取设置」"
      >
        <el-button size="small" @click="store.loadCost(session.currentProjectId)">重新读取成本</el-button>
      </el-empty>
    </el-card>

    <!-- ============ 数据源健康 ============ -->
    <el-card class="card" shadow="never">
      <template #header>
        <div class="card-head">
          <span class="card-title">数据源健康</span>
          <div>
            <el-button @click="store.loadSourceHealth()">立即自检</el-button>
            <router-link to="/papers/feed" class="link">前往论文库查看健康条</router-link>
          </div>
        </div>
      </template>
      <el-table :data="store.sourceHealth" size="small" empty-text="点击「立即自检」拉取四源状态（arXiv / Semantic Scholar / OpenAlex / GitHub）">
        <el-table-column prop="name" label="数据源" width="180" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.ok === true" size="small" type="success">正常</el-tag>
            <el-tag v-else-if="row.ok === false" size="small" type="danger">异常</el-tag>
            <span v-else class="muted">未知</span>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="260">
          <template #default="{ row }">
            <span class="muted">{{ row.detail ?? row.message ?? '—' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- ============ 供应商编辑弹窗 ============ -->
    <el-dialog v-model="dialogVisible" :title="dialogMode === 'create' ? '新增供应商' : '编辑供应商'" width="760px">
      <el-form label-width="96px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="例如 deepseek / siliconflow（会作为 model_ref 的 provider 段）" />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="https://api.deepseek.com/v1" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="dialogMode === 'create' ? '明文 Key，或 env:LLM_DEFAULT_API_KEY' : '留空表示不修改'"
          />
        </el-form-item>
        <el-form-item label="默认供应商">
          <el-switch v-model="form.is_default" />
        </el-form-item>
        <el-form-item label="模型列表">
          <div class="models">
            <div v-for="(model, index) in form.models" :key="index" class="model-row">
              <el-input v-model="model.model_id" placeholder="model_id" class="grow" />
              <el-input v-model="model.label" placeholder="展示名（可选）" class="grow" />
              <el-input-number v-model="model.input_price" :min="0" :step="0.1" placeholder="输入价" controls-position="right" />
              <el-input-number v-model="model.output_price" :min="0" :step="0.1" placeholder="输出价" controls-position="right" />
              <el-input-number v-model="model.price_unit" :min="1" :step="1000" controls-position="right" />
              <el-button text type="danger" @click="form.models.splice(index, 1)">移除</el-button>
            </div>
            <el-button @click="addModelRow">添加模型</el-button>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="store.saving" @click="submitDialog">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { useDemoSession } from '@/api/demo'
import {
  LLM_STAGES,
  STAGE_LABELS,
  type ModelConfig,
  type ModelEntry,
  type RoutingEntry,
  type RoutingStage,
} from '@/api/models'
import ViewStatePanel from '@/components/ViewStatePanel.vue'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'

/**
 * 路由表格行。
 *
 * 刻意不继承 `Partial<RoutingEntry>`：可选字段的 `undefined` 语义与接口回传的 `null`
 * 不一致（`null` 表示「未配置」，`undefined` 表示「不提交」），混用会把 null 静默丢成 undefined。
 */
interface RoutingRow {
  stage: string
  configured: boolean
  source: string
  model_config_id?: number | null
  model_id?: string | null
  temperature?: number | null
  max_tokens?: number | null
  purpose?: string | null
}

const store = useSettingsStore()
const session = useSessionStore()
/** 访问面以服务端 `/owner/session` 判定为准（前端不自行推断"我认为可写"） */
const demoSession = useDemoSession()

/** 全部写操作都要求 Owner：public_demo 匿名面前置禁用并给出原因 */
const canWrite = computed(() => store.hasOwnerToken)

/**
 * 权限态：只读面下写入口已**前置禁用**；若本次请求确实收到 403，再说明「已被拒绝」。
 * 依据 store 记录的真实 HTTP 状态码判断，不靠对错误文案做字符串匹配。
 */
const permissionDenied = computed(() => !canWrite.value || store.errorStatus === 403)
const permissionTitle = computed(() =>
  store.errorStatus === 403
    ? '权限受限：该写操作已被服务端拒绝（403 owner_token_required）'
    : '只读演示面：供应商 / 路由等写入口已前置禁用（非请求被拒）',
)

const permissionNote = computed(() => {
  const reason = demoSession.ownerSession?.permissions?.reason
  return [
    '供应商增删改、连通性测试、环节路由保存均为 owner_only 写操作；当前会话未携带有效 X-Owner-Token。',
    reason ?? demoSession.error ?? '服务端访问面判定未获取。',
    '恢复方式：在上方「访问面」卡片填入服务端 OWNER_TOKEN（仅存本机 sessionStorage）。',
  ].join(' ')
})

/** 降级：盲评隔离未成立 / 存在缺价模型 / 存在降级数据源 / 成本不完整 */

const ownerTokenDraft = ref(store.ownerToken)
const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingId = ref<number | null>(null)

const form = reactive<{
  name: string
  base_url: string
  api_key: string
  is_default: boolean
  models: ModelEntry[]
}>({
  name: '',
  base_url: '',
  api_key: '',
  is_default: false,
  models: [],
})

const routingDraft = ref<Record<string, RoutingRow>>({})

const routingRows = computed<RoutingRow[]>(() =>
  LLM_STAGES.map((stage) => routingDraft.value[stage]).filter(Boolean),
)

const quotaPosition = computed(() => {
  const summary = store.costSummary
  if (!summary || !summary.limit_usd) return 0
  return Math.min(100, (summary.quota_usd / summary.limit_usd) * 100)
})

const stageCostRows = computed(() =>
  Object.entries(store.costSummary?.breakdown_by_stage ?? {}).map(([stage, cost]) => ({ stage, cost })),
)

function fmt(value: number | null | undefined): string {
  return (value ?? 0).toFixed(4)
}

function keySourceLabel(source: string): string {
  if (source === 'env_ref') return 'env 引用'
  if (source === 'encrypted') return '加密存储'
  return '未配置'
}

function sourceLabel(source: string): string {
  switch (source) {
    case 'project':
      return '项目级'
    case 'global':
      return '全局'
    case 'env_default':
      return '环境兜底'
    case 'env_fallback':
      return '备用兜底'
    case 'explicit':
      return '显式指定'
    default:
      return '未配置'
  }
}

function modelsOf(configId?: number | null): ModelEntry[] {
  if (!configId) return []
  return store.configs.find((item) => item.id === configId)?.models ?? []
}

function rebuildDraft(): void {
  const draft: Record<string, RoutingRow> = {}
  const configured = new Map<string, RoutingEntry>()
  store.routingEntries.forEach((entry) => configured.set(entry.stage, entry))
  store.routingStages.forEach((stage: RoutingStage) => {
    const existing = configured.get(stage.stage)
    draft[stage.stage] = {
      stage: stage.stage,
      configured: stage.configured,
      source: stage.source,
      model_config_id: existing?.model_config_id ?? null,
      model_id: existing?.model_id ?? stage.model_id ?? null,
      temperature: existing?.temperature ?? stage.temperature ?? null,
      max_tokens: existing?.max_tokens ?? stage.max_tokens ?? null,
      purpose: existing?.purpose ?? null,
    }
  })
  // 未在生效表中的环节（通常是被解析失败）也保留一行，方便补配
  LLM_STAGES.forEach((stage) => {
    if (!draft[stage]) {
      draft[stage] = { stage, configured: false, source: 'unresolved', model_config_id: null, model_id: null }
    }
  })
  routingDraft.value = draft
}

function onProviderChange(row: RoutingRow): void {
  const models = modelsOf(row.model_config_id)
  row.model_id = models.length ? models[0].model_id : null
}

function addModelRow(): void {
  form.models.push({
    model_id: '',
    label: '',
    input_price: null,
    output_price: null,
    price_unit: 1000,
    temperature: null,
    max_tokens: null,
  })
}

function openCreateDialog(): void {
  dialogMode.value = 'create'
  editingId.value = null
  form.name = ''
  form.base_url = ''
  form.api_key = ''
  form.is_default = false
  form.models = []
  addModelRow()
  dialogVisible.value = true
}

function openEditDialog(row: ModelConfig): void {
  dialogMode.value = 'edit'
  editingId.value = row.id
  form.name = row.name
  form.base_url = row.base_url
  form.api_key = ''
  form.is_default = row.is_default
  form.models = row.models.map((model) => ({ ...model }))
  dialogVisible.value = true
}

async function submitDialog(): Promise<void> {
  const models = form.models.filter((model) => model.model_id.trim().length > 0)
  if (!form.name.trim() || !form.base_url.trim() || models.length === 0) {
    ElMessage.warning('名称、Base URL 与至少一个模型为必填项')
    return
  }
  const ok =
    dialogMode.value === 'create'
      ? await store.createConfig({
          name: form.name.trim(),
          base_url: form.base_url.trim(),
          api_key: form.api_key,
          models,
          is_default: form.is_default,
        })
      : await store.updateConfig(editingId.value as number, {
          name: form.name.trim(),
          base_url: form.base_url.trim(),
          ...(form.api_key ? { api_key: form.api_key } : {}),
          models,
          is_default: form.is_default,
        })
  if (ok) {
    dialogVisible.value = false
    await Promise.all([store.loadRouting(), store.loadIsolation()])
    rebuildDraft()
  }
}

async function removeConfig(row: ModelConfig): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除供应商「${row.name}」？被环节路由引用时将拒绝删除。`, '删除确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  const ok = await store.removeConfig(row.id)
  if (ok) {
    await store.loadRouting()
    rebuildDraft()
  }
}

async function saveRouting(): Promise<void> {
  const entries = routingRows.value
    .filter((row) => row.model_config_id && row.model_id)
    .map((row) => ({
      stage: row.stage,
      model_config_id: row.model_config_id as number,
      model_id: row.model_id as string,
      temperature: row.temperature ?? null,
      max_tokens: row.max_tokens ?? null,
      purpose: row.purpose ?? null,
    }))
  if (!entries.length) {
    ElMessage.warning('请至少为一个环节选择供应商与模型')
    return
  }
  const ok = await store.saveRouting(entries)
  if (ok) rebuildDraft()
}

function applyOwnerToken(): void {
  store.updateOwnerToken(ownerTokenDraft.value.trim())
  void demoSession.refresh()
  ElMessage.success('令牌已应用到本次会话，正在重新校验访问面')
}

async function refresh(): Promise<void> {
  await store.loadAll()
  rebuildDraft()
}

onMounted(async () => {
  await refresh()
  rebuildDraft()
})
</script>

<style scoped>
.settings {
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  color: var(--color-text-primary, var(--text-primary));
  background: var(--color-bg-page, var(--bg-page));
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.denied {
  padding: 0 var(--space-2);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-pill);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.page-title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}


.card {
  border: 1px solid var(--color-border, var(--border-color));
  border-radius: 8px;
  background: var(--color-bg-card, var(--bg-card));
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.card-title {
  font-weight: 600;
}

.row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.grow {
  flex: 1 1 auto;
}

.mb {
  margin-bottom: 12px;
}

.mt {
  margin-top: 12px;
}

.tag {
  margin-right: 6px;
}

.mono {
  font-family: var(--font-mono, ui-monospace, monospace);
}

.muted {
  color: var(--color-text-secondary, var(--text-secondary));
  font-size: var(--font-size-xs);
}


.link {
  margin-left: 12px;
  font-size: var(--font-size-sm);
  color: var(--color-brand, var(--brand));
}

.test-result {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  font-size: var(--font-size-sm);
}

.models {
  width: 100%;
}

.model-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.cost-lines {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}

.cost-line {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--color-border, var(--border-color));
  border-radius: 6px;
  background: var(--color-bg-subtle, var(--bg-subtle));
}

.cost-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary, var(--text-secondary));
}

.cost-value {
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.bar-wrap {
  margin-top: 16px;
}

.bar-track {
  position: relative;
  height: 12px;
  border-radius: 6px;
  background: var(--color-bg-subtle, var(--bg-subtle));
  border: 1px solid var(--color-border, var(--border-color));
  overflow: hidden;
}

.bar-used {
  height: 100%;
  transition: width 0.3s ease;
  background: var(--color-brand, var(--brand));
}

.bar-used--warn {
  background: var(--color-warning, var(--warning));
}

.bar-used--danger {
  background: var(--color-danger, var(--danger));
}

.bar-marker {
  position: absolute;
  top: -2px;
  bottom: -2px;
  width: 2px;
  background: var(--color-text-primary, var(--text-primary));
  opacity: 0.6;
}

.bar-legend {
  position: relative;
  display: flex;
  justify-content: space-between;
  margin-top: 6px;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary, var(--text-secondary));
}

.bar-legend-quota {
  position: absolute;
  transform: translateX(-50%);
  white-space: nowrap;
}

.cost-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 12px;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary, var(--text-secondary));
}
</style>
