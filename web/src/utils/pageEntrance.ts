/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 「页面入场」信号（跨组件一次性令牌）。
 *
 * 为什么需要它：入场动画只在**从别的页面切回首页**时播。而"上一次在哪个路由"这个信息
 * 只有壳层（HomeLayout，它跨路由常驻）看得到，页面组件自己（HomeView，会重新挂载）
 * 看不到 —— 单靠 `onMounted` 无法区分"从文献调研切回来"和"在首页点 ＋ 新建对话"，
 * 后者按需求**不该播**。
 *
 * 口径：壳层在路由切换时打标（仅当 目标=首页类 且 来源≠首页类），页面挂载后消费一次。
 * 首次加载（直接落在 /）不打标 ⇒ 不播 —— 那是"已经在首页"。
 */

import { ref } from 'vue'

const pending = ref(false)

/** 壳层调用：请求下一个挂载的首页类页面播一次入场 */
export function requestEntrance(): void {
  pending.value = true
}

/** 页面挂载时调用：拿到就返回 true 并清空（保证只播一次，不重复触发） */
export function consumeEntrance(): boolean {
  if (!pending.value) return false
  pending.value = false
  return true
}

/** 丢弃未消费的信号（例如用户在动画播完前又切走了） */
export function clearEntrance(): void {
  pending.value = false
}
