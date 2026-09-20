<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * ViewStatePanel —— 视图状态提示条（P1-1「每页补齐六类状态」的统一落点）。
 *
 * 六类状态的可见表达分工（各视图统一口径，避免每页各写一套）：
 * | 状态 | 可见表达 |
 * |---|---|
 * | `loading` | 本组件顶部细条 + 视图内的 `el-skeleton` 骨架 |
 * | `empty` | 视图内的 `el-empty`（文案必须说明「未获取 ≠ 0」） |
 * | `error` | 本组件 error 分支：`code` + 原文 message + 「重试」按钮 |
 * | `permission denied` | 本组件 permission 分支：说明当前访问面 + 指向设置页补 Owner 令牌 |
 * | `retry` | 本组件在 error / permission 分支统一渲染「重试」按钮 |
 *
 * 颜色一律引用 `styles/tokens.css` 变量，组件内不出现硬编码色值。
 */
import { RouterLink } from 'vue-router'

withDefaults(
  defineProps<{
    /** 进行中：即使已有旧数据，也要让用户看到「正在刷新」 */
    loading?: boolean
    loadingText?: string
    /** 失败态：来自 `ApiError.message` / `.code` */
    error?: string | null
    errorCode?: string | null
    errorTitle?: string
    /** 权限拒绝态：`public_demo` 匿名只读面发起了写操作 */
    permissionDenied?: boolean
    permissionNote?: string | null
    /**
     * 权限区块标题。默认强调「已前置禁用」而非「操作被拒绝」——大多数情况下用户
     * 并没有真的发起过写请求（按钮已 disabled），把「被拒绝」当作默认标题会误报；
     * 只有确认收到 401/403 时才应传入「该操作已被拒绝」口径的标题。
     */
    permissionTitle?: string
    /** 中性补充说明（不改变状态判定） */
    note?: string | null
    /** 是否提供重试入口 */
    retryable?: boolean
    retryLabel?: string
    /** 重试按钮 loading */
    busy?: boolean
    /** 权限提示中是否展示「前往设置页配置 Owner 令牌」链接 */
    showOwnerHint?: boolean
  }>(),
  {
    loading: false,
    loadingText: '正在加载…',
    error: null,
    errorCode: null,
    errorTitle: '加载失败',
    permissionDenied: false,
    permissionNote: null,
    permissionTitle: '只读演示面：本页写操作已前置禁用',
    note: null,
    retryable: false,
    retryLabel: '重试',
    busy: false,
    showOwnerHint: true,
  },
)

defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="states">
    <p v-if="loading" class="states__loading" role="status" aria-live="polite">
      <span class="states__spinner" aria-hidden="true" />
      {{ loadingText }}
    </p>

    <el-alert
      v-if="permissionDenied"
      class="states__alert"
      type="warning"
      :closable="false"
      show-icon
      data-state="permission-denied"
    >
      <template #title>{{ permissionTitle }}</template>
      <template #default>
        <p class="states__body">
          {{ permissionNote ?? '当前为浏览模式：写操作需要先在「设置」里启用编辑。' }}
        </p>
        <p v-if="showOwnerHint" class="states__body">
          恢复方式：在
          <RouterLink to="/settings">设置页</RouterLink>
          在「设置」里填写即可写入（凭据只存本机，服务端仍会逐条校验并留审计记录）。
        </p>
        <el-button
          v-if="retryable"
          class="states__action"
          size="small"
          text
          type="primary"
          :loading="busy"
          @click="$emit('retry')"
        >
          {{ retryLabel }}
        </el-button>
      </template>
    </el-alert>

    <el-alert
      v-if="error"
      class="states__alert"
      type="error"
      :closable="false"
      show-icon
      data-state="error"
    >
      <template #title>{{ errorTitle }}（{{ errorCode ?? 'unknown_error' }}）：{{ error }}</template>
      <template #default>
        <el-button
          v-if="retryable"
          class="states__action"
          size="small"
          text
          type="primary"
          :loading="busy"
          @click="$emit('retry')"
        >
          {{ retryLabel }}
        </el-button>
      </template>
    </el-alert>

  </div>
</template>

<style scoped>
.states {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.states__loading {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.states__spinner {
  width: 10px;
  height: 10px;
  border: 2px solid var(--color-border-strong);
  border-top-color: var(--color-brand);
  border-radius: var(--radius-pill);
  animation: states-spin 0.8s linear infinite;
}

@keyframes states-spin {
  to {
    transform: rotate(360deg);
  }
}

.states__alert {
  margin: 0;
}

.states__body {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  line-height: var(--line-height-base);
}

.states__action {
  padding-left: 0;
}

</style>
