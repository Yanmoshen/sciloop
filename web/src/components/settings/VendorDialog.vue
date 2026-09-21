<!--
  Copyright 2026 SciLoop contributors
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  「添加服务商」对话框 —— 照 Cherry Studio `settings.provider.add.*` 的形态：
  只收**建立连接所需的最小信息**（提供商名称 / 提供商类型 / API 地址 / API 密钥），
  模型不在这一步登记（Cherry 也是进详情页后用「获取模型列表」或 `+` 添加）。

  为什么不用 `el-dialog`：本页整体渲染在总览壳层 `.sl-home` 内，Element Plus 走的是
  `tokens.css` 的学术蓝，混用正是「风格不一致」的来源；配色只引 `--h-*`。

  写操作是真实的：`POST /models/configs`（Owner）。api_key 只上送不回显；
  失败时原样带出服务端 `code` / `message`，不伪装成功。
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { ApiError, createModelConfig } from '@/api/models'
import PsSelect from '@/components/settings/PsSelect.vue'
import { useSettingsStore } from '@/stores/settings'

const props = defineProps<{
  modelValue: boolean
  /** 写操作是否可用（沿用「本机有 OWNER_TOKEN」语义） */
  canWrite: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'saved'): void
}>()

const store = useSettingsStore()

const TYPE_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: 'openai', label: 'OpenAI 兼容' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google', label: 'Google' },
]

const name = ref('')
const baseUrl = ref('')
const type = ref('openai')
const apiKey = ref('')
const busy = ref(false)
const errorNotice = ref('')

const canSubmit = computed(
  () => !!name.value.trim() && !!baseUrl.value.trim() && !!apiKey.value.trim() && !busy.value,
)

function close(): void {
  emit('update:modelValue', false)
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    name.value = ''
    baseUrl.value = ''
    type.value = 'openai'
    apiKey.value = ''
    busy.value = false
    errorNotice.value = ''
  },
)

function failure(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}：${err.message}`
  return err instanceof Error ? err.message : String(err)
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  busy.value = true
  errorNotice.value = ''
  try {
    await createModelConfig({
      name: name.value.trim(),
      base_url: baseUrl.value.trim().replace(/\/$/, ''),
      api_key: apiKey.value.trim(),
      models: [],
      type: type.value || null,
    })
    await store.loadConfigs()
    emit('saved')
    close()
  } catch (err) {
    errorNotice.value = failure(err)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="modelValue" class="overlay" @click.self="close">
    <div class="dialog sl-home" role="dialog" aria-modal="true" aria-label="添加提供商">
      <header class="dialog__head">
        <h2 class="dialog__title">添加提供商</h2>
        <button class="btn--link" type="button" @click="close">关闭</button>
      </header>

      <div class="fields">
        <label class="field">
          <span class="field__label">提供商名称</span>
          <input v-model="name" class="input" type="text" placeholder="例如 OpenAI" />
        </label>
        <label class="field">
          <span class="field__label">提供商类型</span>
          <PsSelect v-model="type" :options="TYPE_OPTIONS" block aria-label="提供商类型" />
        </label>
        <label class="field field--wide">
          <span class="field__label">API 地址</span>
          <input
            v-model="baseUrl"
            class="input input--mono"
            type="text"
            placeholder="https://api.deepseek.com"
          />
        </label>
        <label class="field field--wide">
          <span class="field__label">API 密钥</span>
          <input
            v-model="apiKey"
            class="input input--mono"
            type="password"
            autocomplete="off"
            placeholder="明文 Key，或 env:LLM_DEFAULT_API_KEY"
          />
        </label>
      </div>

      <p v-if="errorNotice" class="state state--error">{{ errorNotice }}</p>

      <footer class="dialog__foot">
        <button class="btn" type="button" @click="close">取消</button>
        <button class="btn btn--primary" type="button" :disabled="!canWrite || !canSubmit" @click="submit">
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
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
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 24px;
  background: var(--h-surface-raised);
  color: var(--h-fg);
  border: 1px solid var(--h-line-strong);
  border-radius: 20px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35); /* ui-polish-allow: 弹窗投影色 */
  font-size: var(--font-size-md);
}

.dialog__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.dialog__title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.field--wide {
  grid-column: 1 / -1;
}

.field__label {
  font-size: var(--font-size-sm);
  color: var(--h-fg-subtle);
}

.input {
  width: 100%;
  height: 36px;
  padding: 0 12px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  border-radius: 10px;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  outline: none;
  transition: border-color var(--motion-dur) var(--motion-ease);
}

.input:focus {
  border-color: var(--h-line-strong);
}

.input--mono {
  font-family: 'SFMono-Regular', Consolas, Menlo, monospace;
}

.state {
  margin: 0;
  font-size: var(--font-size-sm);
  color: var(--h-fg-muted);
}

.state--error {
  color: var(--h-primary);
}

.dialog__foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn {
  height: 34px;
  padding: 0 16px;
  border: 1px solid var(--h-line-strong);
  border-radius: 10px;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
  white-space: nowrap;
  transition: background-color var(--motion-dur) var(--motion-ease);
}

.btn:hover:not(:disabled) {
  background: var(--h-hover);
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

.btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn--link {
  border: 0;
  background: transparent;
  color: var(--h-fg-subtle);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
}

.btn--link:hover {
  color: var(--h-fg);
}
</style>
