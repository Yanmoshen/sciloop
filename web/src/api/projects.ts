/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 项目接口封装：列表走 store（session.projects），创建走这里。
 * 视图层不直接 fetch（沿用 api/client.ts 的统一请求与错误体解析）。
 */

import { get, patch, post, type ApiError } from './client'

export interface CreateProjectInput {
  name: string
  /** 备注（原「一句话研究问题」，落库在 settings.note） */
  note: string
  fields: string[]
  mode?: 'manual' | 'auto'
}

/** `GET /projects/{id}` 的响应（含迭代历史；缺值一律 null，不编造） */
export interface ProjectDetail {
  id: number
  name: string
  status: string | null
  mode: string | null
  current_iteration: number
  is_demo: boolean
  idea_id: number | null
  taskbook_id: number | null
  created_at: string | null
  updated_at: string | null
  settings?: Record<string, unknown> | null
  stage_order?: string[]
  run_count?: number
  latest_run?: Record<string, unknown> | null
  iteration_history?: Array<Record<string, unknown>>
}

/** 项目详情（公开只读） */
export async function fetchProjectDetail(id: number): Promise<ProjectDetail> {
  return get<ProjectDetail>(`/projects/${id}`)
}

/** 重命名项目：`PATCH /projects/{id}`（Owner 写操作，只改 name） */
export async function renameProject(id: number, name: string): Promise<ProjectDetail> {
  return patch<ProjectDetail>(`/projects/${id}`, { body: { name: name.trim() } })
}

/**
 * 归档 / 取消归档项目：`PATCH /projects/{id}`（Owner 写操作，只改 archived）。
 *
 * 归档 = 从左栏「项目」收起、进「已归档」折叠区；**不动流水线状态、不删数据**，
 * 项目下的对话一并收起，但点开仍可继续聊。
 */
export async function setProjectArchived(id: number, archived: boolean): Promise<ProjectDetail> {
  return patch<ProjectDetail>(`/projects/${id}`, { body: { archived } })
}

export interface CreatedProject {
  id: number
  name: string
  status?: string
  mode?: string
  is_demo?: boolean
}

/**
 * 可选研究领域（内置前 9 个平铺展示；更多方向在弹窗里用下拉选择）。
 * 与后端抓取用的 arXiv 分类同口径（后端只做 JSONB 落库，不校验取值）。
 */
export const RESEARCH_FIELDS: Array<{ value: string; label: string }> = [
  { value: 'cs.AI', label: 'cs.AI · 人工智能' },
  { value: 'cs.CL', label: 'cs.CL · 计算语言学' },
  { value: 'cs.CV', label: 'cs.CV · 计算机视觉' },
  { value: 'cs.LG', label: 'cs.LG · 机器学习' },
  { value: 'cs.IR', label: 'cs.IR · 信息检索' },
  { value: 'cs.SE', label: 'cs.SE · 软件工程' },
  { value: 'cs.DB', label: 'cs.DB · 数据库' },
  { value: 'cs.HC', label: 'cs.HC · 人机交互' },
  { value: 'cs.MA', label: 'cs.MA · 多智能体' },
  { value: 'cs.NE', label: 'cs.NE · 神经与进化计算' },
  { value: 'cs.RO', label: 'cs.RO · 机器人学' },
  { value: 'cs.CR', label: 'cs.CR · 密码学与安全' },
  { value: 'stat.ML', label: 'stat.ML · 统计机器学习' },
  { value: 'eess.AS', label: 'eess.AS · 语音与音频' },
  { value: 'q-bio.BM', label: 'q-bio.BM · 生物分子' },
  { value: 'econ.EM', label: 'econ.EM · 计量经济' },
]

/**
 * 创建项目：`POST /projects`（Owner 写操作）。
 *
 * `settings` 落库：备注 + 研究方向 + 预算阈值（护栏 8.0 / 演示配额 3.0 / 迭代 3 轮，
 * 与 contracts.guardrails.cost 一致；弹窗里不再让用户填，直接带默认值）。
 */
export async function createProject(input: CreateProjectInput): Promise<CreatedProject> {
  return post<CreatedProject>('/projects', {
    body: {
      name: input.name.trim(),
      mode: input.mode ?? 'manual',
      settings: {
        note: input.note.trim(),
        fields: input.fields,
        budget: {
          max_llm_cost_usd: 8.0,
          demo_cost_quota_usd: 3.0,
          max_iterations: 3,
        },
      },
    },
  })
}

/** 判断是否为「只读面被拒」：public_demo 下写操作必然 403 */
export function isOwnerRequired(error: unknown): boolean {
  const status = (error as ApiError | undefined)?.status
  return status === 403
}
