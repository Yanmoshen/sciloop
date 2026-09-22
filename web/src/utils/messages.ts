/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 面向用户的「人话」文案层（2026-09-20 评审第 5 条）。
 *
 * 原则：界面上不出现实现细节 —— `OWNER_TOKEN` / `public_demo` / `403 owner_token_required` /
 * `SSE` / `rank_score` / `task_id` 这些词只该出现在 tooltip、展开区或接口文档里。
 * 用户真正要的是两件事：**我现在能不能做** + **不能的话我要做什么**。
 *
 * 为什么要收在一处：此前同一件事（只读面被拒）在 20+ 个页面各写一遍，
 * 措辞从「public_demo 只读面会被后端拒绝（403 owner_token_required）」到
 * 「只读面：需要 OWNER_TOKEN」全不一样 —— 阅读负担重，而且必然漂移。
 */

/** 只读面（浏览模式）统一说法：说清"发生了什么 + 你能做什么" */
export function writeDenied(action: string): string {
  return `当前是只读浏览模式，${action}需要先到「设置」里切换成研究者身份。`
}

/** 只读面短标签（徽标 / 按钮 title 用） */
export const READONLY_LABEL = '只读浏览'
export const WRITABLE_LABEL = '研究者身份'

/** 需要研究者身份时的那句人话（**不出现任何内部标识**） */
export const NEED_OWNER_HINT =
  '当前是只读浏览模式，这个操作需要研究者身份：到「设置」里切换后重试。'

/**
 * 把一次失败翻译成人话。
 *
 * ⚠️ 为什么不再拼 `code：message`：那会把 `owner_token_required` / `public_demo`
 * 这类内部标识直接摆到研究者眼前（实测被模拟测试抓到过）。
 * 这里按**错误码**给一句人话；其余情况用后端给的说明，但绝不附内部码。
 */
export function humanError(error: { code?: string; message?: string } | null | undefined): string {
  const code = error?.code ?? ''
  if (code === 'owner_token_required' || code === 'public_demo_readonly') return NEED_OWNER_HINT
  const message = (error?.message ?? '').trim()
  return message || '这次操作没有成功，请稍后再试。'
}

/** 实时连接状态的人话（SSE → 实时进度） */
export function liveStatus(state: string): string {
  switch (state) {
    case 'open':
      return '实时进度已连接'
    case 'connecting':
      return '正在连接实时进度'
    case 'reconnecting':
      return '实时进度暂时断开，正在重新连接'
    case 'error':
    case 'closed':
      return '实时进度已断开，可手动刷新'
    default:
      return '实时进度状态未知，可手动刷新'
  }
}

/**
 * 推荐分的"是什么"（不是"为什么"）——四维真名，别编造维度。
 * 明细（每维得分与权重）在卡片展开区 / 影响力明细图里。
 */
export const RANK_DIMENSION_TEXT = '检索相关性 · 时效性 · 引用趋势 · 证据完整度'
export const RANK_SCORE_LABEL = '推荐排序分（四项加权）'
export const INFLUENCE_SCORE_LABEL = '影响力分（辅助，不参与默认排序）'
