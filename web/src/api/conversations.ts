/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 会话接口：首页对话的多轮记录（后端 JSON 按项目分目录落盘），刷新后据此恢复。
 *
 * 分组口径（2026-09-20 起）：会话归到某个项目下（`project_id`）或「未分组」（null），
 * 并带 `archived` 归档位。左栏「未分组 / 项目 / 已归档」三块都从这里取数。
 */

import { get, patch } from '@/api/client'

export interface ConversationTurn {
  role: 'user' | 'assistant'
  content: string
  model_id?: string
  duration_ms?: number
  /** 流式中断/出错时仍落盘的那一轮会带这个标记（如实标注，不假装完成） */
  interrupted?: boolean
}

export interface ConversationBrief {
  id: string
  title?: string | null
  model_ref?: string | null
  project_id?: number | null
  created_at?: string | null
  updated_at?: string | null
  archived: boolean
  turn_count: number
}

export interface ConversationDetail extends ConversationBrief {
  turns: ConversationTurn[]
}

/** `group`：`all` 不过滤 / `ungrouped` 未分组 / 数字字符串该项目 */
export type ConversationGroup = 'all' | 'ungrouped' | number

export function listConversations(options?: {
  group?: ConversationGroup
  archived?: boolean
  limit?: number
}): Promise<{ items: ConversationBrief[] }> {
  const group = options?.group ?? 'all'
  return get<{ items: ConversationBrief[] }>('/conversations', {
    query: {
      group: String(group),
      archived: options?.archived ?? false,
      limit: options?.limit,
    },
  })
}

export function getConversation(id: string): Promise<ConversationDetail> {
  return get<ConversationDetail>(`/conversations/${id}`)
}

/** 归档 / 取消归档（Owner） */
export function setConversationArchived(id: string, archived: boolean): Promise<ConversationDetail> {
  return patch<ConversationDetail>(`/conversations/${id}`, { body: { archived } })
}

/** 移入项目；`projectId = null` 即移回未分组（Owner） */
export function moveConversation(id: string, projectId: number | null): Promise<ConversationDetail> {
  return patch<ConversationDetail>(`/conversations/${id}`, { body: { project_id: projectId } })
}
