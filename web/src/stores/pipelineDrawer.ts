/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 研究流程抽屉的开关与宽度（跨组件共享：顶栏入口按钮在 HomeLayout，抽屉本体在 HomeView）。
 *
 * 宽度记忆在 localStorage；范围 200–600（拖拽时夹取）。
 * `dismissed` 用来区分「用户主动收起」与「初始收起」：主动收起后不再自动弹出。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

const WIDTH_KEY = 'sciloop.pipeline.drawer.width'
const WIDTH_MIN = 200
const WIDTH_MAX = 600
const WIDTH_DEFAULT = 300

function clampWidth(value: number): number {
  if (!Number.isFinite(value)) return WIDTH_DEFAULT
  return Math.min(Math.max(Math.round(value), WIDTH_MIN), WIDTH_MAX)
}

function readStoredWidth(): number {
  if (typeof localStorage === 'undefined') return WIDTH_DEFAULT
  return clampWidth(Number(localStorage.getItem(WIDTH_KEY)))
}

export const usePipelineDrawerStore = defineStore('pipelineDrawer', () => {
  const open = ref(false)
  const width = ref(readStoredWidth())
  /** 用户是否主动收起过（主动收起后，后续流程启动不再自动弹出） */
  const dismissed = ref(false)

  function toggle(): void {
    open.value = !open.value
    if (!open.value) dismissed.value = true
  }

  function close(): void {
    open.value = false
    dismissed.value = true
  }

  /** 流程启动时自动滑出（用户手动收起过就不再打扰） */
  function autoShow(): void {
    if (!dismissed.value) open.value = true
  }

  function setWidth(next: number): void {
    width.value = clampWidth(next)
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(WIDTH_KEY, String(width.value))
    }
  }

  return { open, width, dismissed, toggle, close, autoShow, setWidth, WIDTH_MIN, WIDTH_MAX }
})
