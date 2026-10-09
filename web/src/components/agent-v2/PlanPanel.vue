<script setup lang="ts">
/**
 * 计划面板：只展示模型给出的计划及其更新，**不提供简单/复杂模式开关**。
 *
 * 计划是事件流里的一个 Item；这里只读渲染，不阻塞执行。
 */
import type { PlanView } from '../../agent-v2/protocol'

defineProps<{ plan: PlanView }>()

const STEP_LABEL: Record<string, string> = {
  pending: '未开始',
  in_progress: '进行中',
  completed: '已完成',
  blocked: '受阻',
}
</script>

<template>
  <section class="plan">
    <header class="plan__head">
      <span class="plan__title">{{ plan.title }}</span>
      <span class="plan__meta" :title="`计划来自事件 ${plan.item_id}`">
        {{ plan.steps.length }} 步 · 更新于 {{ plan.created_at }}
      </span>
    </header>
    <ol class="plan__steps">
      <li v-for="step in plan.steps" :key="step.index" class="step" :class="`step--${step.status}`">
        <span class="step__index">{{ step.index }}</span>
        <span class="step__title">{{ step.title }}</span>
        <span class="step__status">{{ STEP_LABEL[step.status] ?? step.status }}</span>
      </li>
    </ol>
    <p v-if="plan.note" class="plan__note">{{ plan.note }}</p>
  </section>
</template>

<style scoped>
.plan {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-subtle);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.plan__head {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.plan__title {
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.plan__meta {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
  margin-left: auto;
}

.plan__steps {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.step {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
}

.step__index {
  width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-pill);
  background: var(--color-bg-muted);
  color: var(--color-text-secondary);
  font-size: var(--font-size-2xs);
}

.step__title {
  flex: 1;
  min-width: 0;
}

.step__status {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.step--completed .step__status {
  color: var(--color-success);
}

.step--in_progress .step__status {
  color: var(--color-brand);
}

.step--blocked .step__status {
  color: var(--color-danger);
}

.plan__note {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
</style>
