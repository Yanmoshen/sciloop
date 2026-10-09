<script setup lang="ts">
/**
 * 审批卡片：展示命令、参数、目录与风险，并提供五个决策动作。
 *
 * 五个动作与协议决策一一对应：
 * 批准一次 / 当前对话始终批准 / 完全访问 / 拒绝 / 取消。
 * 提交后按钮进入等待态，避免重复提交（同一结论重复提交服务端也只认一次）。
 */
import { computed, ref } from 'vue'

import type { ApprovalDecision, ApprovalView } from '../../agent-v2/protocol'

const props = defineProps<{
  approval: ApprovalView
  threadCwd?: string | null
  submitting?: boolean
}>()

const emit = defineEmits<{ (event: 'resolve', decision: ApprovalDecision): void }>()

const pending = computed(() => props.approval.status === 'pending')
const action = computed(() => props.approval.action ?? {})
const toolName = computed(() => String(action.value.tool ?? '未知工具'))
const argumentText = computed(() => JSON.stringify(action.value.arguments ?? {}, null, 2))
const directory = computed(() => {
  const args = (action.value.arguments ?? {}) as Record<string, unknown>
  const cwd = args.cwd ?? args.workdir ?? props.threadCwd
  return cwd ? String(cwd) : '—'
})
const risk = computed(() => props.approval.risk || String(action.value.risk ?? 'unknown'))
const busy = ref(false)

const OPTIONS: { decision: ApprovalDecision; label: string; hint: string; tone: string }[] = [
  { decision: 'approve_once', label: '批准一次', hint: '仅本次调用放行', tone: 'primary' },
  { decision: 'approve_conversation', label: '本对话始终批准', hint: '按规范化命令前缀在本会话内一直放行', tone: 'default' },
  { decision: 'full_access', label: '完全访问', hint: '本会话自动批准命令与文件/技能操作，仍记录审计', tone: 'warn' },
  { decision: 'deny', label: '拒绝', hint: '拒绝结果会回喂模型，由模型给出替代方案', tone: 'danger' },
  { decision: 'cancel', label: '取消', hint: '拒绝并中断当前回合', tone: 'danger' },
]

function choose(decision: ApprovalDecision): void {
  if (!pending.value || busy.value || props.submitting) return
  busy.value = true
  emit('resolve', decision)
}
</script>

<template>
  <article class="approval" :class="{ 'approval--decided': !pending }">
    <header class="approval__head">
      <span class="approval__title">需要研究者确认</span>
      <span class="tag" :class="pending ? 'tag--pending' : 'tag--done'">
        {{ pending ? '等待决定' : approval.status === 'granted' ? '已批准' : approval.status === 'denied' ? '已拒绝' : approval.status }}
      </span>
      <span v-if="approval.decision_scope" class="approval__scope" :title="`授权作用域：${approval.decision_scope}`">
        {{ approval.decision_scope }}
      </span>
    </header>

    <dl class="approval__facts">
      <div>
        <dt>工具</dt>
        <dd class="mono">{{ toolName }}</dd>
      </div>
      <div>
        <dt>目录</dt>
        <dd class="mono">{{ directory }}</dd>
      </div>
      <div>
        <dt>风险</dt>
        <dd class="mono" :title="risk">{{ risk }}</dd>
      </div>
    </dl>

    <section class="approval__args">
      <p class="label">参数</p>
      <pre class="mono">{{ argumentText }}</pre>
    </section>

    <div v-if="pending" class="approval__actions">
      <button
        v-for="option in OPTIONS"
        :key="option.decision"
        type="button"
        class="btn"
        :class="`btn--${option.tone}`"
        :title="option.hint"
        :disabled="busy || submitting"
        @click="choose(option.decision)"
      >
        {{ option.label }}
      </button>
    </div>
    <p v-else class="approval__decided">
      {{ approval.decided_by ? `由 ${approval.decided_by} 决定` : '已决定' }}
      <span v-if="approval.decided_at">· {{ approval.decided_at }}</span>
    </p>
  </article>
</template>

<style scoped>
.approval {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: var(--color-bg-elevated);
  box-shadow: var(--shadow-card);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.approval--decided {
  border-color: var(--color-border);
  box-shadow: none;
}

.approval__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.approval__title {
  font-size: var(--font-size-md);
  color: var(--color-text-primary);
}

.approval__scope {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
  margin-left: auto;
}

.tag {
  font-size: var(--font-size-2xs);
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
}

.tag--pending {
  color: var(--color-warning);
  background: var(--color-warning-soft);
}

.tag--done {
  color: var(--color-success);
  background: var(--color-success-soft);
}

.approval__facts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
  margin: 0;
}

.approval__facts dt {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.approval__facts dd {
  margin: 2px 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  overflow-wrap: anywhere;
}

.label {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.mono {
  font-family: var(--font-family-mono);
}

pre.mono {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 200px;
  overflow: auto;
}

.approval__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.btn {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  padding: var(--space-1) var(--space-3);
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.btn:hover:not(:disabled) {
  background: var(--color-bg-subtle);
}

.btn:active:not(:disabled) {
  background: var(--color-bg-muted);
}

.btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}

.btn:disabled {
  color: var(--color-text-disabled);
  cursor: progress;
}

.btn--primary {
  background: var(--color-brand);
  border-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.btn--primary:hover:not(:disabled) {
  background: var(--color-brand-hover);
}

.btn--warn {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.btn--danger {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.btn--danger:hover:not(:disabled) {
  background: var(--color-danger-soft);
}

.approval__decided {
  margin: 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
</style>
