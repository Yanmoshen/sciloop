/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 会话接口：首页对话的多轮记录（后端 JSON 落盘），刷新后据此恢复。
 */

import { get } from '@/api/client'

export interface ConversationTurn {
  role: 'user' | 'assistant'
  content: string
  model_id?: string
  duration_ms?: number
}

export interface ConversationBrief {
  id: string
  title?: string | null
  model_ref?: string | null
  project_id?: number | null
  created_at?: string | null
  updated_at?: string | null
  turn_count: number
}

export interface ConversationDetail extends ConversationBrief {
  turns: ConversationTurn[]
}

export function listConversations(limit = 20): Promise<{ items: ConversationBrief[] }> {
  return get<{ items: ConversationBrief[] }>('/conversations', { query: { limit } })
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return get<ConversationDetail>(`/conversations/${id}`)
}

/** 最近一次会话（没有则 null）——用于刷新后接着上次继续 */
export async function latestConversation(): Promise<ConversationDetail | null> {
  const list = await listConversations(1)
  const first = list.items?.[0]
  if (!first) return null
  return getConversation(first.id)
}
