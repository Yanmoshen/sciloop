<script setup lang="ts">
/**
 * 工具调用卡片：参数、增量输出、真实错误与耗时。
 *
 * 关键口径：失败就显示失败原因，**绝不显示伪成功**（验收书 §4）。
 */
import { computed } from 'vue'
import { humanError } from '../../utils/messages'
import { actionDetails } from './presentation'

import type { ToolCallView } from '../../agent-v2/protocol'

const props = defineProps<{
  call: ToolCallView
  output: string
  running?: boolean
  stopped?: boolean
}>()

const STATUS_LABEL: Record<string, string> = {
  requested: '已请求',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  timeout: '超时',
  cancelled: '已取消',
  invalid_arguments: '参数非法',
}

const label = computed(() => {
  if (props.stopped && ['requested', 'running'].includes(props.call.status)) return '已停止'
  return STATUS_LABEL[props.call.status] ?? '处理中'
})
const tone = computed(() => {
  if (props.stopped && ['requested', 'running'].includes(props.call.status)) return 'warn'
  if (props.call.status === 'succeeded') return 'ok'
  if (props.call.status === 'failed' || props.call.status === 'timeout') return 'danger'
  if (props.call.status === 'cancelled' || props.call.status === 'invalid_arguments') return 'warn'
  return 'busy'
})
const kindLabel = computed(() => (props.call.kind === 'read_only' ? '只读' : '有副作用'))
const TOOL_LABELS: Record<string, string> = {
  read_file: '读取本地文件',
  list_dir: '查看目录',
  write_file: '写入本地文件',
  run_command: '运行本地命令',
  update_plan: '更新任务计划',
  'agent.spawn': '创建子任务',
  'agent.send': '向子任务发送消息',
  'agent.wait': '等待子任务',
  'agent.interrupt': '中断子任务',
  'agent.close': '结束子任务',
  'host.command': '运行本地命令',
  'host.file.read': '读取本地文件',
  'host.file.write': '写入本地文件',
  'search.query': '搜索资料',
  'search.fetch': '读取网页资料',
  'knowledge.search': '搜索知识库',
  'knowledge.write': '写入知识库',
  'skill.list': '查看可用技能',
  'skill.run': '运行技能',
  'mcp.call': '调用外部服务',
}
const displayName = computed(() => TOOL_LABELS[props.call.name] ?? '执行操作')
/** 把内部错误码翻成用户能看懂的一句话（界面不出现 code）。 */
const FAILURE_HINT: Record<string, string> = {
  approval_denied: '研究者拒绝了这次操作',
  execution_failed: '执行没有成功',
  timeout: '执行超时',
  cancelled: '操作已取消',
  invalid_arguments: '参数不合法',
  unknown_tool: '这个工具当前不可用',
  failed: '执行失败',
}
const failureText = computed(() => {
  const error = props.call.error
  if (!error) return ''
  return FAILURE_HINT[error.code] ?? humanError(error)
})

const duration = computed(() => {
  const ms = props.call.duration_ms
  if (ms === null || ms === undefined) return ''
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`
})
const argumentText = computed(() => actionDetails(props.call.arguments ?? {}))
const outputText = computed(() => {
  if (props.output) return props.output
  if (!props.call.output) return ''
  return JSON.stringify(props.call.output, null, 2)
})
</script>

<template>
  <article class="tool" :class="`tool--${tone}`" :aria-label="`${displayName}：${label}`">
    <header class="tool__head">
      <span class="tool__name">{{ displayName }}</span>
      <span class="tag" :class="`tag--${tone}`">{{ label }}</span>
      <span class="tool__kind" :title="kindLabel === '只读' ? '只读工具可以并行执行' : '有副作用的工具必须串行执行'">
        {{ kindLabel }}
      </span>
      <span v-if="duration" class="tool__duration">
        {{ duration }}
      </span>
    </header>

    <details class="tool__section">
      <summary>操作内容</summary>
      <pre class="mono">{{ argumentText }}</pre>
    </details>

    <section v-if="outputText" class="tool__section">
      <p class="tool__label">输出</p>
      <pre class="mono">{{ outputText }}</pre>
      <p v-if="call.truncated" class="tool__truncated">输出已截断</p>
    </section>

    <section v-if="call.error" class="tool__error">
      <p class="tool__label">失败原因</p>
      <pre class="mono">{{ failureText }}</pre>
    </section>
  </article>
</template>

<style scoped>
.tool {
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background: var(--color-bg-elevated);
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.tool--ok {
  border-left-color: var(--color-success);
}

.tool--danger {
  border-left-color: var(--color-danger);
}

.tool--warn {
  border-left-color: var(--color-warning);
}

.tool--busy {
  border-left-color: var(--color-brand);
}

.tool__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.tool__name {
  font-family: var(--font-family-mono);
  font-size: var(--font-size-sm);
  color: var(--color-text-primary);
}

.tool__kind,
.tool__duration {
  font-size: var(--font-size-2xs);
  color: var(--color-text-secondary);
}

.tool__duration {
  margin-left: auto;
}

.tag {
  font-size: var(--font-size-2xs);
  padding: 1px var(--space-2);
  border-radius: var(--radius-sm);
}

.tag--ok {
  color: var(--color-success);
  background: var(--color-success-soft);
}

.tag--danger {
  color: var(--color-danger);
  background: var(--color-danger-soft);
}

.tag--warn {
  color: var(--color-warning);
  background: var(--color-warning-soft);
}

.tag--busy {
  color: var(--color-brand);
  background: var(--color-brand-soft);
}

.tool__section > summary {
  cursor: pointer;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.tool__label {
  margin: 0 0 var(--space-1);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.mono {
  margin: 0;
  padding: var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-bg-muted);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-primary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 260px;
  overflow: auto;
}

.tool__error pre {
  background: var(--color-danger-soft);
  color: var(--color-danger);
}

.tool__truncated {
  margin: var(--space-1) 0 0;
  font-size: var(--font-size-2xs);
  color: var(--color-warning);
}
</style>
