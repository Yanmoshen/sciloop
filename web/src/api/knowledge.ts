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
 * 现状：**纯前端 demo**。数据落在浏览器 `localStorage`（键 `sciloop.kb.v2`），首次进入播种示例数据，
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

export function folderPath(folder: string[]): string {
  return folder.join('/')
}

export function hasContent(entry: KnowledgeEntry): boolean {
  return entry.content.trim().length > 0
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
 * 四、本地实现（demo）：localStorage + 示例播种
 * ------------------------------------------------------------------ */

const STORE_KEY = 'sciloop.kb.v2'
const SEED_KEY = 'sciloop.kb.v2.seed'

interface Persisted {
  items: KnowledgeEntry[]
  folders: string[]
}

function nowIso(minutesAgo = 0): string {
  return new Date(Date.now() - minutesAgo * 60_000).toISOString()
}

const BENCH_PY = `"""长文本推理延迟基准：跑三档序列长度，各测 5 次取中位数。"""

import statistics
import time

SEQ_LENGTHS = [8192, 16384, 32768]
REPEAT = 5


def measure(seq_len: int) -> float:
    samples = []
    for _ in range(REPEAT):
        start = time.perf_counter()
        run_once(seq_len)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def main() -> None:
    for seq_len in SEQ_LENGTHS:
        latency = measure(seq_len)
        print(f"seq={seq_len:>6}  {latency * 1000:6.1f} ms/token")
`

const LOG_CSV = `seq_len,tokens_total,ms_per_token,peak_memory_gb,note
8192,8192,41.2,18.4,基线
16384,16384,63.8,27.1,未见明显退化
32768,32768,118.4,41.9,超过预算阈值`

const LITERATURE_MD = `# 块稀疏注意力在 32k 上下文下的显存占用

## 研究问题
块稀疏注意力在 32k 上下文下能否把显存压到 1/4 以内而不掉点。

## 核心方法
固定块大小 64，块内稠密、块间按学习到的路由选择 top-k 块。

## 主要结论
32k 上下文显存降至 27%，下游任务平均掉 0.4 个点。

> 摘录自论文库 #482 的解析卡片，字段均带原文定位。
`

const IDEA_MD = `# 块稀疏 + 分块 KV 缓存的组合方案

把块稀疏路由与分块 KV 缓存合起来：路由决定哪些块需要常驻显存，其余按需重算。

- 生成机制：组合（两篇工作各取一半）
- 证据：2 条（论文库 #482、#517）
- 风险：两条链路的误差会叠加，需要先做小规模对照实验
`

function seed(): Persisted {
  const entry = (
    partial: Pick<KnowledgeEntry, 'name' | 'bucket'> & Partial<KnowledgeEntry>,
    minutesAgo: number,
  ): KnowledgeEntry => {
    const base: KnowledgeEntry = {
      id: `kb-${Math.random().toString(36).slice(2, 10)}`,
      name: partial.name,
      bucket: partial.bucket,
      content: partial.content ?? '',
      size: partial.size ?? 0,
      folder: partial.folder ?? [],
      tags: partial.tags ?? [],
      project_id: partial.project_id ?? null,
      source_label: partial.source_label ?? '本地上传',
      source_route: partial.source_route ?? null,
      file_url: partial.file_url ?? null,
      imported_at: nowIso(minutesAgo),
      modified_at: nowIso(minutesAgo),
      moved_at: null,
      trashed_at: null,
    }
    return base
  }

  return {
    folders: ['文献综述', '实验', '实验/2026-09-18', '论文初稿'],
    items: [
      entry(
        {
          name: '块稀疏注意力在 32k 上下文下的显存占用.md',
          bucket: 'literature',
          content: LITERATURE_MD,
          size: LITERATURE_MD.length,
          folder: ['文献综述'],
          tags: ['块稀疏', '显存'],
          source_label: '论文库 · #482',
          source_route: '/papers/parse/482',
        },
        95,
      ),
      entry(
        {
          name: '滑窗+全局 token 混合方案.md',
          bucket: 'literature',
          content:
            '# 滑窗 + 全局 token 的混合方案\n\n只在 8k 长度上评测，未见 32k 结果。\n\n作者说 32k 结果在补，先按 8k 的口径记着，别当成结论用。\n',
          size: 180,
          folder: ['文献综述'],
          tags: ['滑窗', '待读'],
          source_label: '论文库 · #517',
          source_route: '/papers/parse/517',
        },
        300,
      ),
      entry(
        {
          name: '稀疏注意力综述-阅读笔记.md',
          bucket: 'literature',
          content:
            '# 阅读笔记\n\n按「能否直接换掉 softmax」把 12 篇工作分了三组：\n\n1. 固定模式（滑窗 / 全局 token）\n2. 可训练稀疏模式（块路由）——**与我方方案最接近**\n3. 近似全注意力（低秩、核方法）\n',
          size: 210,
          folder: ['文献综述'],
          tags: ['综述', '已读'],
        },
        1_260,
      ),
      entry(
        {
          name: '块稀疏+分块KV缓存的组合方案.md',
          bucket: 'idea',
          content: IDEA_MD,
          size: IDEA_MD.length,
          tags: ['候选方案'],
          source_label: '研究构思 · idea #37',
          source_route: '/ideas',
        },
        88,
      ),
      entry(
        {
          name: '规则化路由：以可解释性换效率.md',
          bucket: 'idea',
          content:
            '# 规则化路由\n\n用「句法边界 + 实体位置」的规则表替代学习到的路由，换取可解释性；代价是可能需要更多算力。\n',
          size: 150,
          tags: ['可解释性', '待评估'],
          source_label: '研究构思 · idea #41',
          source_route: '/ideas',
        },
        420,
      ),
      entry(
        {
          name: '实验日志-2026-09-18.csv',
          bucket: 'experiment',
          content: LOG_CSV,
          size: LOG_CSV.length,
          folder: ['实验', '2026-09-18'],
          tags: ['实验记录', '延迟'],
        },
        240,
      ),
      entry(
        {
          name: 'run_benchmark.py',
          bucket: 'experiment',
          content: BENCH_PY,
          size: BENCH_PY.length,
          folder: ['实验'],
          tags: ['基准脚本'],
        },
        250,
      ),
      entry(
        {
          name: '方法章节草稿.md',
          bucket: 'paper',
          content:
            '# 3 方法\n\n## 3.1 块级路由\n\n我们把注意力矩阵按 64×64 分块，块内保持稠密计算，块间由一个轻量路由网络选择 top-k…\n\n## 3.2 分块 KV 缓存\n\n（待补：缓存驱逐策略与重算代价的推导）\n',
          size: 260,
          folder: ['论文初稿'],
          tags: ['写作中'],
          project_id: null,
        },
        45,
      ),
      entry(
        {
          name: '审稿意见-第一轮.md',
          bucket: 'paper',
          content:
            '# 第一轮评审意见\n\n1. 与 #482 的差异需要在实验部分说清（审稿人 2 提了两次）\n2. 32k 的延迟数据只有一次运行，缺误差棒\n3. 图 3 的图注与正文不一致\n',
          size: 200,
          folder: ['论文初稿'],
          tags: ['评审', '待改'],
        },
        130,
      ),
      entry(
        {
          name: '结论必须附原文页码.md',
          bucket: 'memory',
          content:
            '# 结论必须附原文页码\n\n凡是写进产出物的结论，都要能指回原文的章节与页码；只给摘要的一律退回重做。\n\n来源：关于引用规范的讨论\n',
          size: 160,
          tags: ['引用规范'],
          source_label: '来自对话 · 引用规范',
          source_route: '/c/8f3a2b1c',
        },
        60,
      ),
      entry(
        {
          name: '翻译用块级原位译写，不引第三方整页翻译.md',
          bucket: 'memory',
          content:
            '# 翻译方案取舍\n\nPDF 翻译走 PyMuPDF 块级原位译写；排版冲突如实写进 layout_warnings，不假装排版无损。\n',
          size: 140,
          tags: ['翻译', '技术选型'],
          source_label: '来自对话 · 翻译方案',
          source_route: '/c/2d7e44a9',
        },
        1_500,
      ),
    ],
  }
}

function read(): Persisted | null {
  if (typeof localStorage === 'undefined') return null
  const raw = localStorage.getItem(STORE_KEY)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as Persisted
    if (!parsed || !Array.isArray(parsed.items)) return null
    return { items: parsed.items, folders: Array.isArray(parsed.folders) ? parsed.folders : [] }
  } catch {
    return null
  }
}

function write(data: Persisted): void {
  if (typeof localStorage === 'undefined') return
  localStorage.setItem(STORE_KEY, JSON.stringify(data))
}

/** 首次进入播种；用户清空后不再自动塞回来 */
export function ensureSeeded(): void {
  if (typeof localStorage === 'undefined') return
  if (localStorage.getItem(SEED_KEY) === '1') return
  localStorage.setItem(SEED_KEY, '1')
  if (!read()) write(seed())
}

/* ------------------------------------------------------------------ *
 * 五、读写接口（远端可用时走远端，否则走本地）
 * ------------------------------------------------------------------ */

/** 设为 '1' 即走真后端（server 侧实现 /knowledge 之后） */
const REMOTE = import.meta.env?.VITE_KB_REMOTE === '1'

export async function loadSnapshot(): Promise<KnowledgeSnapshot> {
  if (REMOTE) {
    return get<KnowledgeSnapshot>('/knowledge/entries', { query: { page_size: 500 } })
  }
  ensureSeeded()
  return read() ?? { items: [], folders: [] }
}

export async function createEntry(draft: KnowledgeDraft): Promise<KnowledgeEntry> {
  if (REMOTE) return post<KnowledgeEntry>('/knowledge/entries', { body: draft })
  const stamp = new Date().toISOString()
  const entry: KnowledgeEntry = {
    ...draft,
    id: `kb-${Math.random().toString(36).slice(2, 10)}`,
    imported_at: stamp,
    modified_at: stamp,
    moved_at: null,
    trashed_at: null,
  }
  const data = read() ?? { items: [], folders: [] }
  const nextFolders = ensureFolderPath(data.folders, draft.folder)
  write({ items: [entry, ...data.items], folders: nextFolders })
  return entry
}

export async function updateEntry(id: string, patchBody: Partial<KnowledgeDraft>): Promise<KnowledgeEntry | null> {
  if (REMOTE) return patch<KnowledgeEntry>(`/knowledge/entries/${id}`, { body: patchBody })
  const data = read() ?? { items: [], folders: [] }
  const index = data.items.findIndex((item) => item.id === id)
  if (index < 0) return null
  const current = data.items[index]!
  const touchedContent = patchBody.content !== undefined && patchBody.content !== current.content
  const next: KnowledgeEntry = {
    ...current,
    ...patchBody,
    modified_at: touchedContent ? new Date().toISOString() : current.modified_at,
  }
  data.items[index] = next
  write(data)
  return next
}

/** 移入回收站 */
export async function trashEntries(ids: string[]): Promise<number> {
  const data = read() ?? { items: [], folders: [] }
  const stamp = new Date().toISOString()
  let changed = 0
  const items = data.items.map((entry) => {
    if (!ids.includes(entry.id) || entry.trashed_at) return entry
    changed += 1
    return { ...entry, trashed_at: stamp }
  })
  write({ items, folders: data.folders })
  return changed
}

/** 从回收站还原 */
export async function restoreEntries(ids: string[]): Promise<number> {
  const data = read() ?? { items: [], folders: [] }
  let changed = 0
  const items = data.items.map((entry) => {
    if (!ids.includes(entry.id) || !entry.trashed_at) return entry
    changed += 1
    return { ...entry, trashed_at: null }
  })
  write({ items, folders: data.folders })
  return changed
}

/** 彻底删除（回收站里才允许） */
export async function deleteEntries(ids: string[]): Promise<number> {
  if (REMOTE) {
    const result = await del<{ deleted: number }>('/knowledge/entries', { query: { ids: ids.join(',') } })
    return result.deleted ?? 0
  }
  const data = read() ?? { items: [], folders: [] }
  const remain = data.items.filter((entry) => !ids.includes(entry.id))
  write({ items: remain, folders: data.folders })
  return data.items.length - remain.length
}

/** 清空回收站（只清回收站里那些） */
export async function emptyTrash(): Promise<number> {
  const data = read() ?? { items: [], folders: [] }
  const remain = data.items.filter((entry) => !entry.trashed_at)
  write({ items: remain, folders: data.folders })
  return data.items.length - remain.length
}

/** 移动到文件夹（含"移到根目录"：folder = []） */
export async function moveEntries(ids: string[], folder: string[]): Promise<number> {
  const data = read() ?? { items: [], folders: [] }
  const stamp = new Date().toISOString()
  let changed = 0
  const items = data.items.map((entry) => {
    if (!ids.includes(entry.id)) return entry
    if (folderPath(entry.folder) === folderPath(folder)) return entry
    changed += 1
    return { ...entry, folder: [...folder], moved_at: stamp }
  })
  write({ items, folders: ensureFolderPath(data.folders, folder) })
  return changed
}

export async function tagEntries(ids: string[], tag: string): Promise<number> {
  const clean = tag.trim()
  if (!clean) return 0
  const data = read() ?? { items: [], folders: [] }
  let changed = 0
  const items = data.items.map((entry) => {
    if (!ids.includes(entry.id) || entry.tags.includes(clean)) return entry
    changed += 1
    return { ...entry, tags: [...entry.tags, clean] }
  })
  write({ items, folders: data.folders })
  return changed
}

/** 在当前路径下新建文件夹（允许空文件夹；父目录一并登记） */
export async function createFolder(folder: string[]): Promise<string[]> {
  const data = read() ?? { items: [], folders: [] }
  const folders = ensureFolderPath(data.folders, folder)
  write({ items: data.items, folders })
  return folders
}

function ensureFolderPath(folders: string[], folder: string[]): string[] {
  const next = [...folders]
  for (let depth = 1; depth <= folder.length; depth += 1) {
    const path = folderPath(folder.slice(0, depth))
    if (path && !next.includes(path)) next.push(path)
  }
  return next.sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'))
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
    lines.push(`## ${entry.name}`, '')
    lines.push(
      `- 分类：${BUCKET_LABELS[entry.bucket]}`,
      `- 位置：知识库${entry.folder.length ? ` / ${entry.folder.join(' / ')}` : ''}`,
      `- 来源：${entry.source_label}`,
      `- 导入：${formatDateTime(entry.imported_at)}`,
    )
    if (entry.tags.length) lines.push(`- 标签：${entry.tags.join(' / ')}`)
    lines.push('')
    if (entry.content.trim()) lines.push(entry.content.trim(), '')
  }
  return lines.join('\n')
}
