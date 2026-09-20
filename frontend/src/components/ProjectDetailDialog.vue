<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 项目详情（**居中弹框 + 背景模糊**，总览壳层专用样式，用 `.sl-home` 的 --h-* 令牌）。
 *
 * 数据源：`GET /projects/{id}`（公开只读）。缺失字段显示「未记录/未关联」，
 * 不编造、不填 0。模块壳层里另有浅色学术蓝版本，两者互不影响。
 */
import { ref, watch } from 'vue'

import { fetchProjectDetail, type ProjectDetail } from '@/api/projects'

const props = defineProps<{
  modelValue: boolean
  projectId: number | null
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const detail = ref<ProjectDetail | null>(null)
const loading = ref(false)
const error = ref('')

watch(
  () => [props.modelValue, props.projectId] as const,
  ([open, id]) => {
    if (!open || id === null) return
    if (detail.value?.id === id) return
    void load(id)
  },
  { immediate: true },
)

async function load(id: number): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    detail.value = await fetchProjectDetail(id)
  } catch (err) {
    detail.value = null
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function fmt(value: string | null | undefined): string {
  if (!value) return '未记录'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN')
}

function close(): void {
  emit('update:modelValue', false)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="overlay sl-home" @click.self="close">
      <div class="dialog scroll-y" role="dialog" aria-modal="true" aria-label="项目详情">
        <header class="dialog__head">
          <h2 class="dialog__title">项目详情</h2>
          <button class="close" type="button" aria-label="关闭" @click="close">关闭</button>
        </header>

        <p v-if="loading" class="muted">加载中…</p>
        <p v-else-if="error" class="error">获取失败：{{ error }}</p>
        <dl v-else-if="detail" class="list">
          <div><dt>项目名</dt><dd>{{ detail.name }}</dd></div>
          <div><dt>项目 ID</dt><dd>#{{ detail.id }}</dd></div>
          <div><dt>状态</dt><dd>{{ detail.status ?? '未记录' }}</dd></div>
          <div><dt>模式</dt><dd>{{ detail.mode ?? '未记录' }}</dd></div>
          <div><dt>迭代轮次</dt><dd>{{ detail.current_iteration }}</dd></div>
          <div><dt>示例项目</dt><dd>{{ detail.is_demo ? '是' : '否' }}</dd></div>
          <div><dt>关联 idea</dt><dd>{{ detail.idea_id ?? '未关联' }}</dd></div>
          <div><dt>关联任务书</dt><dd>{{ detail.taskbook_id ?? '未关联' }}</dd></div>
          <div><dt>创建时间</dt><dd>{{ fmt(detail.created_at) }}</dd></div>
          <div><dt>最近更新</dt><dd>{{ fmt(detail.updated_at) }}</dd></div>
          <div>
            <dt>运行次数</dt>
            <dd>{{ detail.run_count ?? detail.iteration_history?.length ?? 0 }}</dd>
          </div>
          <div>
            <dt>六环节顺序</dt>
            <dd>{{ (detail.stage_order ?? []).join(' → ') || '未记录' }}</dd>
          </div>
        </dl>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 居中弹框：四周半透明遮罩 + 背景模糊（不抢焦点但保留上下文） */
.overlay {
  position: fixed;
  inset: 0;
  z-index: 2200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.35); /* ui-polish-allow: 遮罩色与主题解耦 */
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .overlay {
  background: rgba(0, 0, 0, 0.55); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.dialog {
  width: min(560px, 100%);
  max-height: 84vh;
  overflow-y: auto;
  padding: 24px;
  background: var(--h-surface-raised);
  color: var(--h-fg);
  border: 1px solid var(--h-line-strong);
  border-radius: 20px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35); /* ui-polish-allow: 弹窗投影色 */
  font-size: var(--font-size-md);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.dialog__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--h-line);
}

.dialog__title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.close {
  border: 0;
  background: transparent;
  color: var(--h-fg-subtle);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
}

.close:hover {
  color: var(--h-fg);
}

.muted,
.error {
  margin: 0;
  color: var(--h-fg-subtle);
  font-size: var(--font-size-sm);
}

.error {
  color: var(--h-primary);
}

.list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.list > div {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--h-line);
}

.list dt {
  flex: none;
  font-size: var(--font-size-xs);
  color: var(--h-fg-subtle);
}

.list dd {
  margin: 0;
  text-align: right;
  word-break: break-all;
}
</style>
