/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 知识库（Knowledge Base）契约层。
 *
 * 模型：**一个文件系统**。每个条目都是"一个有扩展名的文件"——
 * PDF 是 `.pdf`，论文摘录 / 技能（已移出）/ 记忆都存成 `.md`，上传的代码是 `.py`。
 * 因此「怎么展示」只由扩展名决定（见 `rendererOf`），不存在"文件式条目 / 数据式条目"的分叉。
 *
 * 持久化：真实后端文件系统。历史说明（已替换）：数据落在浏览器 `localStorage`（键 `sciloop.kb.v2`），首次进入播种示例数据，
 * 刷新不丢；**不写后端、不新增迁移**。二进制格式（pdf / docx）没有真实字节，
 * 只有登记信息，需接后端后才能真正预览（文本类与表格类是**真有内容、真能预览**的）。
 *
 * 与真后端的对应关系（server 侧实现后，把下面函数体换成 `get/post/patch/del` 即可）：
 *   GET    /knowledge/entries?q=&bucket=&folder=&project_id=&since=   → { items, folders }
 *   POST   /knowledge/entries                                        → KnowledgeEntry
 *   PATCH  /knowledge/entries/{id}                                   → KnowledgeEntry
 *   DELETE /knowledge/entries?ids=a,b,c                              → { deleted: number }
 *   POST   /knowledge/folders  { path }                              → { folders: string[] }
 *   POST   /knowledge/files    （multipart，字段名 files）             → 文件登记（真上传落盘时用）
 */
import { del, get, patch, post } from '@/api/client'

/* ------------------------------------------------------------------ *
 * 一、格式表：扩展名 → 展示名 + 用什么渲染
 * ------------------------------------------------------------------ */

/** 阅读器渲染方式 */
export type RendererKind =
  | 'pdf'
  | 'docx'
  | 'sheet'
  | 'markdown'
  | 'code'
  | 'text'
  | 'image'
  | 'unsupported'

/** 图标种类（前端自绘 SVG，不引图标包） */
export type IconKind = 'pdf' | 'doc' | 'sheet' | 'image' | 'code' | 'markdown' | 'text' | 'archive' | 'other'

const EXT_TABLE: Record<string, { label: string; renderer: RendererKind; icon: IconKind }> = {
  pdf: { label: 'PDF', renderer: 'pdf', icon: 'pdf' },
  docx: { label: 'Word 文档', renderer: 'docx', icon: 'doc' },
  doc: { label: 'Word 文档（旧格式）', renderer: 'unsupported', icon: 'doc' },
  xlsx: { label: 'Excel 表格', renderer: 'sheet', icon: 'sheet' },
  xls: { label: 'Excel 表格（旧格式）', renderer: 'sheet', icon: 'sheet' },
  csv: { label: 'CSV 表格', renderer: 'sheet', icon: 'sheet' },
  tsv: { label: 'TSV 表格', renderer: 'sheet', icon: 'sheet' },
  md: { label: 'Markdown', renderer: 'markdown', icon: 'markdown' },
  markdown: { label: 'Markdown', renderer: 'markdown', icon: 'markdown' },
  txt: { label: '纯文本', renderer: 'text', icon: 'text' },
  log: { label: '日志', renderer: 'text', icon: 'text' },
  py: { label: 'Python', renderer: 'code', icon: 'code' },
  ipynb: { label: 'Jupyter Notebook', renderer: 'code', icon: 'code' },
  js: { label: 'JavaScript', renderer: 'code', icon: 'code' },
  mjs: { label: 'JavaScript', renderer: 'code', icon: 'code' },
  cjs: { label: 'JavaScript', renderer: 'code', icon: 'code' },
  ts: { label: 'TypeScript', renderer: 'code', icon: 'code' },
  tsx: { label: 'TypeScript', renderer: 'code', icon: 'code' },
  jsx: { label: 'JavaScript', renderer: 'code', icon: 'code' },
  vue: { label: 'Vue', renderer: 'code', icon: 'code' },
  java: { label: 'Java', renderer: 'code', icon: 'code' },
  c: { label: 'C', renderer: 'code', icon: 'code' },
  h: { label: 'C 头文件', renderer: 'code', icon: 'code' },
  cpp: { label: 'C++', renderer: 'code', icon: 'code' },
  hpp: { label: 'C++ 头文件', renderer: 'code', icon: 'code' },
  cs: { label: 'C#', renderer: 'code', icon: 'code' },
  go: { label: 'Go', renderer: 'code', icon: 'code' },
  rs: { label: 'Rust', renderer: 'code', icon: 'code' },
  rb: { label: 'Ruby', renderer: 'code', icon: 'code' },
  php: { label: 'PHP', renderer: 'code', icon: 'code' },
  sh: { label: 'Shell', renderer: 'code', icon: 'code' },
  bash: { label: 'Shell', renderer: 'code', icon: 'code' },
  sql: { label: 'SQL', renderer: 'code', icon: 'code' },
  r: { label: 'R', renderer: 'code', icon: 'code' },
  m: { label: 'MATLAB', renderer: 'code', icon: 'code' },
  tex: { label: 'LaTeX', renderer: 'code', icon: 'code' },
  bib: { label: 'BibTeX', renderer: 'code', icon: 'code' },
  json: { label: 'JSON', renderer: 'code', icon: 'code' },
  yaml: { label: 'YAML', renderer: 'code', icon: 'code' },
  yml: { label: 'YAML', renderer: 'code', icon: 'code' },
  toml: { label: 'TOML', renderer: 'code', icon: 'code' },
  ini: { label: 'INI', renderer: 'code', icon: 'code' },
  xml: { label: 'XML', renderer: 'code', icon: 'code' },
  html: { label: 'HTML', renderer: 'code', icon: 'code' },
  css: { label: 'CSS', renderer: 'code', icon: 'code' },
  png: { label: 'PNG 图片', renderer: 'image', icon: 'image' },
  jpg: { label: 'JPEG 图片', renderer: 'image', icon: 'image' },
  jpeg: { label: 'JPEG 图片', renderer: 'image', icon: 'image' },
  gif: { label: 'GIF 图片', renderer: 'image', icon: 'image' },
  webp: { label: 'WebP 图片', renderer: 'image', icon: 'image' },
  bmp: { label: 'BMP 图片', renderer: 'image', icon: 'image' },
  svg: { label: 'SVG 图片', renderer: 'image', icon: 'image' },
  zip: { label: '压缩包', renderer: 'unsupported', icon: 'archive' },
  rar: { label: '压缩包', renderer: 'unsupported', icon: 'archive' },
  '7z': { label: '压缩包', renderer: 'unsupported', icon: 'archive' },
  gz: { label: '压缩包', renderer: 'unsupported', icon: 'archive' },
  tar: { label: '压缩包', renderer: 'unsupported', icon: 'archive' },
  pptx: { label: 'PowerPoint', renderer: 'unsupported', icon: 'doc' },
  ppt: { label: 'PowerPoint（旧格式）', renderer: 'unsupported', icon: 'doc' },
  mp4: { label: '视频', renderer: 'unsupported', icon: 'other' },
  webm: { label: '视频', renderer: 'unsupported', icon: 'other' },
  mp3: { label: '音频', renderer: 'unsupported', icon: 'other' },
  wav: { label: '音频', renderer: 'unsupported', icon: 'other' },
}

export interface FormatInfo {
  ext: string
  label: string
  renderer: RendererKind
  icon: IconKind
}

/** 取扩展名（小写、不含点） */
export function extOf(name: string): string {
  const index = name.lastIndexOf('.')
  if (index < 0 || index === name.length - 1) return ''
  return name.slice(index + 1).toLowerCase()
}

/**
 * 扩展名 → 格式信息。
 *
 * 表里没有的扩展名：**有文本内容就按纯文本看**（比一律"不支持"有用），没内容才算不支持。
 */
export function formatOf(name: string, hasContent = false): FormatInfo {
  const ext = extOf(name)
  const known = EXT_TABLE[ext]
  if (known) return { ext, ...known }
  if (hasContent) return { ext, label: ext ? `${ext.toUpperCase()} 文件` : '未知类型', renderer: 'text', icon: 'text' }
  return { ext, label: ext ? `${ext.toUpperCase()} 文件` : '未知类型', renderer: 'unsupported', icon: 'other' }
}

/** 文件类型列的显示名（用户要求：只显示文件格式，不显示条目类型） */
export function formatLabelOf(name: string, hasContent = false): string {
  return formatOf(name, hasContent).label
}

/* ------------------------------------------------------------------ *
 * 二、分类（左栏）与条目
 * ------------------------------------------------------------------ */

/** 条目归属分类：全部 / 最近 / 回收站是"视图"，其余是真实分类 */
export type KnowledgeBucket = 'literature' | 'idea' | 'experiment' | 'paper' | 'memory'
export type KnowledgeScope = KnowledgeBucket | 'all' | 'recent' | 'trash'

export interface BucketMeta {
  key: KnowledgeScope
  label: string
}

/** 左栏顺序（用户 2026-09-22 指定）：全部 → 最近 → 文献 → idea → 实验 → 论文 → 记忆 → 回收站 */
export const KB_BUCKETS: readonly BucketMeta[] = [
  { key: 'all', label: '全部' },
  { key: 'recent', label: '最近' },
  { key: 'literature', label: '文献' },
  { key: 'idea', label: 'idea' },
  { key: 'experiment', label: '实验' },
  { key: 'paper', label: '论文' },
  { key: 'memory', label: '记忆' },
  { key: 'trash', label: '回收站' },
]

export const BUCKET_LABELS: Record<KnowledgeBucket, string> = {
  literature: '文献',
  idea: 'idea',
  experiment: '实验',
  paper: '论文',
  memory: '记忆',
}

/** 一个条目 = 一个有扩展名的文件 */
export interface KnowledgeEntry {
  id: string
  /** 文件名，含扩展名 */
  name: string
  bucket: KnowledgeBucket
  /** 文本内容（md / 代码 / csv 等）；二进制格式为空串 */
  content: string
  /** 字节数（上传时登记的真实大小；文本类按内容算） */
  size: number
  /** 所在文件夹路径（空数组 = 根目录） */
  folder: string[]
  tags: string[]
  project_id: number | null
  source_label: string
  source_route: string | null
  /** 真实文件地址（接后端后才有；demo 里二进制文件为 null） */
  file_url: string | null
  /**
   * 是否有正文。**列表接口只回这个布尔量，不回 `content`**（正文按需走详情接口取）——
   * 否则 915 条的列表响应会到 46MB，页面会一直卡在加载态。
   */
  has_content?: boolean
  /** 导入时间 */
  imported_at: string
  /** 最近一次内容修改 */
  modified_at: string
  /** 最近一次移动/改名（含移动到文件夹） */
  moved_at: string | null
  /** 进回收站的时间；null = 不在回收站 */
  trashed_at: string | null
}

export type KnowledgeDraft = Omit<KnowledgeEntry, 'id' | 'imported_at' | 'modified_at' | 'moved_at' | 'trashed_at'>

export function emptyDraft(bucket: KnowledgeBucket, folder: string[] = []): KnowledgeDraft {
  return {
    name: '',
    bucket,
    content: '',
    size: 0,
    folder,
    tags: [],
    project_id: null,
    source_label: '手动新建',
    source_route: null,
    file_url: null,
  }
}

/** 时间筛选项（用户定的四档固定） */
export type TimeRange = 'today' | 'week' | 'month' | 'older'

export const TIME_RANGES: ReadonlyArray<{ key: TimeRange; label: string }> = [
  { key: 'today', label: '今天' },
  { key: 'week', label: '近 7 天' },
  { key: 'month', label: '近 30 天' },
  { key: 'older', label: '更早' },
]

export type SortField = 'name' | 'size' | 'modified'
export type SortOrder = 'asc' | 'desc'

export interface KnowledgeSnapshot {
  items: KnowledgeEntry[]
  /** 所有文件夹路径（含空文件夹） */
  folders: string[]
}

export interface KnowledgeQuery {
  q?: string
  scope?: KnowledgeScope
  folder?: string[]
  projectId?: number | null
  timeRange?: TimeRange | null
}

/* ------------------------------------------------------------------ *
 * 三、派生视图（纯函数，界面与导出共用一份口径）
 * ------------------------------------------------------------------ */

/**
 * 文件夹路径 → 字符串。
 *
 * ⚠️ 入参按 `string[]` 声明，但**要容忍历史脏数据**：迁移进来的条目里存在
 * `folder: "uploads"` 这种字符串形态，`folder.join()` 会直接抛 TypeError，
 * 而它跑在 computed 里 → **一条坏数据就把整页渲染打断，界面永远停在骨架**。
 * 后端已统一归一（见 `services/knowledge_base.py` 的 `_folder_list`），
 * 这里再兜一层，保证前端不会因为一条数据整页白掉。
 */
export function folderPath(folder: string[] | string | null | undefined): string {
  if (Array.isArray(folder)) return folder.join('/')
  if (typeof folder === 'string') return folder
  return ''
}

/**
 * 「这条有没有正文」。
 *
 * 列表接口只回元数据（正文一律不回，否则 915 条的响应会到 46MB），因此这里优先看后端给的
 * `has_content`；拿不到该字段时（例如条目详情、旧响应）再退回按 `content` 判断。
 */
export function hasContent(entry: KnowledgeEntry): boolean {
  if (typeof entry.has_content === 'boolean') return entry.has_content
  return (entry.content ?? '').trim().length > 0
}

/** 表格「修改时间」列显示的值：导入 / 移动 / 编辑三者取最近 */
export function lastTouched(entry: KnowledgeEntry): string {
  return [entry.imported_at, entry.modified_at, entry.moved_at]
    .filter((value): value is string => !!value)
    .sort()
    .reverse()[0] ?? entry.imported_at
}

/** 悬停提示里的三个原始时间 */
export function timeTrail(entry: KnowledgeEntry): string {
  const parts = [`导入 ${formatDateTime(entry.imported_at)}`]
  if (entry.modified_at !== entry.imported_at) parts.push(`编辑 ${formatDateTime(entry.modified_at)}`)
  if (entry.moved_at) parts.push(`移动 ${formatDateTime(entry.moved_at)}`)
  return parts.join(' · ')
}

function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '时间未知'
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '时间未知'
  const pad = (value: number) => String(value).padStart(2, '0')
  const now = new Date()
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  if (sameDay) return `今天 ${pad(date.getHours())}:${pad(date.getMinutes())}`
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function formatSize(bytes: number): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`
  return `${(bytes / 1048576).toFixed(1)} MB`
}

function inTimeRange(iso: string, range: TimeRange | null | undefined): boolean {
  if (!range) return true
  const time = new Date(iso).getTime()
  if (Number.isNaN(time)) return true
  const ageMs = Date.now() - time
  const day = 86_400_000
  switch (range) {
    case 'today':
      return ageMs < day
    case 'week':
      return ageMs < 7 * day
    case 'month':
      return ageMs < 30 * day
    default:
      return ageMs >= 30 * day
  }
}

/** 关键词命中：文件名 / 内容（全文搜，跨文件夹）/ 标签 / 来源 */
export function matchesQuery(entry: KnowledgeEntry, rawQuery: string): boolean {
  const q = rawQuery.trim().toLowerCase()
  if (!q) return true
  return [entry.name, entry.content, entry.tags.join(' '), entry.source_label, folderPath(entry.folder)]
    .join(' ')
    .toLowerCase()
    .includes(q)
}

/** 分类 + 搜索 + 筛选（不含排序） */
export function filterEntries(entries: KnowledgeEntry[], query: KnowledgeQuery): KnowledgeEntry[] {
  const scope = query.scope ?? 'all'
  return entries.filter((entry) => {
    if (scope === 'trash') {
      if (!entry.trashed_at) return false
    } else {
      if (entry.trashed_at) return false
      if (scope !== 'all' && scope !== 'recent' && entry.bucket !== scope) return false
    }
    // 搜索是全局的（跨文件夹）；没在搜时才按当前文件夹收窄
    if (!query.q?.trim() && query.folder) {
      if (folderPath(entry.folder) !== folderPath(query.folder)) return false
    }
    if (query.projectId && entry.project_id !== query.projectId) return false
    if (!inTimeRange(lastTouched(entry), query.timeRange)) return false
    return matchesQuery(entry, query.q ?? '')
  })
}

export function sortEntries(entries: KnowledgeEntry[], field: SortField, order: SortOrder): KnowledgeEntry[] {
  const sorted = [...entries]
  const sign = order === 'asc' ? 1 : -1
  sorted.sort((a, b) => {
    if (field === 'name') return sign * a.name.localeCompare(b.name, 'zh-Hans-CN')
    if (field === 'size') return sign * (a.size - b.size)
    return sign * lastTouched(a).localeCompare(lastTouched(b))
  })
  return sorted
}

/* ------------------------------------------------------------------ *
 * 四、文件系统 API
 * ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ *
 * 列表快照的前端缓存（stale-while-revalidate）
 *
 * 为什么要有：切分类/在页面间来回走时，重新拉一次快照会让界面白白等网络
 * （哪怕后端只要 0.2s，用户看到的是骨架闪一下）。这里把最近一次快照留在
 * 模块作用域（组件销毁也不丢），命中就**先渲染出来**，再后台静默刷新。
 * 所有写操作成功后立即失效，保证不会拿旧数据当真。
 * ------------------------------------------------------------------ */
export const SNAPSHOT_TTL_MS = 30_000

let snapshotCache: { at: number; data: KnowledgeSnapshot } | null = null

/** 取缓存（没有则 null）——同步返回，用于"先渲染再刷新"。 */
export function peekSnapshot(): KnowledgeSnapshot | null {
  return snapshotCache?.data ?? null
}

/** 缓存年龄（毫秒）；没有缓存返回 null。 */
export function snapshotAgeMs(): number | null {
  return snapshotCache ? Date.now() - snapshotCache.at : null
}

/** 主动失效（写操作后调用，也用于"重新读取"按钮强制拉新）。 */
export function invalidateSnapshot(): void {
  snapshotCache = null
}

/** 拉一份快照并写入缓存。 */
export async function refreshSnapshot(): Promise<KnowledgeSnapshot> {
  const data = await get<KnowledgeSnapshot>('/knowledge/entries')
  snapshotCache = { at: Date.now(), data }
  return data
}

export function loadSnapshot(): Promise<KnowledgeSnapshot> {
  return refreshSnapshot()
}

/** 单条详情（**含正文**）——列表不带正文，阅读器打开时按需取这一条。 */
export function loadEntry(id: string): Promise<KnowledgeEntry> {
  return get<KnowledgeEntry>(`/knowledge/entries/${encodeURIComponent(id)}`)
}

/** 批量详情（**含正文**）——导出这类"要正文"的操作一次取回。 */
export async function loadEntries(ids: string[]): Promise<KnowledgeEntry[]> {
  if (!ids.length) return []
  const result = await post<{ items: KnowledgeEntry[] }>('/knowledge/entries/details', { body: { ids } })
  return result.items
}

/** 写操作成功后统一失效缓存，避免拿旧列表当真。 */
async function mutating<T>(work: Promise<T>): Promise<T> {
  const result = await work
  invalidateSnapshot()
  return result
}

export function createEntry(draft: KnowledgeDraft): Promise<KnowledgeEntry> {
  return mutating(post<KnowledgeEntry>('/knowledge/entries', { body: draft }))
}

export function updateEntry(id: string, body: Partial<KnowledgeDraft>): Promise<KnowledgeEntry> {
  return mutating(patch<KnowledgeEntry>(`/knowledge/entries/${id}`, { body }))
}

export function uploadEntry(file: File, draft: Pick<KnowledgeDraft, 'bucket' | 'folder' | 'tags' | 'project_id'>): Promise<KnowledgeEntry> {
  const body = new FormData()
  body.append('file', file)
  body.append('bucket', draft.bucket)
  body.append('folder', folderPath(draft.folder))
  body.append('tags', JSON.stringify(draft.tags))
  if (draft.project_id !== null) body.append('project_id', String(draft.project_id))
  return mutating(post<KnowledgeEntry>('/knowledge/files', { body, timeoutMs: 120_000 }))
}

async function mutateEntries(action: string, ids: string[], extra: Record<string, unknown> = {}): Promise<number> {
  if (!ids.length) return 0
  const result = await mutating(
    post<{ changed: number }>(`/knowledge/entries/${action}`, { body: { ids, ...extra } }),
  )
  return result.changed
}

export const trashEntries = (ids: string[]) => mutateEntries('trash', ids)
export const restoreEntries = (ids: string[]) => mutateEntries('restore', ids)
export const moveEntries = (ids: string[], folder: string[]) => mutateEntries('move', ids, { folder })
export const tagEntries = (ids: string[], tag: string) => mutateEntries('tag', ids, { tag })

export async function deleteEntries(ids: string[]): Promise<number> {
  if (!ids.length) return 0
  const result = await mutating(
    del<{ deleted: number }>('/knowledge/entries', { query: { ids: ids.join(',') } }),
  )
  return result.deleted
}

export async function emptyTrash(): Promise<number> {
  const snapshot = await loadSnapshot()
  return deleteEntries(snapshot.items.filter((entry) => entry.trashed_at).map((entry) => entry.id))
}

export async function createFolder(folder: string[]): Promise<string[]> {
  const result = await mutating(post<{ folders: string[] }>('/knowledge/folders', { body: { folder } }))
  return result.folders
}

/* ------------------------------------------------------------------ *
 * 六、导出
 * ------------------------------------------------------------------ */

export function exportJson(entries: KnowledgeEntry[]): string {
  return JSON.stringify({ exported_at: new Date().toISOString(), total: entries.length, items: entries }, null, 2)
}

export function exportMarkdown(entries: KnowledgeEntry[]): string {
  const lines: string[] = ['# 知识库导出', '', `共 ${entries.length} 条。`, '']
  for (const entry of entries) {
    const location = folderPath(entry.folder)
    lines.push(`## ${entry.name}`, '')
    lines.push(
      `- 分类：${BUCKET_LABELS[entry.bucket]}`,
      `- 位置：知识库${location ? ` / ${location.split('/').join(' / ')}` : ''}`,
      `- 来源：${entry.source_label}`,
      `- 导入：${formatDateTime(entry.imported_at)}`,
    )
    if (entry.tags.length) lines.push(`- 标签：${entry.tags.join(' / ')}`)
    lines.push('')
    if ((entry.content ?? '').trim()) lines.push((entry.content ?? '').trim(), '')
  }
  return lines.join('\n')
}
