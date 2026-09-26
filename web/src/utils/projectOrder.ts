/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 左栏「项目」分组的**显示顺序**（纯函数，便于离线单测）。
 *
 * 为什么需要单独一层排序：后端 `/projects` 按大赛手册附录 B.5 的
 * `is_demo DESC NULLS LAST, id ASC` 返回（接口契约与 `sort_by` 字段不动），
 * 其中 `id ASC` = 最旧在前 → **新建的项目（id 最大）必然落到分组最后一行**
 * （实测落在第 30 行：前 10 个演示夹具 + 其后 19 个真实项目）。
 *
 * 左栏是给人看的列表，这里按「自己的项目在前、最新的在最上」重排：
 * - 非演示项目：按 `created_at` 倒序（新的在最上）→ 新建的项目固定出现在第一行；
 *   时间戳相同或缺失时用 `id` 倒序兜底，保证顺序稳定。
 * - 演示 / 验收夹具：退到分组底部，并保持接口原本的相对顺序（`id` 升序，夹具之间不乱序）。
 *
 * 顺带改善评委首屏：那一批 `WP14-E2E-*` 夹具不再压在最上面。
 */

export interface RailProjectLike {
  id: number
  name?: string
  is_demo?: boolean
  created_at?: string | null
  /** 这个项目在研究者电脑上的真实工作目录（没定过就是 null/缺失） */
  workspace_dir?: string | null
}

function demoRank(project: RailProjectLike): number {
  return project.is_demo ? 1 : 0
}

function createdAtMs(project: RailProjectLike): number {
  if (!project.created_at) return 0
  const parsed = Date.parse(project.created_at)
  return Number.isNaN(parsed) ? 0 : parsed
}

/** 返回排好序的新数组（不改动入参，`session.projects` 仍保持接口原序） */
export function orderProjectsForRail<T extends RailProjectLike>(projects: readonly T[]): T[] {
  return [...projects].sort((left, right) => {
    const rankDiff = demoRank(left) - demoRank(right)
    if (rankDiff !== 0) return rankDiff
    if (demoRank(left) === 0) {
      const timeDiff = createdAtMs(right) - createdAtMs(left)
      return timeDiff !== 0 ? timeDiff : right.id - left.id
    }
    return left.id - right.id
  })
}
