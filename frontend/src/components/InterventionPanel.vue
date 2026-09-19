<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 介入面板（WP15-T3）。
 *
 * - N1–N4 记录列表与操作入口（approve / modify / reject / rerun / downgrade / abort / switch_mode）
 * - 显式展示 N ↔ D 映射（contracts.intervention_mapping）
 * - 服务端返回的 mapping 文案与本地契约映射并列展示，冲突时以服务端为准并标注
 *
 * 写操作需 Owner；public_demo 面按钮禁用并说明原因（后端仍会独立拒绝）。
 */
import { computed, ref } from 'vue'

import {
  INTERVENTION_NODES,
  type InterventionAction,
  type InterventionNode,
} from '@/api/workbench'
import { useSessionStore } from '@/stores/session'
import { useWorkbenchStore } from '@/stores/workbench'

const store = useWorkbenchStore()
const session = useSessionStore()

const ACTION_LABELS: Record<InterventionAction, string> = {
  approve: '批准',
  modify: '修改',
  reject: '拒绝',
  rerun: '重跑',
  downgrade: '降级',
  abort: '中止',
  switch_mode: '切换模式',
}

const nodeNote = ref('')
const nodeAction = ref<Record<InterventionNode, InterventionAction>>({
  N1: 'approve',
  N2: 'approve',
  N3: 'approve',
  N4: 'approve',
})

const DECISION_NAMES: Record<string, string> = {
  D1: '检索策略',
  D2: '方案选型',
  D3: '实验配置',
  D4: '失败处置',
  D5: '迭代判据',
  D6: '写作结构',
}

const canWrite = computed(() => session.isOwner || store.ownerWritesAllowed)
const writeBlockReason = computed(() =>
  canWrite.value ? '' : 'public_demo 面禁止写操作（需在设置页填入 Owner 令牌）',
)

const nodes = computed(() =>
  INTERVENTION_NODES.map((node) => ({
    ...node,
    serverText: store.interventions.mapping?.[node.id] ?? '',
    records: store.interventions.items.filter((item) => item.node === node.id),
    decisionLabel: node.decision_point
      ? `${node.decision_point}（${DECISION_NAMES[node.decision_point] ?? ''}）`
      : '前置门禁（无决策点）',
  })),
)

/** 当前应介入的节点（由 status.pending_decision.stage 推得） */
const activeNode = computed<InterventionNode | null>(() => {
  const stage = store.status?.pending_decision?.stage ?? store.status?.current_stage?.stage
  if (!stage) return null
  if (stage === 'experiment') return 'N3'
  if (stage === 'plan_review') return 'N2'
  if (stage === 'writing') return 'N4'
  return null
})

function submit(node: InterventionNode): void {
  void store.interveneNode(node, nodeAction.value[node], {}, { note: nodeNote.value, autoResume: true })
}

function switchMode(mode: 'auto' | 'manual'): void {
  void store.switchMode(mode)
}

function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString() : '—'
}
</script>

<template>
  <div class="panel">
    <!-- N ↔ D 映射表 -->
    <section class="mapping">
      <header class="mapping-head">
        <strong>N ↔ D 映射（contracts.intervention_mapping）</strong>
        <span class="mapping-hint">当前应介入节点：{{ activeNode ?? '（无待人工环节）' }}</span>
      </header>
      <table class="map-table">
        <thead>
          <tr>
            <th>节点</th>
            <th>触发时机</th>
            <th>决策点</th>
            <th>可用动作</th>
            <th>记录数</th>
            <th>服务端口径</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="node in nodes" :key="node.id" :class="{ 'row--active': activeNode === node.id }">
            <td class="mono">{{ node.id }}</td>
            <td>{{ node.trigger }}</td>
            <td>{{ node.decisionLabel }}</td>
            <td class="mono">{{ node.actions.join(' / ') }}</td>
            <td>{{ node.records.length }}</td>
            <td class="server">{{ node.serverText || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 操作入口 -->
    <section class="entry">
      <header class="entry-head">
        <strong>人工介入操作</strong>
      </header>

      <div class="entry-form">
        <el-input v-model="nodeNote" size="small" placeholder="备注（写入 interventions.note）" class="note" />
        <div v-for="node in nodes" :key="node.id" class="node-row">
          <span class="node-id mono">{{ node.id }}</span>
          <el-select v-model="nodeAction[node.id]" size="small" class="action-select">
            <el-option
              v-for="action in node.actions"
              :key="action"
              :label="`${action}（${ACTION_LABELS[action]}）`"
              :value="action"
            />
          </el-select>
          <el-button size="small" :disabled="!canWrite" :loading="store.busy === `intervene-${node.id}`" @click="submit(node.id)">
            提交
          </el-button>
        </div>

        <div class="node-row">
          <span class="node-id mono">mode</span>
          <el-button size="small" :disabled="!canWrite || store.busy === 'switch-mode'" @click="switchMode(store.status?.mode === 'auto' ? 'manual' : 'auto')">
            切换为 {{ store.status?.mode === 'auto' ? 'manual' : 'auto' }}（switch_mode）
          </el-button>
          <span class="mapping-hint">当前模式 {{ store.status?.mode ?? '—' }}</span>
        </div>
      </div>

      <p v-if="!canWrite" class="block">{{ writeBlockReason }}</p>
    </section>

    <!-- 记录列表 -->
    <section class="records">
      <header class="records-head">
        <strong>介入记录</strong>
        <span class="mapping-hint">共 {{ store.interventions.total }} 条</span>
      </header>

      <el-table v-if="store.interventions.items.length" :data="store.interventions.items" size="small" border>
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="node" label="节点" width="70" />
        <el-table-column label="动作" width="110">
          <template #default="{ row }">
            {{ ACTION_LABELS[row.action as InterventionAction] ?? row.action }}（{{ row.action }}）
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="180">
          <template #default="{ row }">{{ row.note ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="载荷" min-width="200">
          <template #default="{ row }">
            <code v-if="row.payload" class="payload" :title="JSON.stringify(row.payload)">
              {{ JSON.stringify(row.payload) }}
            </code>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无介入记录" />
    </section>
  </div>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.mapping,
.entry,
.records {
  padding: var(--space-3);
  border: 1px solid var(--color-card-border);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
}

.mapping-head,
.entry-head,
.records-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
  font-size: var(--font-size-sm);
}

.mapping-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.map-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.map-table th,
.map-table td {
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  vertical-align: top;
}

.map-table th {
  color: var(--color-text-secondary);
  font-weight: 400;
}

.row--active {
  background-color: var(--color-brand-soft);
}

.server {
  color: var(--color-text-secondary);
}

.entry-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.note {
  max-width: 420px;
}

.node-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}

.node-id {
  width: 46px;
  color: var(--color-brand);
}

.action-select {
  width: 240px;
}

.block {
  margin: var(--space-2) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-warning);
}

.hint {
  margin: var(--space-2) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.mono {
  font-family: var(--font-family-mono);
}

/* 载荷是原始 JSON，按列宽截断显示，完整值走 title 悬浮提示，避免撑爆表格 */
.payload {
  display: inline-block;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  color: var(--color-text-secondary);
}
</style>
