/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 全局设置 API（后端 `app/api/v1/settings.py`）。
 *
 * 契约：
 * - `GET /settings`          全部 scope（公开只读，设置页首屏一次拉全）
 * - `GET /settings/{scope}`  单个 scope（公开只读），返回 **默认值 ⊕ 已存值**
 * - `PUT /settings/{scope}`  部分更新（**Owner 面**；匿名 403 `owner_token_required`）
 *
 * 两条纪律：
 * 1. 传输层只走 `./client`（baseURL / 超时 / `X-Owner-Token` 注入都由它独占，
 *    `PUT` 属写方法，令牌会自动带上，本文件不自行读写 sessionStorage）。
 * 2. **默认值一律以服务端返回的 `defaults` 为准**，前端不再抄一份副本——
 *    否则两处漂移，界面「恢复默认」会与后端真实默认值不一致。
 */
import { get, put } from './client'

/** 打开论文时的默认版本（与 `reader_versions.kind` 对齐） */
export type ReadingKind = 'original' | 'chinese'

/** 目标版本缺失时的行为 */
export type MissingVersionAction = 'fallback_original' | 'prompt_generate'

/** 行距档位（后端只接受这三档） */
export type LineHeight = 1.45 | 1.6 | 1.85

/** `reading` scope 的字段表（服务端白名单，传未知键会 422 `unknown_setting_key`） */
export interface ReadingSettings {
  default_kind: ReadingKind
  missing_version_action: MissingVersionAction
  /** 整数 14–24 */
  font_size: number
  line_height: LineHeight
  pair_view: boolean
  annotations_visible: boolean
  anchor_highlight: boolean
  /** 最多 50 项 */
  favorite_terms: string[]
}

/** 单 scope 响应：`settings` 是「默认值 ⊕ 已存值」，`defaults` 供界面「恢复默认」 */
export interface SettingsScope<T> {
  scope: string
  title: string
  settings: T
  defaults: T
  updated_at: string | null
}

/** `GET /settings` 的条目（按 scope 分组，字段不做窄化） */
export interface SettingsScopeSummary {
  scope: string
  title: string
  settings: Record<string, unknown>
  defaults: Record<string, unknown>
  updated_at: string | null
}

export interface SettingsList {
  items: SettingsScopeSummary[]
  total: number
}

/** 全部设置分组（公开只读） */
export function listSettingsScopes(): Promise<SettingsList> {
  return get<SettingsList>('/settings')
}

/** 读阅读设置（公开只读） */
export function getReadingSettings(): Promise<SettingsScope<ReadingSettings>> {
  return get<SettingsScope<ReadingSettings>>('/settings/reading')
}

/**
 * 更新阅读设置（**部分更新**：只传要改的键，未传的键保持原值）。
 * 返回写入后的完整值（同样是「默认值 ⊕ 已存值」）。
 */
export function putReadingSettings(
  patch: Partial<ReadingSettings>,
): Promise<SettingsScope<ReadingSettings>> {
  return put<SettingsScope<ReadingSettings>>('/settings/reading', { body: patch })
}
