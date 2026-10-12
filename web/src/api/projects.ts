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

import { del, get, patch, post, type ApiError } from './client'

export interface CreateProjectInput {
  name: string
  /** 备注（原「一句话研究问题」，落库在 settings.note） */
  note: string
  /** @deprecated 2026-09-26 起「研究方向」不再使用（新建弹窗已删掉该功能，接口层不再收） */
  fields?: string[]
  mode?: 'manual' | 'auto'
  /**
   * 这个项目在研究者电脑上的工作目录（可选）。
   *
   * 不填 = 自动在研究项目根下建一个 `<项目号>-<名字>`；填了就按填的建。
   * 目录是**真的在你电脑上建出来的**（服务端走宿主执行器）。
   */
  workspace_dir?: string
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
      workspace_dir: input.workspace_dir?.trim() || undefined,
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


/** 目录选择任务的状态（服务端固定集合，计划 §4.2） */
export type PickFolderStatus = 'pending' | 'selected' | 'canceled' | 'unavailable' | 'failed' | 'expired'

/** 目录选择任务的状态快照（`POST` 受理响应 / `GET` 状态 / `DELETE` 取消，同一形状） */
export interface PickFolderState {
  request_id: string
  status: PickFolderStatus
  path: string | null
  canceled: boolean
  /** **用户级文案**（不允许出现端口 / 毫秒数 / traceback） */
  message: string
  accepted?: boolean
  busy?: boolean
  created?: boolean
}

/**
 * 建一条「选目录」任务：请执行环境在研究员屏幕上弹出**系统自带**的文件夹选择框。
 *
 * ⚠️ 这里**只等"任务已受理"**（5 秒），不等他在窗口里挑完 ——
 * 研究员挑几十秒到几分钟都是正常的 `pending`，用 {@link pickFolderStatus} 轮询取结果。
 * 这条链路以前是同步等待，必然撞上 30 秒默认超时（页面显示"请求超时（30000ms）"）。
 */
export function pickFolder(title?: string): Promise<PickFolderState> {
  return post<PickFolderState>('/projects/pick-folder', {
    body: { title },
    timeoutMs: 5_000,
  })
}

/** 读一条目录选择任务的状态（终态可重复读，返回同一结果） */
export function pickFolderStatus(requestId: string): Promise<PickFolderState> {
  return get<PickFolderState>(`/projects/pick-folder/${encodeURIComponent(requestId)}`)
}

/** 取消一条目录选择任务（关掉同一个系统窗口；幂等） */
export function cancelPickFolder(requestId: string): Promise<PickFolderState> {
  return del<PickFolderState>(`/projects/pick-folder/${encodeURIComponent(requestId)}`)
}
