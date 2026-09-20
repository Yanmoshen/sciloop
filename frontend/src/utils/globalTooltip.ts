/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全局 tooltip 服务：接管所有 `title` 属性的原生提示。
 *
 * 为什么要接管：浏览器原生 title 提示是白底黑字的系统样式，在深色界面上非常突兀，
 * 而且不可控（延迟、位置、字体都由浏览器决定）。这里做一次性全局替换：
 *
 * 1. 冒泡阶段捕获 `mouseover`（document 级委托，动态渲染的元素同样生效）；
 * 2. 首次悬停时把 `title` **摘掉**并转存到 `data-sl-tip`（原生提示随之消失），
 *    若元素没有 `aria-label` 则顺手补上，保证无 title 也不丢无障碍信息；
 * 3. 用单例浮层 `#sl-tooltip` 显示提示：跟随元素定位、跟随主题配色、淡入 150ms。
 *
 * 交互口径（2026-09-20 改：**提示必须能被鼠标够到并选中文字**）
 * ----------------------------------------------------------
 * 旧实现是"指针事件穿透 + 一离开目标立即收起"，实测两个毛病：
 * ① 指针在 OFFSET 空隙里走一半提示就没了；② 根本无法把鼠标移到提示上，更别说选中复制。
 * 现在：
 * - 浮层 `pointer-events: auto`（见 styles/tooltip.css），**可悬停、可选中文字**；
 * - 收起延时**很短**（HIDE_DELAY_MS，观感即"移开就收"）；
 * - 可达性不靠"拖长宽限期"，而靠**几何走廊**：指针在「目标 ∪ 浮层 ∪ 两者之间」这个
 *   外扩 HOVER_BRIDGE_PX 的连通区域内就不收起 → 能走到浮层上，但一走开立刻消失；
 * - 指针在浮层内**选中文字**时强制不收（用户可能正要复制）。
 *
 * 覆盖范围：任意层（总览壳层、模块壳层、弹窗、抽屉）——因为是 document 级委托。
 */

const TIP_ID = 'sl-tooltip'
const SHOW_DELAY_MS = 120
/** 收起延时：**要短**（指针一移开就消失）；可达性交给下面的几何走廊 */
const HIDE_DELAY_MS = 130
/** 走廊外扩像素：目标与浮层各外扩这么多，覆盖 OFFSET 空隙与边缘抖动 */
const HOVER_BRIDGE_PX = 8
const OFFSET = 10
const VIEWPORT_PADDING = 8

let tipEl: HTMLElement | null = null
let showTimer: number | null = null
let hideTimer: number | null = null
let activeTarget: HTMLElement | null = null
let installed = false

/** 取消待执行的收起（指针又回到目标或浮层上时调用） */
function cancelHide(): void {
  if (hideTimer !== null) {
    window.clearTimeout(hideTimer)
    hideTimer = null
  }
}

/**
 * 延时收起（HIDE_DELAY_MS 很短，所以观感上就是"移开即收"）。
 *
 * 同时**取消尚未弹出的显示定时器**：否则"快速划过"（<120ms）时会出现
 * 「指针已经走了、提示才弹出来」的幽灵窗口。
 */
function scheduleHide(): void {
  cancelHide()
  if (showTimer !== null) {
    window.clearTimeout(showTimer)
    showTimer = null
  }
  hideTimer = window.setTimeout(() => {
    hideTimer = null
    hideTip()
  }, HIDE_DELAY_MS)
}

function insideTip(node: EventTarget | null): boolean {
  return Boolean(tipEl && node instanceof Node && (node === tipEl || tipEl.contains(node)))
}

function pointIn(x: number, y: number, rect: DOMRect, pad: number): boolean {
  return (
    x >= rect.left - pad && x <= rect.right + pad && y >= rect.top - pad && y <= rect.bottom + pad
  )
}

/**
 * 指针是否还在「能走到浮层」的连通区域内：**目标 ∪ 浮层 ∪ 两者之间的走廊**（各外扩 HOVER_BRIDGE_PX）。
 *
 * 为什么用几何而不是"长宽限期"：宽限期只能靠拖时间兼顾可达性，代价是**移开后提示赖着不消失**；
 * 几何判定把两件事解耦 —— 走在走廊里就不收（够得着），一旦拐弯走开就立刻收（不拖沓）。
 */
function pointerInHoverRegion(event: MouseEvent): boolean {
  if (!tipEl || tipEl.hidden || !activeTarget) return false
  if (insideTip(event.target)) return true
  if (!activeTarget.isConnected) return false
  const targetRect = activeTarget.getBoundingClientRect()
  const tipRect = tipEl.getBoundingClientRect()
  const left = Math.min(targetRect.left, tipRect.left)
  const top = Math.min(targetRect.top, tipRect.top)
  const corridor = new DOMRect(
    left,
    top,
    Math.max(targetRect.right, tipRect.right) - left,
    Math.max(targetRect.bottom, tipRect.bottom) - top,
  )
  return pointIn(event.clientX, event.clientY, corridor, HOVER_BRIDGE_PX)
}

/** 浮层里正有选中内容（且在浮层内）→ 绝不能收起，否则用户选到一半就没了 */
function selectingInsideTip(): boolean {
  if (!tipEl || tipEl.hidden) return false
  const selection = document.getSelection()
  if (!selection || selection.isCollapsed) return false
  const anchor = selection.anchorNode
  return Boolean(anchor && tipEl.contains(anchor))
}

function ensureTip(): HTMLElement {
  if (tipEl && tipEl.isConnected) return tipEl
  const el = document.createElement('div')
  el.id = TIP_ID
  el.className = 'sl-tooltip'
  el.setAttribute('role', 'tooltip')
  el.hidden = true
  // 浮层本身也是"可悬停区域"的一部分：鼠标移上来不收，移出去才延时收
  el.addEventListener('mouseenter', cancelHide)
  el.addEventListener('mouseleave', () => {
    if (!selectingInsideTip()) scheduleHide()
  })
  document.body.appendChild(el)
  tipEl = el
  return el
}

function hideTip(): void {
  if (showTimer !== null) {
    window.clearTimeout(showTimer)
    showTimer = null
  }
  cancelHide()
  activeTarget = null
  if (tipEl) {
    tipEl.hidden = true
    tipEl.classList.remove('sl-tooltip--on')
  }
}

/** 优先放在元素下方；空间不足则翻到上方；水平方向做视口内收边 */
function place(target: HTMLElement, el: HTMLElement): void {
  const rect = target.getBoundingClientRect()
  const size = el.getBoundingClientRect()

  let top = rect.bottom + OFFSET
  if (top + size.height > window.innerHeight - VIEWPORT_PADDING) {
    top = Math.max(VIEWPORT_PADDING, rect.top - size.height - OFFSET)
  }

  let left = rect.left
  if (left + size.width > window.innerWidth - VIEWPORT_PADDING) {
    left = window.innerWidth - size.width - VIEWPORT_PADDING
  }
  left = Math.max(VIEWPORT_PADDING, left)

  el.style.top = `${Math.round(top)}px`
  el.style.left = `${Math.round(left)}px`
}

function showTip(target: HTMLElement): void {
  const text = target.getAttribute('data-sl-tip') ?? ''
  if (!text.trim()) return
  const el = ensureTip()
  el.textContent = text
  el.hidden = false
  // 先测量再定位，避免首帧跳动
  el.style.top = '0px'
  el.style.left = '0px'
  el.classList.add('sl-tooltip--on')
  place(target, el)
}

/**
 * 记录悬停目标并延时显示。
 *
 * `activeTarget` **必须在排程时立即写入**（而不是等 showTip 时才写）：
 * 否则在 120ms 延迟期内移开鼠标时，mouseout 比对不到目标 → 不收起 →
 * 定时器到点又把提示弹出来，且此后无人负责收起（用户实测到的"移开很远也不消失"）。
 */
function scheduleShow(target: HTMLElement): void {
  hideTip()
  activeTarget = target
  showTimer = window.setTimeout(() => {
    showTimer = null
    // 延迟期内目标已被移除（例如列表刷新）就不再显示
    if (!target.isConnected) {
      activeTarget = null
      return
    }
    showTip(target)
  }, SHOW_DELAY_MS)
}

/** 悬停即把 title 转存，避免原生提示先弹出来；同时补齐无障碍标签 */
function adoptTitle(el: HTMLElement): void {
  const raw = el.getAttribute('title')
  if (raw === null) return
  el.setAttribute('data-sl-tip', raw)
  el.removeAttribute('title')
  if (!el.hasAttribute('aria-label')) el.setAttribute('aria-label', raw)
}

export function installGlobalTooltip(): void {
  if (installed) return
  installed = true

  document.addEventListener(
    'mouseover',
    (event) => {
      const node = event.target as HTMLElement | null
      if (!node || typeof node.closest !== 'function') return
      const target = node.closest<HTMLElement>('[title], [data-sl-tip]')
      if (!target) return
      adoptTitle(target)
      if (activeTarget === target) {
        // 指针从浮层折回原目标 → 撤销待执行的收起
        cancelHide()
        return
      }
      scheduleShow(target)
    },
    true,
  )

  document.addEventListener(
    'mouseout',
    (event) => {
      const node = event.target as HTMLElement | null
      if (!node || typeof node.closest !== 'function') return
      const target = node.closest<HTMLElement>('[data-sl-tip]')
      if (!target || target !== activeTarget) return
      const related = event.relatedTarget as HTMLElement | null
      // 仍在可走到浮层的区域内（目标内部 / 正在跨越走廊 / 已进浮层）→ 先不收，交给 mousemove 逐步判断
      if (pointerInHoverRegion(event)) {
        cancelHide()
        return
      }
      if (related && typeof related.closest === 'function' && related.closest('[data-sl-tip]') === target) {
        cancelHide()
        return
      }
      scheduleHide()
    },
    true,
  )

  /**
   * 兜底守卫：指针彻底离开「目标 ∪ 浮层 ∪ 走廊」后才延时收起。
   * 覆盖 mouseout 可能漏掉的场景：指针移出窗口、目标被重新渲染/移除、
   * 或者通过滚动/快捷键导致布局变化。
   */
  document.addEventListener(
    'mousemove',
    (event) => {
      if (!activeTarget) return
      if (!activeTarget.isConnected) {
        hideTip()
        return
      }
      if (pointerInHoverRegion(event)) {
        cancelHide()
        return
      }
      if (selectingInsideTip()) return
      scheduleHide()
    },
    true,
  )

  // 按下鼠标：**点在浮层里不算"离开"**（否则一点就消失，无法选中/复制）
  document.addEventListener(
    'mousedown',
    (event) => {
      if (!activeTarget) return
      if (insideTip(event.target)) return
      hideTip()
    },
    true,
  )
  // 滚动时立即收起，避免提示与目标错位（正在浮层里选字时除外）
  document.addEventListener(
    'scroll',
    () => {
      if (!selectingInsideTip()) hideTip()
    },
    true,
  )
  window.addEventListener('resize', hideTip)
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideTip()
  })
}
