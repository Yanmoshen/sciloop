<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 知识库（左栏「知识库」入口）—— 版面对齐夸克网盘（用户 2026-09-22 给的参考图）。
 *
 * 结构：左栏（分类 + 回收站，可拖拽调宽）｜ 内容区（面包屑 / 筛选 / 表头 / 表格 / 批量栏）
 * 行为：文件夹按真实路径浏览（可在当前路径新建，允许空文件夹）；点一行 → 内容区整块切成阅读器。
 *
 * 分类口径（用户指定顺序）：全部 → 最近 → 文献 → idea → 实验 → 论文 → 记忆 → 回收站。
 * 数据仍是**本机浏览器**（见 `api/knowledge.ts` 顶部契约），不写后端、不新增迁移。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  createFolder,
  deleteEntries,
  emptyTrash,
  exportJson,
  exportMarkdown,
  extOf,
  filterEntries,
  folderPath,
  formatLabelOf,
  formatOf,
  formatSize,
  formatTime,
  hasContent,
  KB_BUCKETS,
  loadSnapshot,
  moveEntries,
  restoreEntries,
  sortEntries,
  tagEntries,
  TIME_RANGES,
  trashEntries,
  type KnowledgeBucket,
  type KnowledgeEntry,
  type KnowledgeScope,
  type SortField,
  type SortOrder,
  type TimeRange,
} from '@/api/knowledge'
import KnowledgeEntryDialog from '@/components/knowledge/KnowledgeEntryDialog.vue'
import KnowledgeExtractDialog from '@/components/knowledge/KnowledgeExtractDialog.vue'
import KnowledgeReader from '@/components/knowledge/KnowledgeReader.vue'
import KnowledgeUploadDialog from '@/components/knowledge/KnowledgeUploadDialog.vue'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()
const route = useRoute()
const router = useRouter()

/* ---------------- 数据 ---------------- */
const items = ref<KnowledgeEntry[]>([])
const folders = ref<string[]>([])
const loading = ref(false)
const loadError = ref('')
const notice = ref('')

/* ---------------- 视图状态（**以 URL 为准**，刷新 / 前进后退都不丢）---------------- */
/**
 * ⚠️ 这一组原来全是页面内的 ref —— 选文件夹、切分类、搜过的词，**一刷新全没了**
 * （研究者 2026-09-22 实测到两次：先"打开文件"，再"选文件夹"）。
 * 现在统一挂到 URL 查询参数上：
 * - 读：从 `route.query` 取，缺省就是默认视图；
 * - 写：`router.replace` 更新 query（页面里其它 `xxx.value = …` 的写法不用改，走 setter）；
 * - **只有非默认值才写进 URL**，免得 `/knowledge` 后面挂一串没意义的参数。
 * 列表是前端过滤出来的（`visibleEntries`），所以同步 URL 不产生额外请求。
 */
function urlText(key: string): string | null {
  const raw = route.query[key]
  const value = Array.isArray(raw) ? raw[0] : raw
  return typeof value === 'string' && value ? value : null
}

function setKnowledgeQuery(patch: Record<string, string | number | null>): void {
  const query = { ...route.query }
  let changed = false
  for (const [key, value] of Object.entries(patch)) {
    const next = value === null || value === '' ? undefined : String(value)
    const current = typeof query[key] === 'string' ? query[key] : undefined
    if (next === current) continue
    changed = true
    if (next === undefined) delete query[key]
    else query[key] = next
  }
  if (!changed) return
  // replace 而不是 push：切滤镜不该在历史里堆一串，但刷新与前进后退都对得上
  void router.replace({ path: '/knowledge', query })
}

const scope = computed<KnowledgeScope>({
  get: () => (urlText('scope') as KnowledgeScope | null) ?? 'all',
  set: (value) => setKnowledgeQuery({ scope: value === 'all' ? null : value }),
})

/** 文件夹路径：URL 里用 `/` 连起来（`?folder=文献/综述`），直观也好读 */
const folder = computed<string[]>({
  get: () => (urlText('folder') ?? '').split('/').filter(Boolean),
  set: (path) => setKnowledgeQuery({ folder: path.length ? path.join('/') : null }),
})

const keyword = ref(urlText('q') ?? '')
const projectFilter = computed<number | null>({
  get: () => {
    const raw = urlText('project')
    const value = raw === null ? Number.NaN : Number(raw)
    return Number.isFinite(value) ? value : null
  },
  set: (value) => setKnowledgeQuery({ project: value ?? null }),
})
const timeRange = computed<TimeRange | null>({
  get: () => urlText('since') as TimeRange | null,
  set: (value) => setKnowledgeQuery({ since: value }),
})
const sortField = computed<SortField>({
  get: () => (urlText('sort') as SortField | null) ?? 'modified',
  set: (value) => setKnowledgeQuery({ sort: value === 'modified' ? null : value }),
})
const sortOrder = computed<SortOrder>({
  get: () => (urlText('order') as SortOrder | null) ?? 'desc',
  set: (value) => setKnowledgeQuery({ order: value === 'desc' ? null : value }),
})
const selected = ref<string[]>([])

/** 搜索词：打字频繁，**防抖 300ms** 再写回 URL（别每敲一个字就 replace 一次） */
let keywordTimer: ReturnType<typeof setTimeout> | null = null
watch(keyword, (value) => {
  if (keywordTimer) clearTimeout(keywordTimer)
  keywordTimer = setTimeout(() => setKnowledgeQuery({ q: value.trim() || null }), 300)
})
/**
 * 当前打开的文件 id —— **以 URL 为准**（`/knowledge?entry=<id>`）。
 *
 * ⚠️ 这里原来是普通 ref（页面内状态），打开文件不进 URL，于是**一刷新就回到列表**
 * （研究者 2026-09-22 实测到的）。改成可写 computed 后，读写都走 URL，
 * 页面里其它 `openEntryId.value = …` 的写法一个字都不用改。
 */
const openEntryId = computed<string | null>({
  get: () => {
    const value = route.query.entry
    return typeof value === 'string' && value ? value : null
  },
  set: (id) => {
    const current = typeof route.query.entry === 'string' ? route.query.entry : ''
    if (current === (id ?? '')) return
    const query = { ...route.query }
    if (id) query.entry = id
    else delete query.entry
    // replace 而不是 push：连着点几个文件不该在浏览器历史里堆一串，
    // 但**刷新与前进后退都能回到同一个文件**。
    void router.replace({ path: '/knowledge', query })
  },
})

/* ---------------- 弹窗与临时交互 ---------------- */
const uploadOpen = ref(false)
const extractOpen = ref(false)
const extractSource = ref<'paper' | 'idea'>('paper')
const entryDialogOpen = ref(false)
const editingEntry = ref<KnowledgeEntry | null>(null)
const newEntryBucket = ref<KnowledgeBucket>('literature')

const tagging = ref(false)
const tagInput = ref('')
const moveTarget = ref<{ ids: string[]; label: string } | null>(null)
const moveNewFolder = ref('')
const creatingFolder = ref(false)
const newFolderName = ref('')
/** 新建文件夹那一行的输入框（插进列表第一行后自动聚焦） */
const folderInput = ref<HTMLInputElement | null>(null)
const confirmState = ref<{ ids: string[]; label: string; mode: 'trash' | 'delete' } | null>(null)
const busy = ref(false)

/* ---------------- 左栏宽度（可拖拽，记住） ---------------- */
const NAV_WIDTH_KEY = 'sciloop.kb.navWidth'
const NAV_MIN = 160
const NAV_MAX = 360

function clampNav(value: number): number {
  if (!Number.isFinite(value)) return 200
  return Math.min(Math.max(Math.round(value), NAV_MIN), NAV_MAX)
}

const navWidth = ref(
  typeof localStorage === 'undefined' ? 200 : clampNav(Number(localStorage.getItem(NAV_WIDTH_KEY))),
)

function persistNav(): void {
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(NAV_WIDTH_KEY, String(navWidth.value))
  }
}

/** 键盘可达：聚焦分隔条后用左右方向键调宽 */
function nudgeNav(delta: number): void {
  navWidth.value = clampNav(navWidth.value + delta)
  persistNav()
}

function startResize(event: MouseEvent): void {
  event.preventDefault()
  const startX = event.clientX
  const startWidth = navWidth.value
  function onMove(moveEvent: MouseEvent): void {
    navWidth.value = clampNav(startWidth + (moveEvent.clientX - startX))
  }
  function onUp(): void {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    persistNav()
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

/* ---------------- 派生 ---------------- */
const projects = computed(() => session.projects.map((project) => ({ id: project.id, name: project.name })))
const activeItems = computed(() => items.value.filter((entry) => !entry.trashed_at))

const counts = computed<Record<string, number>>(() => {
  const base: Record<string, number> = { all: activeItems.value.length, recent: activeItems.value.length }
  for (const key of ['literature', 'idea', 'experiment', 'paper', 'memory'] as KnowledgeBucket[]) {
    base[key] = activeItems.value.filter((entry) => entry.bucket === key).length
  }
  base.trash = items.value.length - activeItems.value.length
  return base
})

/**
 * 只有「全部」是按文件夹浏览；其余分类（最近 / 文献 / idea / 实验 / 论文 / 记忆 / 回收站）
 * 都是**跨文件夹的扁平视图**（与网盘点"视频 / 图片"的用法一致）——
 * 否则会出现"点了实验、看到的是别的文件夹、点进去还空着"这种自相矛盾的画面。
 */
const isBrowsing = computed(() => scope.value === 'all')

const visibleEntries = computed(() =>
  sortEntries(
    filterEntries(items.value, {
      q: keyword.value,
      scope: scope.value,
      folder: isBrowsing.value ? folder.value : undefined,
      projectId: projectFilter.value,
      timeRange: timeRange.value,
    }),
    sortField.value,
    sortOrder.value,
  ),
)

/** 子文件夹（当前路径的直接下级）；扁平视图与回收站里都不列文件夹 */
const childFolders = computed(() => {
  if (!isBrowsing.value) return []
  const prefix = folderPath(folder.value)
  return folders.value
    .filter((path) => {
      if (!path) return false
      const parts = path.split('/')
      if (parts.length !== folder.value.length + 1) return false
      return folder.value.every((segment, index) => parts[index] === segment)
    })
    .filter((path) => !prefix || path.startsWith(`${prefix}/`))
})

const breadcrumb = computed(() => [
  { label: '知识库', path: [] as string[] },
  ...folder.value.map((segment, index) => ({
    label: segment,
    path: folder.value.slice(0, index + 1),
  })),
])

const openEntry = computed(() => items.value.find((entry) => entry.id === openEntryId.value) ?? null)
const selectedEntries = computed(() => items.value.filter((entry) => selected.value.includes(entry.id)))
const tagPool = computed(() => [...new Set(items.value.flatMap((entry) => entry.tags))].sort())
const hasSelection = computed(() => selected.value.length > 0)

/**
 * 文件类型徽标（原生 App 的图标语言）：18×18 圆角小方块 + 白字缩写。
 * 先按扩展名查表，查不到再按渲染方式兜底 —— 绝不显示空白方块。
 */
const BADGE_BY_EXT: Record<string, { label: string; tone: string }> = {
  pdf: { label: 'PDF', tone: 'pdf' },
  doc: { label: 'DOC', tone: 'doc' },
  docx: { label: 'DOC', tone: 'doc' },
  ppt: { label: 'PPT', tone: 'doc' },
  pptx: { label: 'PPT', tone: 'doc' },
  xls: { label: 'XLS', tone: 'sheet' },
  xlsx: { label: 'XLS', tone: 'sheet' },
  csv: { label: 'CSV', tone: 'sheet' },
  tsv: { label: 'TSV', tone: 'sheet' },
  md: { label: 'MD', tone: 'markdown' },
  markdown: { label: 'MD', tone: 'markdown' },
  txt: { label: 'TXT', tone: 'markdown' },
  log: { label: 'LOG', tone: 'markdown' },
  ipynb: { label: 'IPY', tone: 'code' },
  zip: { label: 'ZIP', tone: 'archive' },
  rar: { label: 'RAR', tone: 'archive' },
  '7z': { label: '7Z', tone: 'archive' },
  tar: { label: 'TAR', tone: 'archive' },
  gz: { label: 'GZ', tone: 'archive' },
}

function badgeOf(entry: KnowledgeEntry): { label: string; tone: string } {
  const known = BADGE_BY_EXT[extOf(entry.name)]
  if (known) return known
  const renderer = formatOf(entry.name, hasContent(entry)).renderer
  if (renderer === 'image') return { label: 'IMG', tone: 'sheet' }
  if (renderer === 'code') return { label: extOf(entry.name).slice(0, 3).toUpperCase() || 'TXT', tone: 'code' }
  if (renderer === 'text' || renderer === 'markdown') return { label: 'TXT', tone: 'markdown' }
  return { label: extOf(entry.name).slice(0, 3).toUpperCase() || '?', tone: 'archive' }
}

function folderOf(entry: KnowledgeEntry): string {
  return entry.folder.length ? `知识库 / ${entry.folder.join(' / ')}` : '知识库'
}

/* ---------------- 读写 ---------------- */
async function reload(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const snapshot = await loadSnapshot()
    items.value = snapshot.items
    folders.value = snapshot.folders
  } catch (error) {
    items.value = []
    folders.value = []
    loadError.value = (error as { message?: string }).message ?? '知识库读取失败'
  } finally {
    loading.value = false
  }
}

function selectScope(next: KnowledgeScope): void {
  scope.value = next
  selected.value = []
  folder.value = []
  if (next === 'recent') {
    sortField.value = 'modified'
    sortOrder.value = 'desc'
  }
}

function enterFolder(path: string[]): void {
  folder.value = path
  selected.value = []
}

function toggleSort(field: SortField): void {
  if (sortField.value === field) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
    return
  }
  sortField.value = field
  sortOrder.value = field === 'name' ? 'asc' : 'desc'
}

function toggleSelect(id: string): void {
  selected.value = selected.value.includes(id) ? selected.value.filter((value) => value !== id) : [...selected.value, id]
}

function toggleSelectAll(): void {
  const ids = visibleEntries.value.map((entry) => entry.id)
  const allSelected = ids.length > 0 && ids.every((id) => selected.value.includes(id))
  selected.value = allSelected ? [] : ids
}

function openReader(id: string): void {
  openEntryId.value = id
}

function editEntry(entry: KnowledgeEntry): void {
  editingEntry.value = entry
  entryDialogOpen.value = true
}

function createInBucket(bucket: KnowledgeBucket): void {
  editingEntry.value = null
  newEntryBucket.value = bucket
  entryDialogOpen.value = true
}

function onAddCommand(command: string): void {
  if (command === 'upload') {
    uploadOpen.value = true
    return
  }
  if (command === 'folder') {
    creatingFolder.value = true
    newFolderName.value = ''
    void nextTick(() => folderInput.value?.focus())
    return
  }
  if (command === 'paper' || command === 'idea') {
    extractSource.value = command
    extractOpen.value = true
    return
  }
  const bucket = command as KnowledgeBucket
  createInBucket(bucket)
}

function mergeEntries(added: KnowledgeEntry[]): void {
  items.value = [...added, ...items.value]
  const paths = new Set(folders.value)
  for (const entry of added) {
    for (let depth = 1; depth <= entry.folder.length; depth += 1) {
      paths.add(entry.folder.slice(0, depth).join('/'))
    }
  }
  folders.value = [...paths].sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'))
}

async function submitFolder(): Promise<void> {
  const name = newFolderName.value.trim()
  if (!name) {
    creatingFolder.value = false
    return
  }
  folders.value = await createFolder([...folder.value, name])
  creatingFolder.value = false
  newFolderName.value = ''
  notice.value = `已新建文件夹「${name}」`
}

function cancelFolder(): void {
  creatingFolder.value = false
  newFolderName.value = ''
}

/** 「在新页面打开」= 打开**独立整页阅读器**（不带壳层，整屏只放内容），不是原始文件 */
function openInNewTab(): void {
  const entry = openEntry.value
  if (!entry) return
  const href = router.resolve({ name: 'knowledge-read', params: { entryId: entry.id } }).href
  window.open(href, '_blank', 'noopener')
}

function exportScope(): KnowledgeEntry[] {
  return hasSelection.value ? selectedEntries.value : visibleEntries.value
}

function download(filename: string, content: string, mime: string): void {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

function onExportCommand(command: string): void {
  const scopeItems = exportScope()
  if (!scopeItems.length) return
  const stamp = new Date().toISOString().slice(0, 10)
  if (command === 'json') {
    download(`sciloop-knowledge-${stamp}.json`, exportJson(scopeItems), 'application/json')
  } else {
    download(`sciloop-knowledge-${stamp}.md`, exportMarkdown(scopeItems), 'text/markdown')
  }
  notice.value = `已导出 ${scopeItems.length} 条`
}

function startTagging(): void {
  tagging.value = true
  tagInput.value = ''
}

async function applyTag(): Promise<void> {
  const tag = tagInput.value.trim()
  if (!tag) {
    tagging.value = false
    return
  }
  busy.value = true
  try {
    const changed = await tagEntries(selected.value, tag)
    await reload()
    tagging.value = false
    notice.value = `已为 ${changed} 条加上标签「${tag}」`
  } finally {
    busy.value = false
  }
}

function askMove(entries: KnowledgeEntry[], label: string): void {
  moveTarget.value = { ids: entries.map((entry) => entry.id), label }
  moveNewFolder.value = ''
}

async function doMove(target: string[]): Promise<void> {
  const pending = moveTarget.value
  if (!pending) return
  busy.value = true
  try {
    const moved = await moveEntries(pending.ids, target)
    await reload()
    selected.value = []
    moveTarget.value = null
    notice.value = moved ? `已移动 ${moved} 条` : '目标位置与当前位置相同'
  } finally {
    busy.value = false
  }
}

async function doMoveNew(): Promise<void> {
  const name = moveNewFolder.value.trim()
  const pending = moveTarget.value
  if (!name || !pending) return
  folders.value = await createFolder([...folder.value, name])
  await doMove([...folder.value, name])
}

async function askTrash(entries: KnowledgeEntry[], label: string): Promise<void> {
  confirmState.value = { ids: entries.map((entry) => entry.id), label, mode: 'trash' }
}

async function askDeleteForever(entries: KnowledgeEntry[], label: string): Promise<void> {
  confirmState.value = { ids: entries.map((entry) => entry.id), label, mode: 'delete' }
}

async function confirmDestructive(): Promise<void> {
  const pending = confirmState.value
  if (!pending) return
  busy.value = true
  try {
    const changed =
      pending.mode === 'trash' ? await trashEntries(pending.ids) : await deleteEntries(pending.ids)
    await reload()
    selected.value = selected.value.filter((id) => !pending.ids.includes(id))
    if (openEntryId.value && pending.ids.includes(openEntryId.value)) openEntryId.value = null
    confirmState.value = null
    notice.value = pending.mode === 'trash' ? `已移入回收站 ${changed} 条` : `已彻底删除 ${changed} 条`
  } finally {
    busy.value = false
  }
}

async function doRestore(entries: KnowledgeEntry[]): Promise<void> {
  const changed = await restoreEntries(entries.map((entry) => entry.id))
  await reload()
  selected.value = []
  notice.value = `已还原 ${changed} 条`
}

async function doEmptyTrash(): Promise<void> {
  const changed = await emptyTrash()
  await reload()
  selected.value = []
  notice.value = `已清空回收站（${changed} 条）`
}

function onRowCommand(command: string, entry: KnowledgeEntry): void {
  if (command === 'edit') editEntry(entry)
  if (command === 'tag') {
    selected.value = [entry.id]
    startTagging()
  }
  if (command === 'move') askMove([entry], entry.name)
  if (command === 'trash') void askTrash([entry], entry.name)
  if (command === 'restore') void doRestore([entry])
  if (command === 'delete') void askDeleteForever([entry], entry.name)
}

onMounted(async () => {
  await reload()
  if (session.currentProjectId === null) await session.loadProjects()
})
</script>

<template>
  <section class="kb">
    <!-- 左栏：分类 + 回收站（可拖拽调宽） -->
    <nav class="kb__nav" :style="{ width: `${navWidth}px` }" aria-label="知识库分类">
      <button
        v-for="bucket in KB_BUCKETS"
        :key="bucket.key"
        class="kb__nav-item"
        :class="{ 'kb__nav-item--on': scope === bucket.key }"
        type="button"
        @click="selectScope(bucket.key)"
      >
        <span>{{ bucket.label }}</span>
        <span class="kb__nav-count">{{ counts[bucket.key] ?? 0 }}</span>
      </button>
    </nav>
    <div
      class="kb__resizer"
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整左栏宽度"
      tabindex="0"
      @mousedown="startResize"
      @keydown.left.prevent="nudgeNav(-16)"
      @keydown.right.prevent="nudgeNav(16)"
    />

    <!-- 内容区 -->
    <div class="kb__content">
      <KnowledgeReader v-if="openEntry" :entry="openEntry" @back="openEntryId = null" @open-standalone="openInNewTab" />

      <template v-else>
        <!-- 第 1 行：批量栏（有选中时替换动作行） -->
        <div v-if="hasSelection" class="kb__actions kb__actions--batch">
          <span class="kb__count">已选 {{ selected.length }} 条</span>
          <template v-if="tagging">
            <el-input
              v-model="tagInput"
              size="small"
              class="kb__tag-input"
              placeholder="输入标签后回车"
              @keyup.enter="applyTag"
            />
            <button class="kb__btn kb__btn--primary" type="button" :disabled="busy" @click="applyTag">应用</button>
            <button class="kb__btn" type="button" @click="tagging = false">取消</button>
          </template>
          <template v-else-if="scope === 'trash'">
            <button class="kb__btn" type="button" :disabled="busy" @click="doRestore(selectedEntries)">还原</button>
            <button class="kb__btn kb__btn--danger" type="button" @click="askDeleteForever(selectedEntries, `选中的 ${selected.length} 条`)">
              彻底删除
            </button>
          </template>
          <template v-else>
            <button class="kb__btn" type="button" @click="startTagging">打标签</button>
            <button class="kb__btn" type="button" @click="askMove(selectedEntries, `选中的 ${selected.length} 条`)">
              移到文件夹
            </button>
            <button class="kb__btn" type="button" @click="askTrash(selectedEntries, `选中的 ${selected.length} 条`)">
              删除
            </button>
            <button class="kb__btn" type="button" @click="onExportCommand('md')">导出选中</button>
          </template>
          <span class="kb__spacer" />
          <button class="kb__btn" type="button" @click="selected = []">取消选择</button>
        </div>

        <!-- 第 1 行：面包屑 + 操作 -->
        <div v-else class="kb__actions">
          <nav class="kb__crumbs" aria-label="当前位置">
            <template v-for="(crumb, index) in breadcrumb" :key="crumb.label">
              <button class="kb__crumb" type="button" @click="enterFolder(crumb.path)">{{ crumb.label }}</button>
              <span v-if="index < breadcrumb.length - 1" class="kb__crumb-sep">/</span>
            </template>
          </nav>
          <span class="kb__spacer" />
          <el-dropdown trigger="click" @command="onExportCommand">
            <button class="kb__btn" type="button">导出</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="json">导出 JSON</el-dropdown-item>
                <el-dropdown-item command="md">导出 Markdown</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <button class="kb__btn" type="button" @click="onAddCommand('folder')">新建文件夹</button>
          <el-dropdown trigger="click" @command="onAddCommand">
            <button class="kb__btn kb__btn--primary" type="button">＋ 上传</button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="upload">上传文件</el-dropdown-item>
                <el-dropdown-item command="paper" divided>从论文库提取</el-dropdown-item>
                <el-dropdown-item command="idea">从研究构思提取</el-dropdown-item>
                <el-dropdown-item command="experiment" divided>新建实验记录</el-dropdown-item>
                <el-dropdown-item command="paper" divided>新建论文草稿</el-dropdown-item>
                <el-dropdown-item command="memory">新建记忆</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <!-- 第 2 行：搜索 + 筛选 -->
        <div class="kb__filters">
          <el-input v-model="keyword" size="small" class="kb__search" placeholder="搜索全部条目（跨文件夹）" clearable />
          <el-select v-model="projectFilter" size="small" class="kb__select" clearable placeholder="全部项目">
            <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
          </el-select>
          <el-select v-model="timeRange" size="small" class="kb__select" clearable placeholder="全部时间">
            <el-option v-for="range in TIME_RANGES" :key="range.key" :label="range.label" :value="range.key" />
          </el-select>
          <span v-if="notice" class="kb__notice">{{ notice }}</span>
        </div>

        <!-- 第 3 行起：表头 + 表格 -->
        <div class="kb__table">
          <div class="kb__row kb__row--head">
            <span class="kb__cell kb__cell--check">
              <input
                type="checkbox"
                class="kb__check"
                aria-label="全选当前列表"
                :checked="visibleEntries.length > 0 && visibleEntries.every((entry) => selected.includes(entry.id))"
                @change="toggleSelectAll"
              />
            </span>
            <span class="kb__cell kb__cell--name">
              <button class="kb__sort" type="button" @click="toggleSort('name')">
                名称<span v-if="sortField === 'name'" class="kb__sort-mark">{{ sortOrder === 'asc' ? '↑' : '↓' }}</span>
              </button>
            </span>
            <span class="kb__cell kb__cell--format">文件类型</span>
            <span class="kb__cell kb__cell--source">来源</span>
            <span class="kb__cell kb__cell--size">
              <button class="kb__sort" type="button" @click="toggleSort('size')">
                大小<span v-if="sortField === 'size'" class="kb__sort-mark">{{ sortOrder === 'asc' ? '↑' : '↓' }}</span>
              </button>
            </span>
            <span class="kb__cell kb__cell--time">
              <button class="kb__sort" type="button" @click="toggleSort('modified')">
                修改时间<span v-if="sortField === 'modified'" class="kb__sort-mark">{{ sortOrder === 'asc' ? '↑' : '↓' }}</span>
              </button>
            </span>
            <span class="kb__cell kb__cell--act" />
          </div>

          <el-skeleton v-if="loading" :rows="6" animated />

          <el-empty v-else-if="loadError" :description="loadError">
            <el-button size="small" @click="reload">重新读取</el-button>
          </el-empty>

          <div v-else class="kb__rows scroll-y">
            <!-- 新建文件夹：**直接插在当前路径的第一行**，就地输入名字（回车/失焦确认，Esc 取消） -->
            <div v-if="creatingFolder" class="kb__row kb__row--folder kb__row--new">
              <span class="kb__cell kb__cell--check" />
              <span class="kb__cell kb__cell--name">
                <span class="kb__badge kb__badge--folder" aria-hidden="true"><svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M1.6 3.3c0-.6.4-1 1-1h1.7c.3 0 .5.1.7.3l.5.5c.2.2.4.3.7.3h2.5c.6 0 1 .4 1 1v3.4c0 .6-.4 1-1 1H2.6c-.6 0-1-.4-1-1V3.3Z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg></span>
                <input
                  ref="folderInput"
                  v-model="newFolderName"
                  class="kb__name-input"
                  type="text"
                  placeholder="新文件夹"
                  aria-label="新文件夹名称"
                  @keydown.enter="submitFolder"
                  @keydown.esc="cancelFolder"
                  @blur="submitFolder"
                />
              </span>
              <span class="kb__cell kb__cell--format">文件夹</span>
              <span class="kb__cell kb__cell--source">—</span>
              <span class="kb__cell kb__cell--size">—</span>
              <span class="kb__cell kb__cell--time">—</span>
              <span class="kb__cell kb__cell--act" />
            </div>

            <!-- 子文件夹 -->
            <div
              v-for="child in childFolders"
              :key="child"
              class="kb__row kb__row--folder"
              role="button"
              tabindex="0"
              @click="enterFolder(child.split('/'))"
              @keyup.enter="enterFolder(child.split('/'))"
            >
              <span class="kb__cell kb__cell--check" />
              <span class="kb__cell kb__cell--name">
                <span class="kb__badge kb__badge--folder" aria-hidden="true"><svg width="11" height="11" viewBox="0 0 12 12" fill="none"><path d="M1.6 3.3c0-.6.4-1 1-1h1.7c.3 0 .5.1.7.3l.5.5c.2.2.4.3.7.3h2.5c.6 0 1 .4 1 1v3.4c0 .6-.4 1-1 1H2.6c-.6 0-1-.4-1-1V3.3Z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/></svg></span>
                <span class="kb__name">{{ child.split('/').pop() }}</span>
              </span>
              <span class="kb__cell kb__cell--format">文件夹</span>
              <span class="kb__cell kb__cell--source">—</span>
              <span class="kb__cell kb__cell--size">—</span>
              <span class="kb__cell kb__cell--time">—</span>
              <span class="kb__cell kb__cell--act" />
            </div>

            <!-- 条目 -->
            <div
              v-for="entry in visibleEntries"
              :key="entry.id"
              class="kb__row"
              :class="{ 'kb__row--on': selected.includes(entry.id) }"
            >
              <span class="kb__cell kb__cell--check">
                <input
                  type="checkbox"
                  class="kb__check"
                  :aria-label="`选择 ${entry.name}`"
                  :checked="selected.includes(entry.id)"
                  @change="toggleSelect(entry.id)"
                />
              </span>
              <span class="kb__cell kb__cell--name">
                <span
                  class="kb__badge"
                  :class="`kb__badge--${badgeOf(entry).tone}`"
                  aria-hidden="true"
                >{{ badgeOf(entry).label }}</span>
                <button class="kb__name kb__name--link" type="button" @click="openReader(entry.id)">
                  {{ entry.name }}
                </button>
              </span>
              <span class="kb__cell kb__cell--format">{{ formatLabelOf(entry.name, hasContent(entry)) }}</span>
              <span class="kb__cell kb__cell--source">
                <RouterLink v-if="entry.source_route" class="kb__link" :to="entry.source_route">
                  {{ entry.source_label }}
                </RouterLink>
                <template v-else>{{ entry.source_label }}</template>
              </span>
              <span class="kb__cell kb__cell--size">{{ formatSize(entry.size) }}</span>
              <span class="kb__cell kb__cell--time" :title="folderOf(entry)">{{ formatTime(entry.modified_at) }}</span>
              <span class="kb__cell kb__cell--act">
                <el-dropdown trigger="click" @command="(command: string) => onRowCommand(command, entry)">
                  <button class="kb__more" type="button" :aria-label="`${entry.name} 的操作`">⋯</button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <template v-if="scope === 'trash'">
                        <el-dropdown-item command="restore">还原</el-dropdown-item>
                        <el-dropdown-item command="delete" divided>彻底删除</el-dropdown-item>
                      </template>
                      <template v-else>
                        <el-dropdown-item command="edit">编辑</el-dropdown-item>
                        <el-dropdown-item command="tag">打标签</el-dropdown-item>
                        <el-dropdown-item command="move">移到文件夹</el-dropdown-item>
                        <el-dropdown-item command="trash" divided>删除</el-dropdown-item>
                      </template>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </span>
            </div>

            <el-empty
              v-if="!visibleEntries.length && !childFolders.length && !creatingFolder"
              :description="scope === 'trash' ? '回收站是空的' : '这里还没有内容，从右上角「＋ 上传」开始'"
            />
          </div>
        </div>

        <!-- 底部：回收站操作 / 危险操作确认 -->
        <div v-if="scope === 'trash' && counts.trash" class="kb__inline kb__inline--footer">
          <span class="kb__count">回收站里还有 {{ counts.trash }} 条</span>
          <button class="kb__btn kb__btn--danger" type="button" :disabled="busy" @click="doEmptyTrash">清空回收站</button>
        </div>

        <div v-if="confirmState" class="kb__confirm">
          <span>
            将{{ confirmState.mode === 'trash' ? '把' : '彻底删除' }}「{{ confirmState.label }}」{{
              confirmState.mode === 'trash' ? '移入回收站' : ''
            }}<template v-if="confirmState.mode === 'delete'">，不可恢复</template>。
          </span>
          <button
            class="kb__btn"
            :class="confirmState.mode === 'trash' ? 'kb__btn--primary' : 'kb__btn--danger'"
            type="button"
            :disabled="busy"
            @click="confirmDestructive"
          >
            确认
          </button>
          <button class="kb__btn" type="button" @click="confirmState = null">取消</button>
        </div>
      </template>
    </div>

    <!-- 移到文件夹 -->
    <Teleport to="body">
      <div v-if="moveTarget" class="kb__overlay" @click.self="moveTarget = null">
        <section class="kb__dialog" role="dialog" aria-modal="true" aria-label="移到文件夹">
          <h2 class="kb__dialog-title">移到文件夹</h2>
          <p class="kb__count">{{ moveTarget.label }}</p>
          <div class="kb__dialog-body scroll-y">
            <button class="kb__dest" type="button" @click="doMove([])">知识库（根目录）</button>
            <button v-for="path in folders" :key="path" class="kb__dest" type="button" @click="doMove(path.split('/'))">
              {{ path }}
            </button>
          </div>
          <div class="kb__dialog-foot">
            <el-input v-model="moveNewFolder" size="small" placeholder="新建文件夹并移入" @keyup.enter="doMoveNew" />
            <button class="kb__btn" type="button" :disabled="!moveNewFolder.trim()" @click="doMoveNew">新建并移入</button>
            <button class="kb__btn" type="button" @click="moveTarget = null">取消</button>
          </div>
        </section>
      </div>
    </Teleport>

    <KnowledgeUploadDialog
      v-model="uploadOpen"
      :tag-pool="tagPool"
      :folders="folders"
      :projects="projects"
      :current-folder="folder"
      :can-write="true"
      @added="mergeEntries"
    />
    <KnowledgeExtractDialog
      v-model="extractOpen"
      :source="extractSource"
      :tag-pool="tagPool"
      :projects="projects"
      :current-folder="folder"
      :can-write="true"
      @added="(entry: KnowledgeEntry) => mergeEntries([entry])"
    />
    <KnowledgeEntryDialog
      v-model="entryDialogOpen"
      :entry="editingEntry"
      :default-bucket="newEntryBucket"
      :tag-pool="tagPool"
      :folders="folders"
      :projects="projects"
      :current-folder="folder"
      :can-write="true"
      @saved="(entry: KnowledgeEntry) => mergeEntries([entry])"
    />
  </section>
</template>

<style scoped>
.kb {
  display: flex;
  gap: var(--space-2);
  flex: 1;
  min-height: 0;
  /* 风格 03（原生 macOS）：整个内容区是一块浅灰画布，面板是浮在上面的白卡。
     负外边距抵掉壳层内边距，让灰色铺满 —— 白卡才有"贴上去"的层次感 */
  padding: var(--space-4);
  margin: calc(var(--space-5) * -1);
  background-color: var(--color-bg-muted);
}

.kb__nav {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-2);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}

.kb__nav-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: 6px var(--space-2);
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition:
    background-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease);
}

.kb__nav-item:hover {
  background-color: var(--color-bg-subtle);
  color: var(--color-text-primary);
}

.kb__nav-item:active {
  transform: scale(var(--motion-press));
}

.kb__nav-item:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__nav-item--on,
.kb__nav-item--on:hover {
  background-color: var(--color-brand);
  color: var(--color-text-inverse);
  font-weight: 600;
}

.kb__nav-count {
  color: var(--color-text-disabled);
  font-family: var(--font-family-mono);
  font-size: var(--font-size-xs);
}

.kb__nav-item--on .kb__nav-count {
  color: var(--color-text-inverse);
  opacity: 0.85;
}

.kb__resizer {
  position: relative;
  flex: none;
  width: 9px;
  /* 正好压在 nav 与内容区之间的那道 8px 缝上（左右各吃回 8px），拖拽更好瞄 */
  margin: 0 calc(var(--space-2) * -1);
  cursor: col-resize;
}

/* 分隔线：常态是一条浅线，悬停变品牌色并露出中间的抓手 */
.kb__resizer::before {
  content: '';
  position: absolute;
  left: 50%;
  top: var(--space-1);
  bottom: var(--space-1);
  width: 1px;
  transform: translateX(-50%);
  background-color: var(--color-border-strong);
  transition: background-color var(--motion-dur-fast) var(--motion-ease);
}

.kb__resizer::after {
  content: '';
  position: absolute;
  left: 50%;
  top: 50%;
  width: 3px;
  height: 26px;
  transform: translate(-50%, -50%);
  border-radius: var(--radius-pill);
  background-color: var(--color-border-strong);
  opacity: 0;
  transition: opacity var(--motion-dur-fast) var(--motion-ease);
}

.kb__resizer:hover::before,
.kb__resizer:focus-visible::before {
  background-color: var(--color-brand);
}

.kb__resizer:hover::after {
  opacity: 1;
}

.kb__resizer:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__content {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.kb__actions,
.kb__filters,
.kb__inline,
.kb__confirm {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
}

.kb__actions--batch {
  background-color: var(--color-brand-soft);
}

.kb__crumbs {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  min-width: 0;
}

.kb__crumb {
  padding: 2px var(--space-1);
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-sm);
  cursor: pointer;
  transition: color var(--motion-dur-fast) var(--motion-ease);
}

/* 当前位置（面包屑最后一节）是黑字加粗，其余是灰的可点链接 */
.kb__crumbs .kb__crumb:last-child {
  color: var(--color-text-primary);
  font-weight: 600;
}

.kb__crumb:hover {
  color: var(--color-brand);
}

.kb__crumb:active {
  transform: scale(var(--motion-press));
}

.kb__crumb:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__crumb-sep {
  color: var(--color-text-disabled);
}

.kb__spacer {
  flex: 1;
}

.kb__count,
.kb__notice {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kb__notice {
  color: var(--color-success);
}

.kb__search {
  flex: 1;
  min-width: 200px;
}

.kb__select {
  width: 150px;
}

.kb__tag-input {
  width: 220px;
}

.kb__btn {
  height: 26px;
  padding: 0 var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  background-color: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-xs);
  cursor: pointer;
  box-shadow: var(--shadow-card);
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease),
    background-color var(--motion-dur-fast) var(--motion-ease);
}

.kb__btn:hover {
  border-color: var(--color-border-strong);
  background-color: var(--color-bg-subtle);
  color: var(--color-text-primary);
}

.kb__btn:active {
  transform: scale(var(--motion-press));
}

.kb__btn:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__btn--primary {
  border-color: var(--color-brand);
  background-color: var(--color-brand);
  color: var(--color-text-inverse);
}

.kb__btn--primary:hover {
  border-color: var(--color-brand-hover);
  background-color: var(--color-brand-hover);
  color: var(--color-text-inverse);
}

.kb__btn--danger {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.kb__btn--danger:hover {
  background-color: var(--color-danger);
  color: var(--color-text-inverse);
}

.kb__table {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-radius: var(--radius-lg);
  background-color: var(--color-card-bg);
  box-shadow: var(--shadow-card);
  overflow: hidden;
}

.kb__row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 7px var(--space-3);
  border-bottom: 1px solid var(--color-border);
  transition: background-color var(--motion-dur-fast) var(--motion-ease);
}

.kb__row:hover {
  background-color: var(--color-bg-subtle);
}

.kb__row--on {
  background-color: var(--color-brand-soft);
}

.kb__row--head {
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  border-bottom-color: var(--color-border-strong);
}

.kb__row--head:hover {
  background-color: transparent;
}

.kb__row--folder {
  cursor: pointer;
}

/* 新建文件夹那一行：和普通行同样高，只是名字处换成输入框 */
.kb__row--new {
  background-color: var(--color-brand-soft);
  cursor: default;
}

.kb__name-input {
  flex: 1;
  min-width: 0;
  height: 22px;
  padding: 0 var(--space-2);
  border: 1px solid var(--color-brand);
  border-radius: var(--radius-sm);
  background-color: var(--color-card-bg);
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
}

.kb__name-input:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 1px;
}

.kb__name-input::placeholder {
  color: var(--color-text-disabled);
}

.kb__rows {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.kb__cell {
  flex: none;
  min-width: 0;
  font-size: var(--font-size-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb__cell--check {
  width: 16px;
}

.kb__cell--name {
  flex: 1;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.kb__cell--format {
  width: 96px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kb__cell--source {
  width: 132px;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
}

.kb__cell--size {
  width: 72px;
  text-align: right;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-family: var(--font-family-mono);
}

.kb__cell--time {
  width: 108px;
  text-align: right;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-family: var(--font-family-mono);
}

.kb__cell--act {
  width: 30px;
  display: flex;
  justify-content: flex-end;
}

.kb__check {
  width: 13px;
  height: 13px;
  margin: 0;
  accent-color: var(--color-brand);
  cursor: pointer;
}

/* 文件类型徽标：18×18 圆角小方块 + 白字缩写（原生 App 的文件类型图标语言） */
.kb__badge {
  flex: none;
  width: 18px;
  height: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background-color: var(--color-text-disabled);
  color: var(--color-text-inverse);
  font-family: var(--font-family-mono);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: -0.3px;
}

.kb__badge--folder {
  background-color: var(--color-brand);
}

.kb__badge--pdf {
  background-color: var(--color-danger);
}

.kb__badge--sheet {
  background-color: var(--color-success);
}

.kb__badge--doc {
  background-color: var(--color-info);
}

.kb__badge--code {
  background-color: var(--color-warning);
}

.kb__badge--markdown {
  background-color: var(--color-text-secondary);
}

.kb__badge--archive {
  background-color: var(--color-text-disabled);
}

.kb__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb__name--link {
  padding: 0;
  border: none;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: color var(--motion-dur-fast) var(--motion-ease);
}

.kb__name--link:hover {
  color: var(--color-brand);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.kb__name--link:active {
  filter: brightness(0.95);
}

.kb__name--link:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__link {
  color: var(--color-brand);
}

.kb__link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
}

.kb__sort {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 0;
  border: none;
  background: none;
  color: inherit;
  font: inherit;
  cursor: pointer;
  transition: color var(--motion-dur-fast) var(--motion-ease);
}

.kb__sort:hover {
  color: var(--color-brand);
}

.kb__sort:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__sort-mark {
  color: var(--color-brand);
}

.kb__more {
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--font-size-md);
  line-height: 1;
  cursor: pointer;
  transition:
    border-color var(--motion-dur-fast) var(--motion-ease),
    color var(--motion-dur-fast) var(--motion-ease);
}

.kb__more:hover {
  border-color: var(--color-border-strong);
  color: var(--color-text-primary);
}

.kb__more:active {
  transform: scale(var(--motion-press));
}

.kb__more:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__inline--footer {
  justify-content: flex-start;
}

.kb__confirm {
  border-color: var(--color-danger);
  background-color: var(--color-danger-soft);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
}

.kb__overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-5);
  background: rgba(0, 0, 0, 0.35); /* ui-polish-allow: 遮罩色与主题解耦 */
  backdrop-filter: blur(8px) saturate(120%);
  -webkit-backdrop-filter: blur(8px) saturate(120%);
}

:global(:root[data-theme='dark']) .kb__overlay {
  background: rgba(0, 0, 0, 0.55); /* ui-polish-allow: 遮罩色与主题解耦 */
}

.kb__dialog {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: min(420px, 100%);
  max-height: min(560px, 100%);
  padding: var(--space-4);
  background: var(--color-card-bg);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-popover);
  animation: dialog-pop var(--motion-dur) var(--motion-ease-out);
}

.kb__dialog-title {
  margin: 0;
  font-size: var(--font-size-lg);
}

.kb__dialog-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.kb__dest {
  padding: var(--space-2);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-primary);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition: background-color var(--motion-dur-fast) var(--motion-ease);
}

.kb__dest:hover {
  background-color: var(--color-bg-subtle);
}

.kb__dest:active {
  transform: scale(var(--motion-press));
}

.kb__dest:focus-visible {
  outline: 2px solid var(--color-brand);
  outline-offset: 2px;
}

.kb__dialog-foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
}

@media (max-width: 900px) {
  .kb {
    flex-direction: column;
  }

  .kb__nav {
    flex-direction: row;
    flex-wrap: wrap;
    width: auto !important;
  }

  .kb__resizer {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .kb__nav-item,
  .kb__crumb,
  .kb__btn,
  .kb__row,
  .kb__name--link,
  .kb__sort,
  .kb__more,
  .kb__resizer,
  .kb__dest {
    transition: none;
  }
}
</style>
