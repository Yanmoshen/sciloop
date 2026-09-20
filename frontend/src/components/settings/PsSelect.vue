<!--
  Copyright 2026 SciLoop contributors
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

  设置页下拉（替代原生 `<select>`）。

  为什么自绘：原生 select 的弹层由浏览器 / 系统绘制，CSS 完全够不到，深色壳层里会掉出
  一块系统色；这里只引壳层令牌 `--h-*`（不引 `tokens.css` 的学术蓝、不造 hex）。

  交互：Enter / Space 打开（走原生 click，避免双触发），Esc 关闭，↑/↓ 移动，Enter 选中，
  点组件外部收起（用 document 级 pointerdown 判断包含关系，不用 fixed 遮罩层）。
  禁用时不可展开，除降不透明度外同时改文字色与光标，不只靠颜色区分。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'

export interface PsOption {
  value: string
  label: string
}

const props = withDefaults(
  defineProps<{
    modelValue: string
    options: ReadonlyArray<PsOption>
    /** 禁用：不可展开（只读面） */
    disabled?: boolean
    /** 触发器无可读文本时的无障碍名称 */
    ariaLabel?: string
    /** 撑满父容器（表格单元格用）；默认按内容宽度 */
    block?: boolean
  }>(),
  { disabled: false, ariaLabel: '', block: false },
)

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'change', value: string): void
}>()

const listId = `ps-select-${useId()}`
const optionId = (index: number): string => `${listId}-option-${index}`

const open = ref(false)
const activeIndex = ref(-1)
const root = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)

const selectedIndex = computed(() => props.options.findIndex((item) => item.value === props.modelValue))
const currentLabel = computed(() =>
  selectedIndex.value >= 0 ? props.options[selectedIndex.value].label : '',
)

function openPanel(): void {
  if (props.disabled) return
  activeIndex.value = selectedIndex.value >= 0 ? selectedIndex.value : 0
  open.value = true
}

function toggle(): void {
  if (open.value) open.value = false
  else openPanel()
}

/** 与原生 select 一致：选到同一个值不派发 change */
function commit(index: number): void {
  const option = props.options[index]
  open.value = false
  if (!option || option.value === props.modelValue) return
  emit('update:modelValue', option.value)
  emit('change', option.value)
}

function move(delta: number): void {
  if (!open.value) {
    openPanel()
    return
  }
  const total = props.options.length
  if (!total) return
  activeIndex.value = (activeIndex.value + delta + total) % total
}

function onKeydown(event: KeyboardEvent): void {
  switch (event.key) {
    case 'Escape':
      if (open.value) {
        event.preventDefault()
        open.value = false
      }
      return
    case 'ArrowDown':
      event.preventDefault()
      move(1)
      return
    case 'ArrowUp':
      event.preventDefault()
      move(-1)
      return
    case 'Enter':
      // 关闭态交给原生 click（Enter 在 button 上会派发 click），避免双触发
      if (open.value) {
        event.preventDefault()
        commit(activeIndex.value)
      }
      return
    case 'Tab':
      open.value = false
      return
    default:
      return
  }
}

function onPointerDown(event: PointerEvent): void {
  if (!open.value) return
  if (root.value && !root.value.contains(event.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('pointerdown', onPointerDown, true))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown, true))

/** 选中值被外部改写（如 PATCH 失败回滚）时收起，避免弹层停在旧选项上 */
watch(
  () => props.modelValue,
  () => {
    open.value = false
  },
)

/** 键盘移动到可视区外时把高亮项带进视野 */
watch([open, activeIndex], async ([isOpen, index]) => {
  if (!isOpen || index < 0) return
  await nextTick()
  panel.value?.querySelector<HTMLElement>(`[data-index="${index}"]`)?.scrollIntoView({ block: 'nearest' })
})
</script>

<template>
  <div ref="root" class="ps-select" :class="{ 'ps-select--block': block }">
    <button
      class="ps-select__trigger"
      type="button"
      aria-haspopup="listbox"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-controls="open ? listId : undefined"
      :aria-label="ariaLabel || undefined"
      :aria-activedescendant="open && activeIndex >= 0 ? optionId(activeIndex) : undefined"
      :disabled="disabled"
      @click="toggle"
      @keydown="onKeydown"
    >
      <span class="ps-select__value">{{ currentLabel }}</span>
      <svg
        class="ps-select__caret"
        :class="{ 'ps-select__caret--open': open }"
        viewBox="0 0 10 6"
        aria-hidden="true"
      >
        <path
          d="M1 1.2 5 5l4-3.8"
          fill="none"
          stroke="currentColor"
          stroke-width="1.4"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </button>

    <ul v-if="open" :id="listId" ref="panel" class="ps-select__panel" role="listbox">
      <li
        v-for="(item, index) in options"
        :id="optionId(index)"
        :key="item.value"
        :data-index="index"
        class="ps-select__option"
        :class="{
          'ps-select__option--active': index === activeIndex,
          'ps-select__option--selected': item.value === modelValue,
        }"
        role="option"
        :aria-selected="item.value === modelValue ? 'true' : 'false'"
        @mouseenter="activeIndex = index"
        @click="commit(index)"
      >
        <span class="ps-select__check" aria-hidden="true">
          <svg v-if="item.value === modelValue" viewBox="0 0 12 12" width="12" height="12">
            <path
              d="M2 6.4 4.6 9 10 3.2"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </span>
        <span class="ps-select__option-label" :title="item.label">{{ item.label }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
/* 颜色一律 --h-*；尺寸与相邻控件对齐（32px 高 / 8px 圆角 / --font-size-sm） */
.ps-select {
  position: relative;
  display: inline-flex;
  min-width: 0;
  max-width: 100%;
}

.ps-select--block {
  display: flex;
  width: 100%;
}

.ps-select__trigger {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  height: 32px;
  padding: 0 10px;
  border: 1px solid var(--h-line);
  border-radius: 8px;
  background: var(--h-surface-input);
  color: var(--h-fg);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
}

.ps-select__trigger:hover:not(:disabled),
.ps-select__trigger[aria-expanded='true'] {
  border-color: var(--h-line-strong);
}

.ps-select__trigger:focus-visible {
  outline: none;
  border-color: var(--h-line-strong);
}

.ps-select__trigger:disabled {
  /* 不只靠颜色：同时降不透明度 + 换光标 */
  color: var(--h-fg-subtle);
  opacity: 0.55;
  cursor: not-allowed;
}

.ps-select__value {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ps-select__caret {
  width: 10px;
  height: 6px;
  flex: none;
  color: var(--h-fg-subtle);
  transition: transform 160ms ease;
}

.ps-select__caret--open {
  transform: rotate(180deg);
}

.ps-select__panel {
  position: absolute;
  z-index: var(--z-popover);
  top: calc(100% + 4px);
  left: 0;
  width: 100%;
  max-height: 240px;
  margin: 0;
  padding: 4px;
  overflow-y: auto;
  list-style: none;
  background: var(--h-surface-raised);
  border: 1px solid var(--h-line-strong);
  border-radius: 10px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.32); /* ui-polish-allow: 弹层投影色 */
}

.ps-select__option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  color: var(--h-fg);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.ps-select__option--active {
  background: var(--h-hover);
}

.ps-select__option--selected {
  background: var(--h-active);
  color: var(--h-primary);
  font-weight: 500;
}

.ps-select__check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
  flex: none;
  color: var(--h-primary);
}

.ps-select__option-label {
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
