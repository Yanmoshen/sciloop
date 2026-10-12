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
  const code = String(error?.code ?? '').trim().toLowerCase()
  if (code === 'owner_token_required' || code === 'public_demo_readonly') return NEED_OWNER_HINT
  if (code === 'permission_denied' || code === 'approval_pending') return '这项操作需要你的确认后才能继续。'
  if (code === 'timeout') return '这次操作等待时间过长，已经停止，请稍后重试。'
  if (code === 'network_error' || code === 'not_connected') return '暂时无法连接服务，请检查连接后重试。'
  if (code === 'conversation_not_found') return '找不到这段对话，可能已经被移除，请刷新后重试。'
  if (
    code === 'internal_error' ||
    code === 'model_stream_error' ||
    code === 'tool_error' ||
    code === 'executor_error' ||
    code === 'io_error' ||
    code === 'protocol_error' ||
    code === 'fatal'
  ) {
    return '这次回答没有完整完成，请重新发送；已经完成的内容会保留。'
  }
  const message = (error?.message ?? '').trim()
  if (!message) return '这次操作没有成功，请稍后再试。'
  if (
    /reasoning_content|tool_calls|tool_call_id|request_id|invalid_request|approval_denied|execution_failed|unknown_tool|traceback|internal server error|服务内部错误|(?:\[(?:fatal|error|internal_error)\]\s*)?(?:read|write|connect|protocol|tool|executor)error\b|debug(?:\s+参数|_state)?/i.test(
      message,
    )
  ) {
    return '这次回答没有完整完成，请重新发送；已经完成的内容会保留。'
  }
  return message.replace(/\s*\(request_id:[^)]+\)/gi, '').trim() || '这次操作没有成功，请稍后再试。'
}

/** Store/layout 共享的安全错误文案入口，禁止把协议字段直接渲染到页面。 */
export function safeErrorMessage(error: unknown): string {
  if (error && typeof error === 'object') {
    const value = error as { code?: string; message?: string }
    return humanError(value)
  }
  return humanError({ message: String(error ?? '') })
}

/**
 * Remove internal tool/protocol diagnostics if a provider echoed them into a reply.
 *
 * Tool names and protocol field names are ordinary text when the assistant
 * explains its capabilities. Only unmistakable raw diagnostic lines are
 * removed. Execution status comes from structured events, never a guess made
 * from words in the answer.
 */
export function sanitizeAssistantText(text: string): string {
  const value = String(text ?? '')
  // Strip the stack itself, not the rest of the answer: a model can continue
  // with a useful explanation after quoting a diagnostic.
  const readable = value.replace(
    /^[ \t]*Traceback \(most recent call last\):[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*(?:\r?\n[A-Za-z_][\w.]*(?:Error|Exception)(?::[^\r\n]*)?)?(?:\r?\n|$)/gm,
    '',
  )
  const cleaned = readable.replace(
    /^(?:[ \t]*(?:Error:\s*)?(?:All connection attempts failed|Error executing tool\b|unreachable\s*[:：]))[^\r\n]*(?:\r?\n|$)/gim,
    '',
  )
  const withoutFatalReadError = cleaned.replace(
    /^[ \t]*(?:\[(?:fatal|error|internal_error)\]\s*)?(?:ReadError|WriteError|ConnectError|ProtocolError|ToolError|ExecutorError)\b[^\r\n]*(?:\r?\n|$)/gim,
    '',
  )
  // 旧版本在执行器不可用时把内部恢复指令当成助手正文写入历史；
  // 这不是面向用户的回答，刷新旧会话时也不能再次展示。
  const withoutRuntimeFallback = withoutFatalReadError.replace(
    /^[ \t]*这次文件操作还没有执行，因为本地执行环境暂时不可用。启动本地执行服务后，请重新发送这条请求。[ \t]*(?:\r?\n|$)/gm,
    '',
  )
  return withoutRuntimeFallback === value ? value : withoutRuntimeFallback.trimEnd()
}

/**
 * Merge a final SSE payload with text that has already been streamed.
 *
 * Providers differ on whether `done.content` is the whole answer, only the
 * last model round, or an error note. Treat it as an additional snapshot:
 * preserve the streamed answer, replace a partial snapshot with its complete
 * form, and avoid duplicating a suffix that was already delivered by deltas.
 */
export function mergeAssistantContent(current: string, incoming: string): string {
  const existing = sanitizeAssistantText(current)
  const next = sanitizeAssistantText(incoming)
  if (!next.trim()) return existing
  if (!existing.trim()) return next
  if (next === existing || existing.endsWith(next) || existing.includes(next)) return existing
  if (next.startsWith(existing)) return next
  // A corrected final snapshot can share an opening with the streamed answer
  // without containing it verbatim. Appending that snapshot repeats the whole
  // introduction; the final version is authoritative for the shared answer.
  let shared = 0
  while (shared < Math.min(existing.length, next.length) && existing[shared] === next[shared]) {
    shared += 1
  }
  if (shared >= 24 && next.length >= existing.length) return next
  return `${existing}${existing.endsWith('\n') ? '' : '\n\n'}${next}`
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
