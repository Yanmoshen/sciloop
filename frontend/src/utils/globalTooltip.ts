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
 * 3. 用单例浮层 `#sl-tooltip` 显示提示：跟随元素定位、跟随主题配色、
 *    淡入 150ms、指针事件穿透（不挡点击）、滚动/按下/Esc 即隐藏。
 *
 * 覆盖范围：任意层（总览壳层、模块壳层、弹窗、抽屉）——因为是 document 级委托。
 */

const TIP_ID = 'sl-tooltip'
const SHOW_DELAY_MS = 120
const OFFSET = 10
const VIEWPORT_PADDING = 8

let tipEl: HTMLElement | null = null
let showTimer: number | null = null
let activeTarget: HTMLElement | null = null
let installed = false

function ensureTip(): HTMLElement {
  if (tipEl && tipEl.isConnected) return tipEl
  const el = document.createElement('div')
  el.id = TIP_ID
  el.className = 'sl-tooltip'
  el.setAttribute('role', 'tooltip')
  el.hidden = true
  document.body.appendChild(el)
  tipEl = el
  return el
}

function hideTip(): void {
  if (showTimer !== null) {
    window.clearTimeout(showTimer)
    showTimer = null
  }
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
      if (activeTarget === target) return
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
      // 在同一元素内部移动不隐藏（避免闪烁）
      if (related && typeof related.closest === 'function' && related.closest('[data-sl-tip]') === target) {
        return
      }
      hideTip()
    },
    true,
  )

  /**
   * 兜底守卫：指针只要没停在当前目标（或其子元素）上，就立即收起。
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
      const node = event.target as HTMLElement | null
      if (!node) return
      if (node === activeTarget) return
      if (typeof node.closest === 'function' && node.closest('[data-sl-tip]') === activeTarget) return
      hideTip()
    },
    true,
  )

  // 交互与滚动时立即收起，避免提示漂移或遮挡
  document.addEventListener('mousedown', hideTip, true)
  document.addEventListener('scroll', hideTip, true)
  window.addEventListener('resize', hideTip)
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideTip()
  })
}
