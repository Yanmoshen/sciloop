<!--
  Copyright 2026 SciLoop contributors
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  设置页：内容区顶部 tabbar 切三页（通用设置 / 模型与成本设置 / 阅读设置）。

  设计层纪律
  ----------
  本页渲染在 `HomeLayout` 的 `.sl-home` 内，**只用壳层的 `--h-*` 令牌**（n8n design
  language），不再引 `tokens.css` 的学术蓝变量——两套混用正是「风格不一致」的来源。
  视觉基线：用户已确认的 `preview/settings.html`。

  接口
  ----
  - 供应商 / 按环节路由 / 成本双线 / 数据源健康：既有 `stores/settings`（WP02），逻辑未改
  - 阅读设置：`GET /settings/reading`（公开只读）/ `PUT /settings/reading`（Owner）
    默认值一律取服务端返回的 `defaults`，前端不抄副本；写入只提交**改动的键**（部分更新）

  权限
  ----
  匿名（`public_demo`）**前置禁用**所有写控件（走 disabled 态），而不是让用户点了才吃 403。
-->
<template>
  <section class="sl-settings">
    <!-- 模块子页切换放在内容区顶部：左栏是单一常驻导航，不塞设置子项 -->
    <nav class="tabbar" aria-label="设置分组">
      <button
        v-for="tab in TABS"
        :key="tab.key"
        class="tabbar__btn"
        :class="{ 'tabbar__btn--on': activeTab === tab.key }"
        type="button"
        :aria-current="activeTab === tab.key ? 'page' : undefined"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- ================= 1 通用设置 ================= -->
    <section v-show="activeTab === 'general'" class="pane" aria-label="通用设置">
      <header class="page-head">
        <h1 class="page-title">通用设置</h1>
      </header>

      <!-- 访问面（从原「模型与成本设置」迁出） -->
      <div class="card">
        <div class="card__head">
          <span class="card__title">访问面（Owner 模式）</span>
          <div class="card__actions">
            <span class="pill" :class="demoSession.isOwner ? 'pill--ok' : 'pill--quiet'">
              <i class="dot" />{{ sessionPill }}
            </span>
            <button
              class="btn--link"
              type="button"
              :disabled="demoSession.loading"
              @click="demoSession.refresh()"
            >
              重新校验访问面
            </button>
          </div>
        </div>
        <div class="row">
          <div class="row__body">
            <input
              v-model="ownerTokenDraft"
              class="input input--mono"
              type="password"
              autocomplete="off"
              placeholder="粘贴研究者密钥后即可修改配置（只保存在这台电脑上）"
              @keyup.enter="applyOwnerToken"
            />
          </div>
          <div class="row__end">
            <button
              class="btn btn--primary"
              type="button"
              :disabled="!ownerTokenDraft.trim()"
              @click="applyOwnerToken"
            >
              应用令牌
            </button>
          </div>
        </div>
        <p v-if="demoSession.error" class="state state--error">{{ demoSession.error }}</p>
      </div>

      <!-- 关于 -->
      <div class="card">
        <div class="card__head">
          <span class="card__title">关于</span>
        </div>
        <div class="rows">
          <div class="row">
            <span class="row__label">应用</span>
            <div class="row__body">
              <span>{{ APP_NAME }}</span>
              <span class="pill pill--quiet">{{ appVersion }}</span>
            </div>
          </div>
          <div class="row">
            <span class="row__label">许可证</span>
            <div class="row__body"><span>Apache License 2.0</span></div>
          </div>
        </div>
      </div>
    </section>

    <!-- ================= 2 模型与成本设置 ================= -->
    <section v-show="activeTab === 'model'" class="pane" aria-label="模型与成本设置">
      <header class="page-head">
        <h1 class="page-title">模型与成本设置</h1>
      </header>

      <p v-if="store.error" class="state state--error">
        {{ store.errorCode ?? 'unknown_error' }}：{{ store.error }}
        <button class="btn--link" type="button" @click="refresh">重试读取设置</button>
      </p>
      <p v-else-if="store.notice" class="state"><span class="pill pill--ok">{{ store.notice }}</span></p>

      <!-- 供应商（双栏，交互照 Cherry Studio「模型服务」） -->
      <div class="card">
        <div class="card__head">
          <span class="card__title">模型服务</span>
          <div class="card__actions">
            <span v-if="store.pricingIncomplete.length" class="pill pill--warn" title="这些供应商下有模型未配置单价，其 cost_usd 记为 null（禁止估算），成本护栏无法精确判定">
              单价未配置 {{ store.pricingIncomplete.length }} 家
            </span>
          </div>
        </div>

        <ProviderSettingsPane :can-write="canWrite" @create="openCreate" />

        <div v-if="store.lastTestResult" class="card__foot">
          <span class="foot__label">最近一次连通性测试</span>
          <span class="tag-mono">{{ store.lastTestResult.model_ref ?? '未指定模型' }}</span>
          <span class="pill" :class="store.lastTestResult.ok ? 'pill--ok' : 'pill--warn'">
            {{ store.lastTestResult.ok ? '成功' : store.lastTestResult.error_kind ?? '失败' }}
          </span>
          <span class="pill pill--quiet">{{ store.lastTestResult.latency_ms ?? '—' }} ms</span>
          <span
            class="foot__preview"
            :title="
              (store.lastTestResult.ok ? store.lastTestResult.reply_preview : store.lastTestResult.message) ??
              ''
            "
          >
            {{ store.lastTestResult.ok ? store.lastTestResult.reply_preview : store.lastTestResult.message }}
          </span>
        </div>
      </div>

      <!-- 按环节路由 -->
      <div class="card card--flush">
        <div class="card__head">
          <span class="card__title">按环节路由</span>
          <span v-if="store.error" class="pill pill--warn">
            {{ store.errorCode ?? 'failed' }}：{{ store.error }}
          </span>
          <span v-else-if="store.notice" class="pill pill--ok">{{ store.notice }}</span>
          <div class="card__actions">
            <span
              v-if="store.isolation"
              class="pill"
              :class="store.isolation.isolated ? 'pill--ok' : 'pill--warn'"
              :title="isolationTitle"
            >
              <i class="dot" />{{ store.isolation.isolated ? '盲评隔离成立' : '盲评隔离不成立' }}
            </span>
            <button class="btn btn--sm" type="button" :disabled="store.loading" @click="store.loadIsolation()">
              隔离自检
            </button>
            <button
              class="btn btn--primary btn--sm"
              type="button"
              :disabled="!canWrite || store.saving"
              @click="saveRouting"
            >
              保存路由
            </button>
          </div>
        </div>

        <table class="table">
          <thead>
            <tr>
              <th>环节</th>
              <th>供应商</th>
              <th>模型</th>
              <th class="cell-narrow">temperature</th>
              <th class="cell-narrow">max_tokens</th>
              <th class="cell-narrow">生效来源</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in routingRows" :key="row.stage">
              <td>{{ STAGE_LABELS[row.stage] ?? row.stage }}</td>
              <td>
                <PsSelect
                  block
                  :model-value="row.model_config_id == null ? '' : String(row.model_config_id)"
                  :options="providerOptions"
                  :disabled="!canWrite"
                  aria-label="选择供应商"
                  @change="onProviderPick(row, $event)"
                />
              </td>
              <td>
                <PsSelect
                  block
                  :model-value="row.model_id ?? ''"
                  :options="modelOptions(row.model_config_id)"
                  :disabled="!canWrite || !row.model_config_id"
                  aria-label="选择模型"
                  @change="onModelPick(row, $event)"
                />
              </td>
              <td>
                <input
                  class="input input--sm"
                  type="number"
                  min="0"
                  max="2"
                  step="0.1"
                  placeholder="未设"
                  :value="row.temperature ?? ''"
                  :disabled="!canWrite"
                  @change="onNumberInput(row, 'temperature', $event)"
                />
              </td>
              <td>
                <input
                  class="input input--sm"
                  type="number"
                  min="1"
                  step="128"
                  placeholder="未设"
                  :value="row.max_tokens ?? ''"
                  :disabled="!canWrite"
                  @change="onNumberInput(row, 'max_tokens', $event)"
                />
              </td>
              <td>
                <span class="pill pill--quiet">{{ sourceLabel(row.source) }}</span>
              </td>
            </tr>
          </tbody>
        </table>

        <p v-if="routingError" class="state state--error card__foot">{{ routingError }}</p>
      </div>

      <!-- 成本双线 -->
      <div class="card">
        <div class="card__head">
          <span class="card__title">成本双线</span>
          <div class="card__actions">
            <span
              v-if="store.costSummary?.warning"
              class="pill pill--warn"
              :title="store.costSummary?.warning ?? ''"
            >
              有告警
            </span>
            <span
              v-if="nonUsdSummary"
              class="pill pill--warn"
              :title="nonUsdTitle"
            >
              {{ nonUsdSummary }}
            </span>
            <span class="pill pill--quiet">护栏优先于配额</span>
            <button class="btn btn--sm" type="button" :disabled="store.loading" @click="reloadCost">刷新成本</button>
          </div>
        </div>

        <template v-if="store.costSummary">
          <div class="rows">
            <div class="row">
              <span class="row__label">硬护栏</span>
              <div class="row__body">
                <div class="bar">
                  <div class="bar__fill" :style="{ width: `${limitRatio}%` }" />
                </div>
              </div>
              <div class="row__end">
                <span class="pill pill--quiet">{{ usd(store.costSummary.limit_usd) }} USD</span>
                <span class="pill" :class="store.costSummary.limit_exceeded ? 'pill--warn' : 'pill--ok'">
                  {{ store.costSummary.limit_exceeded ? '已触发' : '未触发' }}
                </span>
              </div>
            </div>
            <div class="row">
              <span class="row__label">演示配额</span>
              <div class="row__body">
                <div class="bar">
                  <div
                    class="bar__fill"
                    :class="store.costSummary.quota_exceeded ? 'bar__fill--over' : 'bar__fill--quiet'"
                    :style="{ width: `${quotaRatio}%` }"
                  />
                </div>
              </div>
              <div class="row__end">
                <span class="pill pill--quiet">{{ usd(store.costSummary.quota_usd) }} USD</span>
                <span class="pill" :class="store.costSummary.quota_exceeded ? 'pill--warn' : 'pill--ok'">
                  {{ store.costSummary.quota_exceeded ? '已触发' : '未触发' }}
                </span>
              </div>
            </div>
          </div>

          <div class="metrics">
            <div class="metric">
              <div class="metric__label">真实调用</div>
              <div class="metric__value">{{ usd(store.costSummary.used_usd) }} USD</div>
            </div>
            <div class="metric">
              <div class="metric__label">桩调用（不计费）</div>
              <div class="metric__value">{{ usd(store.costSummary.stub_saved_usd) }} USD</div>
            </div>
            <div class="metric">
              <div class="metric__label">回放（不计费）</div>
              <div class="metric__value">{{ usd(store.costSummary.replay_saved_usd) }} USD</div>
            </div>
            <div class="metric">
              <div class="metric__label">调用总数 / 失败</div>
              <div class="metric__value">
                {{ store.costSummary.calls.toLocaleString() }} / {{ store.costSummary.failed_calls }}
              </div>
            </div>
          </div>

          <table v-if="stageCostRows.length" class="table">
            <thead>
              <tr>
                <th>环节</th>
                <th>stage 标识</th>
                <th class="cell-narrow">成本（USD）</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in stageCostRows" :key="row.stage">
                <td>{{ STAGE_LABELS[row.stage] ?? row.stage }}</td>
                <td class="mono">{{ row.stage }}</td>
                <td>{{ usd(row.cost, 4) }}</td>
              </tr>
            </tbody>
          </table>
        </template>

        <p v-else-if="store.loading" class="state">正在读取成本摘要…</p>
        <div v-else class="empty">
          <span class="pill pill--quiet">成本摘要未获取（非 0 值）</span>
          <button class="btn btn--sm" type="button" @click="reloadCost">重新读取成本</button>
        </div>
      </div>

    </section>

    <!-- ================= 3 拉取设置 ================= -->
    <section v-show="activeTab === 'fetch'" class="pane" aria-label="拉取设置">
      <header class="page-head">
        <h1 class="page-title">拉取设置</h1>
      </header>

      <div class="card card--flush">
        <div class="card__head">
          <span class="card__title">数据源健康</span>
          <div class="card__actions">
            <router-link class="btn--link" to="/papers/feed">前往论文库查看健康条</router-link>
            <button class="btn btn--sm" type="button" :disabled="store.loading" @click="store.loadSourceHealth()">
              立即自检
            </button>
          </div>
        </div>

        <table v-if="store.sourceHealth.length" class="table">
          <thead>
            <tr>
              <th>数据源</th>
              <th class="cell-narrow">状态</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in store.sourceHealth" :key="item.name">
              <td>{{ item.name }}</td>
              <td>
                <span v-if="item.ok === true" class="pill pill--ok"><i class="dot" />ok</span>
                <span v-else-if="item.ok === false" class="pill pill--warn"><i class="dot" />降级</span>
                <span v-else class="pill pill--quiet">未知</span>
              </td>
              <td class="detail-cell">{{ sourceDetail(item) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="state card__foot">暂无数据</p>
      </div>
    </section>

    <!-- ================= 3 阅读设置 ================= -->
    <section v-show="activeTab === 'reading'" class="pane" aria-label="阅读设置">
      <header class="page-head">
        <h1 class="page-title">阅读设置</h1>
        <div class="head-actions">
          <span v-if="readingDirty" class="pill pill--warn">未保存</span>
          <button class="btn" type="button" :disabled="!canEditReading" @click="resetReading">恢复默认</button>
          <button
            class="btn btn--primary"
            type="button"
            :disabled="!canEditReading || readingSaving"
            @click="saveReading"
          >
            {{ readingSaving ? '保存中…' : readingSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </header>

      <p v-if="readingError" class="state state--error">
        {{ readingError }}
        <button class="btn--link" type="button" @click="loadReading">重试读取设置</button>
      </p>

      <p v-if="readingLoading" class="state">正在读取阅读设置…</p>

      <template v-if="readingDraft">
        <div class="card">
          <div class="card__head">
            <span class="card__title">默认阅读版本</span>
            <div class="card__actions">
              <span class="pill pill--ok"><i class="dot" />全站生效</span>
            </div>
          </div>
          <div class="rows">
            <div class="row">
              <span class="row__label">打开时的版本</span>
              <div class="row__body">
                <div class="seg">
                  <button
                    v-for="option in KIND_OPTIONS"
                    :key="option.value"
                    class="seg__btn"
                    :class="{ 'seg__btn--on': readingDraft.default_kind === option.value }"
                    type="button"
                    :disabled="!canEditReading"
                    @click="setKind(option.value)"
                  >
                    {{ option.label }}
                  </button>
                </div>
              </div>
            </div>
            <div class="row">
              <span class="row__label">缺版本时</span>
              <div class="row__body">
                <div class="seg">
                  <button
                    v-for="option in MISSING_OPTIONS"
                    :key="option.value"
                    class="seg__btn"
                    :class="{ 'seg__btn--on': readingDraft.missing_version_action === option.value }"
                    type="button"
                    :disabled="!canEditReading"
                    @click="setMissingAction(option.value)"
                  >
                    {{ option.label }}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="grid2">
          <div class="card">
            <div class="card__head">
              <span class="card__title">排版</span>
            </div>
            <div class="rows">
              <div class="row">
                <span class="row__label">正文字号</span>
                <div class="row__body">
                  <input
                    class="range"
                    type="range"
                    min="14"
                    max="24"
                    step="1"
                    :value="readingDraft.font_size"
                    :disabled="!canEditReading"
                    @input="setFontSize"
                  />
                </div>
                <div class="row__end">
                  <span class="pill pill--quiet">{{ readingDraft.font_size }} px</span>
                </div>
              </div>
              <div class="row">
                <span class="row__label">行距</span>
                <div class="row__body">
                  <div class="seg">
                    <button
                      v-for="option in LINE_HEIGHT_OPTIONS"
                      :key="option.value"
                      class="seg__btn"
                      :class="{ 'seg__btn--on': readingDraft.line_height === option.value }"
                      type="button"
                      :disabled="!canEditReading"
                      @click="setLineHeight(option.value)"
                    >
                      {{ option.label }}
                    </button>
                  </div>
                </div>
              </div>
              <div class="row">
                <span class="row__label">对照阅读</span>
                <div class="row__body">
                  <button
                    class="sw"
                    type="button"
                    role="switch"
                    :aria-checked="readingDraft.pair_view ? 'true' : 'false'"
                    :disabled="!canEditReading"
                    @click="setPairView(!readingDraft.pair_view)"
                  >
                    <span class="sw__knob" />
                  </button>
                  <span class="pill pill--quiet">{{ readingDraft.pair_view ? '双栏对照' : '单栏' }}</span>
                </div>
              </div>
            </div>
          </div>

          <div class="card">
            <div class="card__head">
              <span class="card__title">阅读辅助</span>
            </div>
            <div class="rows">
              <div class="row">
                <span class="row__label">批注默认可见</span>
                <div class="row__body">
                  <button
                    class="sw"
                    type="button"
                    role="switch"
                    :aria-checked="readingDraft.annotations_visible ? 'true' : 'false'"
                    :disabled="!canEditReading"
                    @click="setAnnotationsVisible(!readingDraft.annotations_visible)"
                  >
                    <span class="sw__knob" />
                  </button>
                  <span class="pill pill--quiet">{{ readingDraft.annotations_visible ? '显示' : '隐藏' }}</span>
                </div>
              </div>
              <div class="row">
                <span class="row__label">原文锚点高亮</span>
                <div class="row__body">
                  <button
                    class="sw"
                    type="button"
                    role="switch"
                    :aria-checked="readingDraft.anchor_highlight ? 'true' : 'false'"
                    :disabled="!canEditReading"
                    @click="setAnchorHighlight(!readingDraft.anchor_highlight)"
                  >
                    <span class="sw__knob" />
                  </button>
                  <span class="pill pill--quiet">{{ readingDraft.anchor_highlight ? '跳转时定位' : '不定位' }}</span>
                </div>
              </div>
              <div class="row">
                <span class="row__label">术语收藏</span>
                <div class="row__body">
                  <span class="pill pill--quiet">已收藏 {{ readingDraft.favorite_terms.length }} 个</span>
                  <button
                    class="btn btn--sm"
                    type="button"
                    :disabled="!canEditReading"
                    @click="termsOpen = !termsOpen"
                  >
                    {{ termsOpen ? '收起' : '管理' }}
                  </button>
                </div>
              </div>
            </div>

            <div v-if="termsOpen && canEditReading" class="terms">
              <div class="terms__chips">
                <span v-for="term in readingDraft.favorite_terms" :key="term" class="pill">
                  {{ term }}
                  <button class="terms__del" type="button" :aria-label="`移除 ${term}`" @click="removeTerm(term)">
                    ×
                  </button>
                </span>
                <span v-if="!readingDraft.favorite_terms.length" class="pill pill--quiet">尚未收藏术语</span>
              </div>
              <div class="terms__row">
                <input
                  v-model="termDraft"
                  class="input"
                  type="text"
                  maxlength="60"
                  placeholder="输入术语后回车添加（最多 50 个）"
                  @keyup.enter="addTerm"
                />
                <button class="btn btn--sm" type="button" @click="addTerm">添加</button>
              </div>
              <p v-if="termsError" class="state state--error">{{ termsError }}</p>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card__head">
            <span class="card__title">实时预览</span>
            <span class="pill pill--quiet">示例段落 · 非真实论文</span>
          </div>
          <div class="reader-preview">
            <div class="reader-preview__title" :style="previewStyle">Attention Is All You Need</div>
            <div v-if="!readingDraft.pair_view">
              <p :style="previewStyle">
                The dominant sequence transduction models are based on complex recurrent or convolutional neural
                networks that include an encoder and a decoder. We propose a new simple network architecture, the
                Transformer, based solely on attention mechanisms.
              </p>
            </div>
            <div v-else class="reader-preview__pair">
              <div class="reader-preview__col">
                <h4>原文</h4>
                <p :style="previewStyle">
                  The dominant sequence transduction models are based on complex recurrent or convolutional neural
                  networks.
                </p>
              </div>
              <div class="reader-preview__col">
                <h4>中文</h4>
                <p :style="previewStyle">
                  主流的序列转换模型建立在包含编码器与解码器的复杂循环或卷积神经网络之上。
                </p>
              </div>
            </div>
          </div>
        </div>
      </template>
    </section>

    <VendorDialog v-model="dialogOpen" :can-write="canWrite" @saved="onVendorSaved" />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { ApiError } from '@/api/client'
import { humanError } from '@/utils/messages'
import { useDemoSession } from '@/api/demo'
import {
  LLM_STAGES,
  STAGE_LABELS,
  type ModelEntry,
  type RoutingEntry,
  type RoutingStage,
  type SourceHealthItem,
} from '@/api/models'
import {
  getReadingSettings,
  putReadingSettings,
  type LineHeight,
  type MissingVersionAction,
  type ReadingKind,
  type ReadingSettings,
  type SettingsScope,
} from '@/api/settings'
import ProviderSettingsPane from '@/components/settings/ProviderSettingsPane.vue'
import PsSelect, { type PsOption } from '@/components/settings/PsSelect.vue'
import VendorDialog from '@/components/settings/VendorDialog.vue'
import { useSessionStore } from '@/stores/session'
import { useSettingsStore } from '@/stores/settings'

/* ------------------------------------------------------------------ *
 * 页签
 * ------------------------------------------------------------------ */
import { useQueryTab } from '@/utils/queryTab'

type TabKey = 'general' | 'model' | 'fetch' | 'reading'

const TABS: ReadonlyArray<{ key: TabKey; label: string }> = [
  { key: 'general', label: '通用设置' },
  { key: 'model', label: '模型与成本设置' },
  { key: 'fetch', label: '拉取设置' },
  { key: 'reading', label: '阅读设置' },
]

/** 页内切换：三个页签同属 `/settings`，**不改 URL、不进路由** */
const TAB_KEYS: ReadonlyArray<TabKey> = TABS.map((tab) => tab.key)
const activeTab = useQueryTab<TabKey>('tab', TAB_KEYS, 'general')

/* ------------------------------------------------------------------ *
 * 共享状态与权限
 * ------------------------------------------------------------------ */
const store = useSettingsStore()
const session = useSessionStore()
/** 访问面以服务端 `/owner/session` 判定为准（前端不自行推断"我认为可写"） */
const demoSession = useDemoSession()

/**
 * 写控件是否可用：沿用既有语义——本机会话里有 OWNER_TOKEN 即视为可写面，
 * 服务端仍会逐条校验（令牌无效时写请求会如实 403）。
 */
const canWrite = computed(() => store.hasOwnerToken)

const sessionPill = computed(() =>
  demoSession.isOwner ? '研究者身份 · 可写' : '只读浏览 · 匿名',
)

/** 应用名固定为产品名（与左栏品牌一致）；版本取 `/health` 的真实响应，不在前端手写 */
const APP_NAME = 'SciLoop'
const appVersion = computed(() => session.health?.app.version ?? '—')

function describe(error: unknown): string {
  if (error instanceof ApiError) return humanError(error)
  return error instanceof Error ? error.message : String(error)
}

/* ------------------------------------------------------------------ *
 * 通用设置：Owner 令牌（沿用既有存储，不新增第二套）
 * ------------------------------------------------------------------ */
const ownerTokenDraft = ref(store.ownerToken)

function applyOwnerToken(): void {
  const token = ownerTokenDraft.value.trim()
  if (!token) return
  store.updateOwnerToken(token)
  void demoSession.refresh()
}

/* ------------------------------------------------------------------ *
 * 模型与成本：路由草稿
 * ------------------------------------------------------------------ */
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

const routingDraft = ref<Record<string, RoutingRow>>({})
const routingError = ref('')
const dialogOpen = ref(false)

const routingRows = computed<RoutingRow[]>(() =>
  LLM_STAGES.map((stage) => routingDraft.value[stage]).filter(Boolean),
)

const stageCostRows = computed(() =>
  Object.entries(store.costSummary?.breakdown_by_stage ?? {}).map(([stage, cost]) => ({ stage, cost })),
)

const limitRatio = computed(() => {
  const summary = store.costSummary
  return summary ? ratio(summary.used_usd, summary.limit_usd) : 0
})

const quotaRatio = computed(() => {
  const summary = store.costSummary
  return summary ? ratio(summary.used_usd, summary.quota_usd) : 0
})

const isolationTitle = computed(() => {
  const isolation = store.isolation
  if (!isolation) return ''
  const generator = STAGE_LABELS[isolation.generator_stage] ?? isolation.generator_stage
  const reviewer = STAGE_LABELS[isolation.reviewer_stage] ?? isolation.reviewer_stage
  const pair = `${generator} ${isolation.generator?.model_ref ?? '—'} ≠ ${reviewer} ${
    isolation.reviewer?.model_ref ?? '—'
  }`
  return isolation.isolated ? pair : (isolation.message ?? `同一模型：${pair}`)
})

function ratio(used: number, cap: number): number {
  if (!cap) return 0
  return Math.min(100, (used / cap) * 100)
}

/**
 * 非 USD 口径的调用：**只标注，不计入护栏**（Cherry 口径不换算、不猜价）。
 * 没有非 USD 调用时不显示任何徽标。
 */
const nonUsdTitle = computed(() => {
  const summary = store.costSummary
  const currencies = summary?.non_usd_currencies ?? []
  const models = summary?.non_usd_models ?? []
  const parts = [`非 USD 定价不做汇率换算，因此不计入任何 USD 总额（护栏仅统计 USD）`]
  if (currencies.length) parts.push(`币种：${currencies.join(' / ')}`)
  if (models.length) parts.push(`模型：${models.join(' / ')}`)
  return parts.join('；')
})

const nonUsdSummary = computed(() => {
  const calls = store.costSummary?.non_usd_calls ?? 0
  if (!calls) return ''
  return `非 USD，未计入护栏 ${calls} 次`
})

/** 金额展示：默认 2 位（与展示稿一致），分环节成本保留 4 位避免小值被抹平 */
function usd(value: number | null | undefined, digits = 2): string {
  return (value ?? 0).toFixed(digits)
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

function sourceDetail(item: SourceHealthItem): string {
  const detail = item.detail ?? item.message
  return typeof detail === 'string' && detail.length > 0 ? detail : '—'
}

function modelsOf(configId?: number | null): ModelEntry[] {
  if (!configId) return []
  return store.configs.find((item) => item.id === configId)?.models ?? []
}

/** 供应商下拉选项：空值项 = 未指定（与原先占位 option 同义） */
const providerOptions = computed<PsOption[]>(() => [
  { value: '', label: '选择供应商' },
  ...store.configs.map((config) => ({ value: String(config.id), label: config.name })),
])

/** 模型下拉选项：跟随该行的供应商草稿（未选供应商时只剩空值项） */
function modelOptions(configId?: number | null): PsOption[] {
  return [
    { value: '', label: '选择模型' },
    ...modelsOf(configId).map((model) => ({
      value: model.model_id,
      label: model.label || model.model_id,
    })),
  ]
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

/** 换供应商：模型回落到该供应商的第一个模型（与原生 select 的 @change 同语义） */
function onProviderPick(row: RoutingRow, value: string): void {
  row.model_config_id = value === '' ? null : Number(value)
  const models = modelsOf(row.model_config_id)
  row.model_id = models.length ? models[0].model_id : null
}

function onModelPick(row: RoutingRow, value: string): void {
  row.model_id = value === '' ? null : value
}

function onNumberInput(row: RoutingRow, field: 'temperature' | 'max_tokens', event: Event): void {
  const raw = (event.target as HTMLInputElement).value.trim()
  row[field] = raw === '' ? null : Number(raw)
}

/** 「模型服务」左栏底部的「+ 添加服务商」：新建走 Cherry 式最小表单（名称/类型/地址/密钥） */
function openCreate(): void {
  dialogOpen.value = true
}

/** 供应商写入后：路由可选模型变了，草稿与隔离态必须跟着重算 */
async function onVendorSaved(): Promise<void> {
  await Promise.all([store.loadRouting(), store.loadIsolation()])
  rebuildDraft()
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
    routingError.value = '请至少为一个环节选择供应商与模型'
    return
  }
  routingError.value = ''
  const ok = await store.saveRouting(entries)
  if (ok) rebuildDraft()
}

function reloadCost(): void {
  void store.loadCost(session.currentProjectId)
}

async function refresh(): Promise<void> {
  await store.loadAll()
  rebuildDraft()
}

/* ------------------------------------------------------------------ *
 * 阅读设置（GET 公开只读 / PUT 需 Owner）
 * ------------------------------------------------------------------ */
const reading = ref<SettingsScope<ReadingSettings> | null>(null)
const readingDraft = ref<ReadingSettings | null>(null)
const readingLoading = ref(false)
const readingSaving = ref(false)
const readingSaved = ref(false)
const readingError = ref('')
let savedTimer: number | null = null

const KIND_OPTIONS: ReadonlyArray<{ value: ReadingKind; label: string }> = [
  { value: 'original', label: '原文' },
  { value: 'chinese', label: '中文' },
]

const MISSING_OPTIONS: ReadonlyArray<{ value: MissingVersionAction; label: string }> = [
  { value: 'fallback_original', label: '回落原文' },
  { value: 'prompt_generate', label: '提示生成' },
]

const LINE_HEIGHT_OPTIONS: ReadonlyArray<{ value: LineHeight; label: string }> = [
  { value: 1.45, label: '紧凑' },
  { value: 1.6, label: '标准' },
  { value: 1.85, label: '宽松' },
]

/** 匿名 / 未加载完成时一律禁用，并配合卡头 pill */
const canEditReading = computed(
  () => canWrite.value && readingDraft.value !== null && !readingLoading.value,
)

const previewStyle = computed(() => {
  const draft = readingDraft.value
  if (!draft) return {}
  return { fontSize: `${draft.font_size}px`, lineHeight: String(draft.line_height) }
})

function cloneSettings(source: ReadingSettings): ReadingSettings {
  return { ...source, favorite_terms: [...source.favorite_terms] }
}

function sameTerms(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((term, index) => term === b[index])
}

/**
 * 只提交**改动过的键**（服务端是部分更新）：既避免覆盖并发会话改的其他键，
 * 也避免把未改动字段当成"要写"的东西送出去。
 */
function buildPatch(): Partial<ReadingSettings> {
  const patch: Partial<ReadingSettings> = {}
  const draft = readingDraft.value
  const base = reading.value?.settings
  if (!draft || !base) return patch
  if (draft.default_kind !== base.default_kind) patch.default_kind = draft.default_kind
  if (draft.missing_version_action !== base.missing_version_action) {
    patch.missing_version_action = draft.missing_version_action
  }
  if (draft.font_size !== base.font_size) patch.font_size = draft.font_size
  if (draft.line_height !== base.line_height) patch.line_height = draft.line_height
  if (draft.pair_view !== base.pair_view) patch.pair_view = draft.pair_view
  if (draft.annotations_visible !== base.annotations_visible) {
    patch.annotations_visible = draft.annotations_visible
  }
  if (draft.anchor_highlight !== base.anchor_highlight) patch.anchor_highlight = draft.anchor_highlight
  if (!sameTerms(draft.favorite_terms, base.favorite_terms)) patch.favorite_terms = draft.favorite_terms
  return patch
}

const readingDirty = computed(() => Object.keys(buildPatch()).length > 0)

async function loadReading(): Promise<void> {
  readingLoading.value = true
  readingError.value = ''
  try {
    const payload = await getReadingSettings()
    reading.value = payload
    readingDraft.value = cloneSettings(payload.settings)
  } catch (error) {
    reading.value = null
    readingDraft.value = null
    readingError.value = describe(error)
  } finally {
    readingLoading.value = false
  }
}

function flashSaved(): void {
  readingSaved.value = true
  if (savedTimer !== null) window.clearTimeout(savedTimer)
  savedTimer = window.setTimeout(() => {
    readingSaved.value = false
  }, 1400)
}

async function saveReading(): Promise<void> {
  const patch = buildPatch()
  if (Object.keys(patch).length === 0) {
    flashSaved()
    return
  }
  readingSaving.value = true
  readingError.value = ''
  termsError.value = ''
  try {
    const payload = await putReadingSettings(patch)
    reading.value = payload
    readingDraft.value = cloneSettings(payload.settings)
    flashSaved()
  } catch (error) {
    readingError.value = describe(error)
  } finally {
    readingSaving.value = false
  }
}

/** 恢复默认：默认值来自服务端 `defaults`，前端不维护副本（改完仍需点「保存」才落库） */
function resetReading(): void {
  const defaults = reading.value?.defaults
  if (!defaults) return
  readingDraft.value = cloneSettings(defaults)
  readingError.value = ''
  termsError.value = ''
}

function setKind(value: ReadingKind): void {
  if (readingDraft.value) readingDraft.value.default_kind = value
}

function setMissingAction(value: MissingVersionAction): void {
  if (readingDraft.value) readingDraft.value.missing_version_action = value
}

function setLineHeight(value: LineHeight): void {
  if (readingDraft.value) readingDraft.value.line_height = value
}

function setPairView(value: boolean): void {
  if (readingDraft.value) readingDraft.value.pair_view = value
}

function setAnnotationsVisible(value: boolean): void {
  if (readingDraft.value) readingDraft.value.annotations_visible = value
}

function setAnchorHighlight(value: boolean): void {
  if (readingDraft.value) readingDraft.value.anchor_highlight = value
}

function setFontSize(event: Event): void {
  const draft = readingDraft.value
  if (!draft) return
  draft.font_size = Number((event.target as HTMLInputElement).value)
}

/* ---------------- 术语收藏（≤ 50 项） ---------------- */
const termsOpen = ref(false)
const termDraft = ref('')
const termsError = ref('')

function addTerm(): void {
  const draft = readingDraft.value
  const term = termDraft.value.trim()
  if (!draft || !term) return
  if (draft.favorite_terms.includes(term)) {
    termsError.value = '该术语已在收藏中'
    return
  }
  if (draft.favorite_terms.length >= 50) {
    termsError.value = '最多收藏 50 个术语'
    return
  }
  draft.favorite_terms = [...draft.favorite_terms, term]
  termDraft.value = ''
  termsError.value = ''
}

function removeTerm(term: string): void {
  const draft = readingDraft.value
  if (!draft) return
  draft.favorite_terms = draft.favorite_terms.filter((item) => item !== term)
  termsError.value = ''
}

/* ------------------------------------------------------------------ */
onMounted(async () => {
  await refresh()
  void loadReading()
})

onUnmounted(() => {
  if (savedTimer !== null) window.clearTimeout(savedTimer)
})
</script>

<style scoped>
/* 页面基准字号对齐展示稿（壳层 .sl-home 是 16px）；颜色一律取 --h-* 令牌 */
.sl-settings {
  display: block;
  font-size: var(--font-size-md);
}

/* ---------- 内容区顶部 tabbar ---------- */
.tabbar {
  display: flex;
  gap: 28px;
  border-bottom: 1px solid var(--h-line);
  margin-bottom: 24px;
}

.tabbar__btn {
  position: relative;
  padding: 10px 2px 12px;
  border: 0;
  background: transparent;
  color: var(--h-fg-subtle);
  font: inherit;
  font-size: var(--font-size-lg);
  cursor: pointer;
}

.tabbar__btn:hover {
  color: var(--h-fg);
}

.tabbar__btn--on {
  color: var(--h-fg);
  font-weight: 600;
}

.tabbar__btn--on::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  border-radius: 2px;
  background: var(--h-primary);
}

/* ---------- 页头 ---------- */
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.page-title {
  margin: 0;
  /* = 24px，对齐展示稿 */
  font-size: calc(var(--font-size-lg) * 1.5);
  font-weight: 600;
}

.head-actions {
  display: flex;
  gap: 10px;
  flex: none;
}

/* ---------- 卡片 ---------- */
.card {
  background: var(--h-surface-raised);
  border: 1px solid var(--h-line);
  border-radius: 12px;
  padding: 20px;
  margin-bottom: 16px;
}

.card--flush {
  padding: 0;
  overflow: hidden;
}

.card--flush .card__head {
  padding: 16px 20px;
  margin-bottom: 0;
}

.card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}

.card__title {
  font-size: var(--font-size-lg);
  font-weight: 600;
}

.card__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.card__foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  padding: 14px 20px;
  border-top: 1px solid var(--h-line);
}

.foot__label {
  color: var(--h-fg-subtle);
}

.foot__preview {
  min-width: 0;
  flex: 1;
  color: var(--h-fg-muted);
  font-size: var(--font-size-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---------- 按钮 ---------- */
.btn {
  height: 36px;
  padding: 0 16px;
  border: 1px solid var(--h-line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
  white-space: nowrap;
}

.btn:hover {
  background: var(--h-hover);
}

.btn--primary {
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
  font-weight: 500;
}

.btn--primary:hover {
  filter: brightness(1.06);
}

.btn--sm {
  height: 30px;
  padding: 0 12px;
  font-size: var(--font-size-sm);
  border-radius: 8px;
}

.btn--danger {
  border-color: var(--h-primary);
  color: var(--h-primary);
}

.btn--link {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--h-secondary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.btn--link:hover {
  text-decoration: underline;
}

.btn--link:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn:disabled:hover {
  background: transparent;
}

/* ---------- pill / tag ---------- */
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
  margin-right: 6px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--h-secondary) 14%, transparent);
  color: var(--h-secondary);
  white-space: nowrap;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

/* ---------- 状态行（错误提示 / 空状态，非说明性小字） ---------- */
.state {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 12px;
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.card--flush .state.card__foot,
.card .state.card__foot {
  margin: 0;
}

.state--error {
  color: var(--h-primary);
}

.empty {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* ---------- 输入控件 ---------- */
.input {
  height: 38px;
  padding: 0 12px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: var(--h-surface-input);
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  width: 100%;
}

.input:focus {
  outline: none;
  border-color: var(--h-line-strong);
}

.input--mono {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}

.input--sm {
  height: 32px;
  font-size: var(--font-size-sm);
  border-radius: 8px;
}

.input:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* ---------- 行布局 ---------- */
.rows {
  display: flex;
  flex-direction: column;
}

.row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 14px 0;
  border-top: 1px solid var(--h-line);
}

.row:first-child {
  border-top: 0;
}

.row__label {
  width: 132px;
  flex: none;
  color: var(--h-fg-muted);
}

.row__body {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}

.row__end {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
}

.grid2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

/* ---------- 分段控件 ---------- */
.seg {
  display: inline-flex;
  padding: 3px;
  gap: 2px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  border-radius: 10px;
}

.seg__btn {
  border: 0;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-sm);
  padding: 6px 14px;
  border-radius: 8px;
  cursor: pointer;
}

.seg__btn:hover {
  color: var(--h-fg);
}

.seg__btn--on {
  background: var(--h-surface-raised);
  color: var(--h-fg);
  font-weight: 500;
}

.seg__btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* ---------- 开关 ---------- */
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
  background: #ffffff; /* ui-polish-allow: 品牌色轨道上的白色滑块 */
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25); /* ui-polish-allow: 滑块投影色 */
  transition: transform 180ms cubic-bezier(0.4, 0, 0.2, 1);
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

/* ---------- 滑块 ---------- */
.range {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 4px;
  border-radius: 999px;
  background: var(--h-line);
  outline: none;
}

.range::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--h-primary);
  border: 2px solid var(--h-surface-raised);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.3); /* ui-polish-allow: 滑块投影色 */
  cursor: pointer;
}

.range:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

/* ---------- 进度条与指标 ---------- */
.bar {
  flex: 1;
  height: 8px;
  border-radius: 999px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  overflow: hidden;
}

.bar__fill {
  height: 100%;
  border-radius: 999px;
  background: var(--h-primary);
}

.bar__fill--quiet {
  background: var(--h-secondary);
}

.bar__fill--over {
  background: var(--h-primary);
}

.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin: 16px 0;
}

.metric {
  padding: 14px 16px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: var(--h-surface);
}

.metric__label {
  font-size: var(--font-size-xs);
  color: var(--h-fg-subtle);
}

.metric__value {
  margin-top: 2px;
  font-size: var(--font-size-xl);
  font-variant-numeric: tabular-nums;
}

/* ---------- 表格 ---------- */
.table {
  width: 100%;
  border-collapse: collapse;
}

.table th,
.table td {
  text-align: left;
  padding: 14px 20px;
  font-size: var(--font-size-sm);
  vertical-align: middle;
}

.table th {
  color: var(--h-fg-subtle);
  font-weight: 500;
  border-bottom: 1px solid var(--h-line);
}

.table td {
  border-bottom: 1px solid var(--h-line);
}

.table tr:last-child td {
  border-bottom: 0;
}

.table tbody tr:hover {
  background: var(--h-hover);
}

.table td.cell-narrow,
.table th.cell-narrow {
  width: 1%;
  white-space: nowrap;
}

.detail-cell {
  color: var(--h-fg-muted);
  overflow-wrap: anywhere;
}

/* ---------- 术语收藏 ---------- */
.terms {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 12px;
  padding: 12px;
  border: 1px solid var(--h-line);
  border-radius: 10px;
  background: var(--h-surface);
}

.terms__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.terms__del {
  border: 0;
  padding: 0;
  background: transparent;
  color: currentColor;
  font: inherit;
  font-size: var(--font-size-md);
  line-height: 1;
  cursor: pointer;
}

.terms__del:hover {
  color: var(--h-primary);
}

.terms__row {
  display: flex;
  gap: 8px;
}

/* ---------- 实时预览 ---------- */
.reader-preview {
  border: 1px solid var(--h-line);
  border-radius: 12px;
  background: var(--h-surface);
  padding: 22px 24px;
}

.reader-preview__title {
  font-weight: 500;
  margin-bottom: 12px;
}

.reader-preview__pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
}

.reader-preview__col h4 {
  margin: 0 0 8px;
  font-size: var(--font-size-xs);
  font-weight: 500;
  color: var(--h-fg-subtle);
}

.reader-preview p {
  margin: 0;
}

@media (max-width: 1000px) {
  .grid2,
  .metrics {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
