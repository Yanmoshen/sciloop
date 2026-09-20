/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 首页对话：一次请求拿到「项目标题」+「正式回答」。
 * 写操作（会产生真实费用）→ 需要 OWNER_TOKEN，令牌由 `api/client.ts` 统一注入请求头。
 */

import { post } from '@/api/client'

export interface HomeChatInput {
  text: string
  model_config_id: number
  model_id: string
}

export interface HomeChatResult {
  title: string
  /** `model` = 模型给出；`fallback` = 标题调用失败后按输入截断 */
  title_source: string
  reply: string
  model_ref: string
  provider: string
  model_id: string
  cost_usd: number | null
  cost_unknown_reason: string | null
  usage: {
    prompt_tokens: number | null
    completion_tokens: number | null
    total_tokens: number | null
  }
}

export function chatHome(input: HomeChatInput): Promise<HomeChatResult> {
  return post<HomeChatResult>('/chat/home', { body: input })
}
