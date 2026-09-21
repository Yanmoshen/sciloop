<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 平滑下拉（替代原生 `<select>`）。
 *
 * 为什么自绘：原生 select 的弹出层由浏览器/系统绘制，CSS 与 transition **完全够不到**，
 * 想给"展开/收起"加平滑动效就只能自绘。视觉沿用页面既有口径（32px 高、`--radius-md`
 * 圆角、`--color-border-strong` 边框、`--font-size-sm` 字号），只额外提供动效与键盘操作。
 *
 * 动效：面板 `opacity + translateY(-6px) + scale(.98)` → 归位，160ms
 * `cubic-bezier(0.16, 1, 0.3, 1)`（先快后缓，收尾不"撞墙"）；箭头同步旋转。
 * `prefers-reduced-motion` 下时长压到 1ms（不取消交互，只去动效）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

export interface SmoothOption {
  value: string
  label: string
}

const props = defineProps<{
  modelValue: string
  options: SmoothOption[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  change: []
}>()

const open = ref(false)
const active = ref(0)
const root = ref<HTMLElement | null>(null)

const currentLabel = computed(
  () =>
    props.options.find((item) => item.value === props.modelValue)?.label ??
    props.options[0]?.label ??
    '',
)

function openPanel(): void {
  active.value = Math.max(
    0,
    props.options.findIndex((item) => item.value === props.modelValue),
  )
  open.value = true
}

function toggle(): void {
  if (open.value) open.value = false
  else openPanel()
}

function pick(value: string): void {
  open.value = false
  if (value === props.modelValue) return
  emit('update:modelValue', value)
  emit('change')
}

function step(delta: number): void {
  if (!open.value) {
    openPanel()
    return
  }
  const total = props.options.length
  if (total === 0) return
  active.value = (active.value + delta + total) % total
}

/** Enter / Space 交给原生 button 的 click 处理，这里只管方向键与 Esc，避免双触发 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    open.value = false
    return
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    step(1)
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    step(-1)
    return
  }
  if (event.key === 'Enter' && open.value) {
    event.preventDefault()
    const option = props.options[active.value]
    if (option) pick(option.value)
  }
}

function onPointerDown(event: PointerEvent): void {
  if (!open.value) return
  if (root.value && !root.value.contains(event.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('pointerdown', onPointerDown, true))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onPointerDown, true))
watch(
  () => props.modelValue,
  () => {
    open.value = false
  },
)
</script>

<template>
  <div ref="root" class="sselect">
    <button
      class="sselect__trigger"
      type="button"
      aria-haspopup="listbox"
      :aria-expanded="open"
      @click="toggle"
      @keydown="onKeydown"
    >
      <span class="sselect__label">{{ currentLabel }}</span>
      <svg class="sselect__caret" :class="{ 'is-open': open }" viewBox="0 0 10 6" aria-hidden="true">
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

    <Transition name="sselect-pop">
      <ul v-if="open" class="sselect__panel scroll-y" role="listbox">
        <li
          v-for="(item, index) in options"
          :key="item.value"
          class="sselect__option"
          :class="{ 'is-active': index === active, 'is-selected': item.value === modelValue }"
          role="option"
          :aria-selected="item.value === modelValue"
          @mouseenter="active = index"
          @click="pick(item.value)"
        >
          {{ item.label }}
        </li>
      </ul>
    </Transition>
  </div>
</template>

<style scoped>
.sselect {
  position: relative;
}

.sselect__trigger {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  height: 32px;
  padding: 0 var(--space-3);
  background: var(--color-bg-page);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.sselect__trigger:hover {
  border-color: var(--color-brand);
}

.sselect__trigger[aria-expanded='true'] {
  border-color: var(--color-brand);
}

.sselect__label {
  white-space: nowrap;
}

.sselect__caret {
  width: 10px;
  height: 6px;
  flex: none;
  color: var(--color-text-secondary);
  transition: transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.sselect__caret.is-open {
  transform: rotate(180deg);
}

.sselect__panel {
  position: absolute;
  z-index: var(--z-popover);
  top: calc(100% + 4px);
  left: 0;
  min-width: 100%;
  max-height: 260px;
  margin: 0;
  padding: 4px;
  overflow-y: auto;
  list-style: none;
  background: var(--color-bg-elevated);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-popover);
}

.sselect__option {
  padding: 6px var(--space-3);
  border-radius: var(--radius-sm);
  color: var(--color-text-primary);
  font-size: var(--font-size-sm);
  white-space: nowrap;
  cursor: pointer;
}

.sselect__option.is-active {
  background: var(--color-bg-muted);
}

.sselect__option.is-selected {
  color: var(--color-brand);
  font-weight: 500;
}

/* 展开/收起：先快后缓，160ms；收起时同曲线上抛，不打折 */
.sselect-pop-enter-active,
.sselect-pop-leave-active {
  transition:
    opacity 160ms cubic-bezier(0.16, 1, 0.3, 1),
    transform 160ms cubic-bezier(0.16, 1, 0.3, 1);
}

.sselect-pop-enter-from,
.sselect-pop-leave-to {
  opacity: 0;
  transform: translateY(-6px) scale(0.98);
}

@media (prefers-reduced-motion: reduce) {
  .sselect__caret,
  .sselect-pop-enter-active,
  .sselect-pop-leave-active {
    transition-duration: 1ms;
  }
}
</style>
