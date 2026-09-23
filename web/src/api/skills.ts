/**
 * 技能库接口（对应后端 `api/v1/skills.py`）。
 *
 * 口径：读公开、写要有研究者身份（`X-Owner-Token`，由 `api/client` 统一带上）。
 */
import { get, post, put, del } from '@/api/client'

/** 一个技能在列表里的样子（后端 `service.library()` 的条目） */
export interface SkillItem {
  name: string
  description: string
  stage: string
  stage_label: string
  /** `scripts` = 带脚本能真跑；`instructions` = 说明书型（模型照说明做，不跑脚本） */
  mode: 'scripts' | 'instructions'
  version: string
  source: string
  license: string
  enabled: boolean
  usable: boolean
  runnable: boolean
  steps: number
  scripts: number
  problems: string[]
  requires_env: Array<{ name: string; required: boolean }>
  /** 声明了环境变量但当前没配的（界面据此说"现在跑不了"） */
  missing_env: string[]
  /** 依赖体检结论（**没体检过是 null**：界面不许假装知道能不能跑） */
  health: SkillHealth | null
}

export interface SkillHealth {
  packages: string[]
  missing_python: string[]
  missing_env: string[]
  can_run: boolean
}

export interface SkillLibrary {
  items: SkillItem[]
  stages: Array<{ label: string; names: string[] }>
  mounts: string[]
  health_checked_at?: string
  health_probe_error?: string
  health_python?: string
  total: number
  enabled: number
  runnable: number
  state_loaded: boolean
}

export interface SkillRunRecord {
  skill: string
  topic: string
  ok: boolean
  outputs: Array<{ name: string; bytes: number; sha256: string }>
  message?: string
  finished_at?: string
}

export function fetchLibrary(): Promise<SkillLibrary> {
  return get<SkillLibrary>('/skills')
}

export function setSkillEnabled(name: string, enabled: boolean): Promise<{ name: string; enabled: boolean }> {
  return post<{ name: string; enabled: boolean }>(`/skills/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`, {
    body: enabled ? { enabled: true } : {},
  })
}

export function mountSkillsDir(path: string): Promise<{ mounts: string[]; message: string; library: SkillLibrary }> {
  return post('/skills/mounts', { body: { path } })
}

export function unmountSkillsDir(path: string): Promise<{ mounts: string[] }> {
  return del('/skills/mounts', { query: { path } })
}

export function saveSkill(name: string, content: string): Promise<{ ok: boolean; message: string; problems: string[] }> {
  return put(`/skills/${encodeURIComponent(name)}`, { body: { content } })
}

/** 某个技能的完整说明 + **当前 SKILL.md 原文**（编辑时要拿它，别把原内容覆盖成空壳） */
export function fetchSkillDetail(name: string): Promise<{
  name: string
  description: string
  stage: string
  content: string
  problems: string[]
}> {
  return get(`/skills/${encodeURIComponent(name)}`)
}

/** 体检一次：算依赖 + 在研究者的电脑上跑一条只读探针（看包/密钥在不在） */
export function runSkillHealthCheck(): Promise<{
  python: string
  probe_error: string
  probed_packages: string[]
  skills: Record<string, SkillHealth>
  checked_at: string
}> {
  return post('/skills/health-check', { body: {} })
}

export function fetchSkillRuns(taskId: string): Promise<{ task_id: string; runs: SkillRunRecord[] }> {
  return get('/skills/runs', { query: { task_id: taskId } })
}
