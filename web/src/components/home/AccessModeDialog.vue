<script setup lang="ts">
/**
 * 「完全访问模式」说明弹窗（研究者 2026-09-25 定稿）。
 *
 * 为什么要有它：这个开关原来是「鼠标悬停看一眼说明 + 点一下就开」，等于**开着的时候再告知**；
 * 现在改成点击先弹窗、**点「开启完全访问模式」才真的打开** —— 知情同意在前。
 * 关掉不弹（收紧权限不需要再确认一次）。
 *
 * 文案两句话都按后端 `server/services/agent/policy.py` 的真实口径写，不是宣传语：
 * 开着完全访问时普通动手操作放行，而**四类高危（删除或覆盖文件 / 改系统与权限 /
 * 数据库破坏性操作 / 下载即执行）仍然要点批准卡**。
 *
 * 视觉规格与 `components/home/ConfirmDialog.vue` 保持一致（遮罩 0.55 + 8px 毛玻璃、
 * 卡片 min(420px)、圆角 20、品牌色主按钮），免得站里两种弹窗长得不一样。
 */
withDefaults(defineProps<{ modelValue: boolean; busy?: boolean; error?: string }>(), {
  busy: false,
  error: '',
})

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'confirm'): void
}>()

function close(): void {
  emit('update:modelValue', false)
}
</script>

<template>
  <Teleport to="body">
    <div v-if="modelValue" class="overlay sl-home" @click.self="close">
      <div class="dialog" role="dialog" aria-modal="true" aria-label="完全访问模式">
        <h2 class="dialog__title">完全访问模式</h2>
        <p class="dialog__message">在本对话内，普通命令无需批准，高危命令仍需批准。</p>
        <p class="dialog__note">
          <b>免责声明</b>
          模型可能出错，开启后它会直接改动你的文件和本机环境，请自行核验。
        </p>
        <p v-if="error" class="dialog__error">{{ error }}</p>
        <div class="foot">
          <button class="btn" type="button" :disabled="busy" @click="close">取消</button>
          <button class="btn btn--primary" type="button" :disabled="busy" @click="emit('confirm')">
            {{ busy ? '正在开启…' : '开启完全访问模式' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
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
  width: min(420px, 100%);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  background: var(--h-surface-raised);
  color: var(--h-fg);
  border: 1px solid var(--h-line-strong);
  border-radius: 20px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.35); /* ui-polish-allow: 弹窗投影色 */
  font-size: var(--font-size-md);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.dialog__title {
  margin: 0;
  font-size: var(--font-size-xl);
  font-weight: 600;
}

.dialog__message {
  margin: 0;
  color: var(--h-fg-muted);
  line-height: 1.6;
}

/* 免责声明：与正文同一份内容，但**降一档、加一条细线** —— 它是"请你注意"，不是条目 */
.dialog__note {
  margin: 0;
  padding-top: 12px;
  border-top: 1px solid var(--h-line);
  color: var(--h-fg-subtle);
  font-size: var(--font-size-sm);
  line-height: 1.6;
}

.dialog__note b {
  display: block;
  font-weight: 500;
  margin-bottom: 2px;
}

.dialog__error {
  margin: 0;
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}

.foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.btn {
  height: 34px;
  padding: 0 16px;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  cursor: pointer;
}

.btn:hover {
  border-color: var(--h-line-strong);
  color: var(--h-fg);
}

.btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.btn--primary {
  background: var(--h-primary);
  border-color: var(--h-primary);
  color: var(--h-primary-fg);
}

.btn--primary:hover {
  background: var(--h-primary-hover, var(--h-primary));
  color: var(--h-primary-fg);
}
</style>
