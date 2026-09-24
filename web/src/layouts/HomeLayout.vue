<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 总览壳层（双壳层的第一层）：左栏（品牌 / 固定区 / 独立滚动区）
 * + 顶部（搜索 / 颜色切换 / 圆形头像悬浮菜单）+ 总览内容。
 *
 * 左栏分两块（2026-09-20 重构）：
 * - **固定区**：开始使用 / 文献调研 / 知识库（吸在左上角，永不滚动）
 * - 分块线之后是**独立滚动区**：研究构想 / 流水线工作台 / 未分组 / 项目 / 已归档
 *   只有这一块自己滚，整页不滚（沿用 `.sl-home{height:100vh;overflow:hidden}` 的口径）。
 *
 * 「未分组 / 项目」取代了原来的「最近打开」：项目 ↔ 对话 = 1:N，对话可以未分组；
 * 点项目行**展开**它的对话（不再直接进工作台——工作台入口在对话页里）。
 *
 * 设计语言：styles/home-theme.css（n8n design language，仅作用于 `.sl-home`）。
 * 主题沿用 session store 的 `sciloop.theme`（localStorage 持久化），此处不再自造一份。
 * 进入项目后由 ShellLayout（模块壳层）接管，7 个模块页保持原样。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { writeDenied } from '@/utils/messages'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { setConversationArchived, type ConversationBrief } from '@/api/conversations'
import type { CreatedProject } from '@/api/projects'
import { setProjectArchived } from '@/api/projects'
import ConfirmDialog from '@/components/home/ConfirmDialog.vue'
import SciLoopMark from '@/components/SciLoopMark.vue'
import ConversationRenameDialog from '@/components/ConversationRenameDialog.vue'
import MoveConversationDialog from '@/components/home/MoveConversationDialog.vue'
import ProjectNameDialog from '@/components/home/ProjectNameDialog.vue'
import ProjectRenameDialog from '@/components/ProjectRenameDialog.vue'
import TaskHistoryDialog from '@/components/TaskHistoryDialog.vue'
import TaskMonitorDialog from '@/components/TaskMonitorDialog.vue'
import { useConversationStore } from '@/stores/conversations'
import { useSessionStore } from '@/stores/session'
import { useTaskStore } from '@/stores/tasks'
import { usePipelineDrawerStore } from '@/stores/pipelineDrawer'
import { requestEntrance } from '@/utils/pageEntrance'
import { orderProjectsForRail } from '@/utils/projectOrder'

const session = useSessionStore()
const conversations = useConversationStore()
const route = useRoute()
const router = useRouter()

/** 左栏主入口：开始使用 / 文献调研（可展开分组）/ 知识库 */
interface HomeNavChild {
  key: string
  label: string
  path: string
}
interface HomeNavItem {
  key: string
  label: string
  path: string
  children?: HomeNavChild[]
}
const HOME_NAV: HomeNavItem[] = [
  { key: 'home', label: '开始使用', path: '/' },
  {
    key: 'literature',
    label: '文献调研',
    path: '/papers',
    // 点「文献调研」在其下方展开这几项；子页高亮由 route.meta.module 决定
    // （「论文导入」2026-09-20 起并入文献总览弹窗，已从导航除名）
    children: [
      { key: 'papers', label: '文献总览', path: '/papers' },
      { key: 'feed', label: '论文库', path: '/papers/feed' },
      { key: 'translate', label: '论文翻译', path: '/papers/translate' },
      { key: 'reader', label: '全文阅读', path: '/papers/reader' },
      { key: 'parse', label: '论文解析', path: '/papers/parse' },
      { key: 'aggregate', label: '聚合对比', path: '/papers/aggregate/demo' },
      { key: 'export', label: '多格式导出', path: '/papers/export' },
    ],
  },
  { key: 'knowledge', label: '知识库', path: '/knowledge' },
]

/** 左栏二级入口（不在分组里的功能页）；设置由右上角头像的悬浮菜单进入 */
const MODULE_NAV = [
  { key: 'ideas', label: '研究构想', path: '/ideas' },
  { key: 'workbench', label: '流水线工作台', path: '/workbench/demo' },
]

const keyword = ref('')

/**
 * 左栏折叠。
 *
 * 折叠后左栏整体滑出（用 `margin-left` 位移，而不是把宽度压到 0）：
 * 压宽度会让栏内文字跟着挤压换行，看着像"被揉皱"；位移则是一整块平滑滑走。
 * 折叠按钮有两个落点，见模板：展开时在品牌行右侧，折叠后挪到顶栏搜索框左边。
 * 状态存 localStorage —— 不存的话每次进页面都要再折一次。
 */
const RAIL_COLLAPSED_KEY = 'sciloop.railCollapsed'
const railCollapsed = ref(localStorage.getItem(RAIL_COLLAPSED_KEY) === '1')

function toggleRail(): void {
  railCollapsed.value = !railCollapsed.value
  localStorage.setItem(RAIL_COLLAPSED_KEY, railCollapsed.value ? '1' : '0')
}

/**
 * 「未分组 / 项目」两个分组各自的折叠开关（2026-09-24 研究者要求：分组标题旁给个折叠角）。
 *
 * 口径与左栏折叠一致：**状态存 localStorage**（不存的话每次刷新又要重新折一遍）；
 * 默认展开。收起只隐藏**直接子级**（`.crow-block` / `.group__empty`），
 * 不动嵌套在项目下的对话 —— 那些归项目自己的箭头管。
 */
const GROUP_OPEN_KEYS = {
  ungrouped: 'sciloop.rail.ungroupedOpen',
  projects: 'sciloop.rail.projectsOpen',
} as const

/** 只有明确存过 '0' 才算收起，其余（首次访问 / 存了脏值）一律按展开处理 */
function readGroupOpen(key: string): boolean {
  return localStorage.getItem(key) !== '0'
}

const ungroupedOpen = ref(readGroupOpen(GROUP_OPEN_KEYS.ungrouped))
const projectsOpen = ref(readGroupOpen(GROUP_OPEN_KEYS.projects))

function toggleGroup(which: 'ungrouped' | 'projects'): void {
  const target = which === 'ungrouped' ? ungroupedOpen : projectsOpen
  target.value = !target.value
  localStorage.setItem(GROUP_OPEN_KEYS[which], target.value ? '1' : '0')
}

/* ------------------------------------------------------------------ *
 * 左栏宽度：右边缘可拖拽（200–400px，宽高存 localStorage）
 * ------------------------------------------------------------------ */
const RAIL_WIDTH_KEY = 'sciloop.railWidth'
const RAIL_MIN = 200
const RAIL_MAX = 400
const RAIL_DEFAULT = 248
/** 键盘微调的步长（方向键按一下挪这么多） */
const RAIL_STEP = 16

function clampRailWidth(value: number): number {
  if (!Number.isFinite(value)) return RAIL_DEFAULT
  return Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(value)))
}

/** 读盘时就 clamp 一次：存进来的脏值（手改过 localStorage / 旧版本写的）不该把布局撑坏。
 *  ⚠️ 不要写成 `Number(localStorage.getItem(...))`：键不存在时 `getItem` 返回 `null`，
 *  而 `Number(null)` 是 **0**（不是 NaN），会被 clamp 成最窄的 200 —— 实测踩过。 */
function readStoredRailWidth(): number {
  const raw = localStorage.getItem(RAIL_WIDTH_KEY)
  if (raw === null || raw.trim() === '') return RAIL_DEFAULT
  return clampRailWidth(Number(raw))
}

const railWidth = ref(readStoredRailWidth())

const railResizing = ref(false)
let resizeStartX = 0
let resizeStartWidth = RAIL_DEFAULT

/**
 * 按下即 `setPointerCapture`：指针移出那 8px 热区后 `pointermove` 仍会派发到本元素，
 * 于是不用往 window 上挂监听、也就没有卸载时的泄漏与重复绑定。
 */
function startRailResize(event: PointerEvent): void {
  if (railCollapsed.value) return
  const handle = event.currentTarget as HTMLElement | null
  try {
    handle?.setPointerCapture?.(event.pointerId)
  } catch {
    // 指针已失效（极快的点击/抬起）时会抛 InvalidPointerId；捕获失败不影响拖拽本身，
    // 因为 move 事件照样会派发到本元素（指针仍在它上面）。
  }
  // **必须 preventDefault**：不拦的话，按住往右拖会移过左栏里的文字，浏览器随即启动
  // 原生文本选择/拖拽，并发出 `pointercancel` 把我们的拖拽打断 —— 实测表现是
  // 「只能拖动一小段，之后宽度再也不跟手」。
  event.preventDefault()
  railResizing.value = true
  resizeStartX = event.clientX
  resizeStartWidth = railWidth.value
}

function moveRailResize(event: PointerEvent): void {
  if (!railResizing.value) return
  railWidth.value = clampRailWidth(resizeStartWidth + (event.clientX - resizeStartX))
}

function endRailResize(event?: PointerEvent): void {
  if (!railResizing.value) return
  railResizing.value = false
  const handle = event?.currentTarget as HTMLElement | null
  if (handle?.hasPointerCapture?.(event?.pointerId ?? -1)) {
    handle.releasePointerCapture(event?.pointerId ?? -1)
  }
  localStorage.setItem(RAIL_WIDTH_KEY, String(railWidth.value))
}

/** 键盘可达：焦点在分隔条上时用 ←/→ 微调（与鼠标拖拽同一套 clamp 与落盘） */
function nudgeRail(delta: number): void {
  if (railCollapsed.value) return
  railWidth.value = clampRailWidth(railWidth.value + delta)
  localStorage.setItem(RAIL_WIDTH_KEY, String(railWidth.value))
}

/** 当前高亮的主入口；未标注 homeNav 的页面不高亮 */
const activeKey = computed(() => (route.meta?.homeNav as string | undefined) ?? '')
/** 当前高亮的模块页（二级入口） */
const activeModule = computed(() => (route.meta?.module as string | undefined) ?? '')

/** 「首页那套」的页面（开始使用 / 对话）：内容区不加模块页的内边距，撑满交给页面自己排 */
const isHomeLike = computed(() => route.name === 'home' || route.name === 'conversation')

/** 首页类路由名（判断"上一次是不是已经在首页"用，与 isHomeLike 同一口径） */
const HOME_LIKE_ROUTES = new Set(['home', 'conversation'])

/**
 * 页面入场信号：只在**从别的页面切回首页类路由**时打标。
 *
 * 「已经在首页」（点 ＋ 新建对话、首页 ↔ 对话之间互跳）不打标 ⇒ 不播入场动画：
 * 那属于同一页内部的切换，再放一遍"自下而上"会显得啰嗦。
 * 首次加载也不打标（watcher 只在 name 变化时触发），所以硬刷新不会播。
 */
watch(
  () => route.name,
  (name, previous) => {
    if (HOME_LIKE_ROUTES.has(String(name)) && !HOME_LIKE_ROUTES.has(String(previous))) {
      requestEntrance()
    }
  },
)

/**
 * 文献调研分组：落在任一子页时视为激活（并自动展开）。
 * ⚠️ 必须声明在 `activeKey` / `activeModule` **之后**：`watch(source, cb)` 会在创建时
 * 立刻求值一次 source，而 `literatureActive` 读的是上面两个 computed——
 * 顺序反了会触发 TDZ（`ReferenceError: Cannot access 'x' before initialization`），
 * 整个 bundle 直接崩成黑屏（2026-09-19 实测踩过）。
 */
const LITERATURE_MODULES = [
  'papers',
  'feed',
  'parse',
  'aggregate',
  'import',
  'translate',
  'reader',
  'export',
]
const literatureOpen = ref(false)
const literatureActive = computed(
  () => activeKey.value === 'literature' || LITERATURE_MODULES.includes(activeModule.value),
)

// immediate 必须为 true：首屏直接落在 /papers/* 时，computed 一开始就是 true，
// 没有 immediate 的话 watcher 不会触发，分组会保持收起状态。
watch(
  literatureActive,
  (active) => {
    if (active) literatureOpen.value = true
  },
  { immediate: true },
)

/** 点父项：展开/收起（首次点击只展开，不跳页——跳页交给「文献总览」子项） */
function toggleLiterature(): void {
  literatureOpen.value = !literatureOpen.value
}

function search(): void {
  const q = keyword.value.trim()
  if (!q) return
  void router.push({ path: '/papers/feed', query: { q } })
}

function openSettings(): void {
  void router.push({ path: '/settings' })
}

// ---- 同步任务历史（顶栏按钮打开）----
const historyOpen = ref(false)

// ---- 项目行：重命名 / 归档（都收进「更多」菜单里）----
const renameOpen = ref(false)
const renameTargetId = ref<number | null>(null)
const renameTargetName = ref('')

function openRename(project: { id: number; name: string }): void {
  renameTargetId.value = project.id
  renameTargetName.value = project.name
  renameOpen.value = true
}

/**
 * 「更多」浮层：同一时间只开一个，点别处 / Esc / 选中任一项都关掉。
 *
 * 用 `stopPropagation` + 文档级监听实现（不用 `mouseleave` 那种「够不着」的判定）：
 * 菜单是行内绝对定位浮层，鼠标从按钮移到菜单项的路上不会穿过别的东西。
 */
const moreOpen = ref<number | null>(null)

function toggleMore(projectId: number, event: MouseEvent): void {
  event.stopPropagation()
  moreOpen.value = moreOpen.value === projectId ? null : projectId
}

function closeMore(): void {
  moreOpen.value = null
}

function onDocumentClick(): void {
  closeMore()
  closeConvMore()
}

function onDocumentKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    closeMore()
    closeConvMore()
  }
}

// --------------------------------------------------------------------------- //
// 已归档折叠区与项目展开
// --------------------------------------------------------------------------- //
/**
 * 已归档折叠区：**展开态要持久化**（2026-09-24 研究者反馈「展开后刷新又折起来了」）。
 * 口径与左栏、未分组/项目一致：存 localStorage，只能明确存过 '1' 才算展开。
 * 数据侧不用额外处理 —— onMounted 里本来就无条件拉了已归档对话与项目（徽标条数要真）。
 */
const ARCHIVED_OPEN_KEY = 'sciloop.rail.archivedOpen'
const archivedOpen = ref(localStorage.getItem(ARCHIVED_OPEN_KEY) === '1')

async function toggleArchived(): Promise<void> {
  archivedOpen.value = !archivedOpen.value
  localStorage.setItem(ARCHIVED_OPEN_KEY, archivedOpen.value ? '1' : '0')
  if (archivedOpen.value) {
    await Promise.all([conversations.loadArchived(), session.loadArchivedProjects()])
  }
}

/**
 * 项目下的**全部**对话（含已归档）：项目本身被归档后，它的未归档对话在「项目」区
 * 会随项目行一起消失，必须在「已归档 → 项目」里仍能看得到、点得开。
 */
function projectConversations(projectId: number): ConversationBrief[] {
  const all = [...conversations.items, ...conversations.archived]
  return all.filter((item) => item.project_id === projectId)
}

const archivedProjectIds = computed(() => new Set(session.archivedProjects.map((p) => p.id)))

/** 「已归档 → 对话」只列**不挂在已归档项目下**的那些，避免与项目节点重复显示 */
const archivedLooseConversations = computed(() =>
  conversations.archived.filter((item) => {
    const owner = item.project_id
    if (owner == null) return true
    return !archivedProjectIds.value.has(owner)
  }),
)

/** 折叠区徽标 = 顶层可见行数（与展开后看到的一致） */
const archivedTotal = computed(
  () => session.archivedProjects.length + archivedLooseConversations.value.length,
)

/** 展开/收起某个项目下的对话（点项目行 = 展开，不再直接跳工作台） */
const expandedProjects = ref<number[]>([])

/** 左栏唯一的滚动容器（新建项目后要滚回顶部，否则用户以为"建了但没出现"） */
const railScrollEl = ref<HTMLElement | null>(null)

function isExpanded(projectId: number): boolean {
  return expandedProjects.value.includes(projectId)
}

function toggleProject(projectId: number): void {
  expandedProjects.value = isExpanded(projectId)
    ? expandedProjects.value.filter((id) => id !== projectId)
    : [...expandedProjects.value, projectId]
}

// --------------------------------------------------------------------------- //
// 对话：新建 / 打开 / 归档 / 移入项目
// --------------------------------------------------------------------------- //
const createProjectOpen = ref(false)
const moveOpen = ref(false)
const moveTarget = ref<ConversationBrief | null>(null)

/** 「开始使用」右侧的 ＋：**始终未分组**（在项目里点也一样） */
function startNewConversation(): void {
  void router.push({ path: '/' })
}

/** 项目行右侧的 ＋：在这条项目内新建对话（只有这个 ＋ 才是项目内新建） */
function startConversationIn(projectId: number): void {
  if (!isExpanded(projectId)) expandedProjects.value = [...expandedProjects.value, projectId]
  void router.push({ path: '/', query: { project: String(projectId) } })
}

function openConversation(conversation: ConversationBrief): void {
  void router.push({ name: 'conversation', params: { conversationId: conversation.id } })
}

/** 当前正在看的那条对话（左栏据此高亮 —— 对话页顶栏标题已去掉，这里成了唯一的方位标识） */
function isCurrentConversation(id: string): boolean {
  return route.name === 'conversation' && String(route.params.conversationId ?? '') === id
}

/**
 * 打开某项目的流水线工作台。
 * 原先是对话页顶部那个按钮（带项目参数直达），顶栏去掉后入口收到这里 ——
 * 否则「按项目直达看板」就没路径了，只能从左栏「流水线工作台」进再自己挑项目。
 */
function openProjectWorkbench(projectId: number): void {
  closeMore()
  session.selectProject(projectId)
  void router.push({ name: 'workbench', params: { projectId: String(projectId) } })
}

function openMove(conversation: ConversationBrief): void {
  moveTarget.value = conversation
  moveOpen.value = true
}

/** 归档：悬停行才出图标 + **一次确认弹窗**（归档后仍可继续聊） */
const archiveOpen = ref(false)
const archiveTarget = ref<ConversationBrief | null>(null)
const archiveBusy = ref(false)

function askArchive(conversation: ConversationBrief): void {
  archiveTarget.value = conversation
  archiveOpen.value = true
}

/* ---------- 对话行的「更多」菜单 + 重命名（2026-09-21）----------
   行内动作固定为两个：① 移入项目（未分组对话才有）② 更多（⋯ = 重命名 / 归档）。
   菜单与项目行同一套做法：**行内 .fold 块**，不做绝对定位浮层（左栏滚动区会裁掉浮层）。 */
const convMoreOpen = ref<string | null>(null)

function toggleConvMore(id: string, event: MouseEvent): void {
  // 必须 stopPropagation：document 上挂了"点任意处收起菜单"，不拦会刚开就被关掉
  event.stopPropagation()
  // 同时只允许一个菜单展开（项目行那个也要收起，否则两块菜单一起摊开很乱）
  moreOpen.value = null
  convMoreOpen.value = convMoreOpen.value === id ? null : id
}

function closeConvMore(): void {
  convMoreOpen.value = null
}

const convRenameOpen = ref(false)
const convRenameTarget = ref<ConversationBrief | null>(null)

function openConvRename(conversation: ConversationBrief): void {
  closeConvMore()
  convRenameTarget.value = conversation
  convRenameOpen.value = true
}

async function onConversationRenamed(): Promise<void> {
  // 标题落到左栏 + 对话页顶部的唯一来源是 store，改完重新拉一次，不做本地假改
  await conversations.load()
}

async function confirmArchive(): Promise<void> {
  const target = archiveTarget.value
  if (!target || archiveBusy.value) return
  archiveBusy.value = true
  try {
    await setConversationArchived(target.id, true)
    conversations.remove(target.id)
    // 折叠状态下也要刷新：徽标上的条数必须是真的（显示 0 却藏着一条就是假状态）
    await conversations.loadArchived()
    archiveOpen.value = false
    archiveTarget.value = null
  } catch (error) {
    ElMessage.warning(messageOf(error))
  } finally {
    archiveBusy.value = false
  }
}

async function unarchive(conversation: ConversationBrief): Promise<void> {
  try {
    await setConversationArchived(conversation.id, false)
    await conversations.load()
    await conversations.loadArchived()
  } catch (error) {
    ElMessage.warning(messageOf(error))
  }
}

function messageOf(error: unknown): string {
  const status = (error as { status?: number } | undefined)?.status
  if (status === 403) {
    return writeDenied('修改对话')
  }
  return error instanceof Error ? error.message : String(error)
}

async function onMoved(): Promise<void> {
  await conversations.load()
}

/**
 * 左栏「项目」分组的**显示顺序**（排序规则与理由见 `utils/projectOrder.ts`）。
 *
 * 一句话：后端按手册 B.5 的 `id ASC` 返回，新建项目会掉到最后一行；
 * 左栏改成「非演示项目最新在最上、演示夹具退到底部」，新建的项目固定出现在第一行。
 */
const displayProjects = computed(() => orderProjectsForRail(session.projects))

async function onProjectCreated(project: CreatedProject): Promise<void> {
  await session.loadProjects()
  expandedProjects.value = [...expandedProjects.value, project.id]
  // 新项目现在固定排在最上面（见 displayProjects），但左栏可能正滚在下面 ——
  // 不滚回去的话用户会以为"建了但没出现"。
  await nextTick()
  railScrollEl.value?.scrollTo({ top: 0, behavior: 'smooth' })
}

// --------------------------------------------------------------------------- //
// 项目归档 / 取消归档
// --------------------------------------------------------------------------- //
const projectArchiveOpen = ref(false)
const projectArchiveBusy = ref(false)
const projectArchiveTarget = ref<{ id: number; name: string } | null>(null)

function askArchiveProject(project: { id: number; name: string }): void {
  closeMore()
  projectArchiveTarget.value = project
  projectArchiveOpen.value = true
}

async function confirmArchiveProject(): Promise<void> {
  const target = projectArchiveTarget.value
  if (!target || projectArchiveBusy.value) return
  projectArchiveBusy.value = true
  try {
    await setProjectArchived(target.id, true)
    await Promise.all([
      session.loadProjects(),
      session.loadArchivedProjects(),
      conversations.load(),
      conversations.loadArchived(),
    ])
    projectArchiveOpen.value = false
    projectArchiveTarget.value = null
  } catch (error) {
    ElMessage.warning(projectMessageOf(error))
  } finally {
    projectArchiveBusy.value = false
  }
}

async function unarchiveProject(projectId: number): Promise<void> {
  try {
    await setProjectArchived(projectId, false)
    await Promise.all([
      session.loadProjects(),
      session.loadArchivedProjects(),
      conversations.load(),
      conversations.loadArchived(),
    ])
  } catch (error) {
    ElMessage.warning(projectMessageOf(error))
  }
}

function projectMessageOf(error: unknown): string {
  const status = (error as { status?: number } | undefined)?.status
  if (status === 403) return writeDenied('修改项目')
  return error instanceof Error ? error.message : String(error)
}

// ---- 任务：顶栏按钮 → 历史 → 打开某一条的完整监控窗口 ----
const tasks = useTaskStore()
const pipelineDrawer = usePipelineDrawerStore()
const monitorOpen = ref(false)

async function openTaskMonitor(taskId: string): Promise<void> {
  monitorOpen.value = true
  await tasks.track(taskId)
}

async function controlTask(kind: 'pause' | 'resume' | 'cancel'): Promise<void> {
  await tasks.control(kind)
}

/** 监控窗口里的「重新同步」：再跑一轮并跟踪它（Owner 面，403 如实提示） */
async function retryFetch(): Promise<void> {
  const result = await tasks.startFetch(30)
  if (!result.ok) {
    ElMessage.warning(
      result.status === 403
        ? '现在是只读浏览模式，没法在服务端触发抓取：到「设置」里切换成研究者身份后就能用。'
        : (result.message ?? '触发抓取失败'),
    )
  }
}

onMounted(() => {
  void session.loadProjects()
  void session.loadHealth()
  void conversations.load()
  // 已归档要一起拉：折叠区虽然收起，但徽标上的条数得是真的
  void conversations.loadArchived()
  void session.loadArchivedProjects()
  // 「更多」浮层的关闭：点任意处 / Esc
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onDocumentKeydown)
  tasks.startWatch()
})

onUnmounted(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onDocumentKeydown)
  tasks.stopWatch()
  tasks.stopPolling()
})
</script>

<template>
  <div class="sl-home" :class="{ 'sl-home--resizing': railResizing }">
    <!-- `inert`：折叠后左栏整体滑出可视区，但里面的几十个链接仍会拦键盘 Tab，
         所以折叠时把它整块对键盘/读屏关掉（`:inert` 传 undefined 才会真正摘掉属性）。 -->
    <aside
      id="rail"
      class="rail"
      :class="{ 'rail--collapsed': railCollapsed, 'rail--dragging': railResizing }"
      :style="{ '--rail-w': `${railWidth}px` }"
      :inert="railCollapsed || undefined"
    >
      <div class="brand">
        <SciLoopMark class="brand__mark" />
        <div class="brand__name">SciLoop</div>
        <button
          class="rail-toggle"
          type="button"
          title="折叠左栏（折叠后按钮移到顶部搜索框左侧）"
          aria-label="折叠左栏"
          aria-controls="rail"
          :aria-expanded="!railCollapsed"
          @click="toggleRail"
        >
          <!-- 面板图标（外框 + 内侧靠左实心竖条）：展开/折叠两态共用一个，
               靠 tooltip 区分 —— 同形不同义比翻转箭头更不容易误判。 -->
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <rect
              x="2"
              y="3"
              width="16"
              height="14"
              rx="4"
              stroke="currentColor"
              stroke-width="1.6"
            />
            <rect x="5.2" y="6.2" width="2.2" height="7.6" rx="1.1" fill="currentColor" />
          </svg>
        </button>
      </div>

      <div class="rail__fixed">
        <nav class="nav" aria-label="总览导航">
          <template v-for="item in HOME_NAV" :key="item.key">
            <!-- 可展开分组：文献调研 -->
            <div v-if="item.children" class="nav__block">
              <div class="nav__row" :class="{ 'nav__row--on': literatureActive }">
                <button
                  class="nav__item nav__item--group"
                  type="button"
                  :aria-expanded="literatureOpen"
                  @click="toggleLiterature"
                >
                  <span class="nav__icon">
                    <svg width="17" height="17" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <circle cx="7.1" cy="7.1" r="4.3" stroke="currentColor" stroke-width="1.3" />
                      <path
                        d="M10.3 10.3 13.6 13.6"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linecap="round"
                      />
                    </svg>
                  </span>
                  {{ item.label }}
                  <span class="nav__caret" :class="{ 'nav__caret--open': literatureOpen }">
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                      <path
                        d="M2.6 4.4 6 7.8l3.4-3.4"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      />
                    </svg>
                  </span>
                </button>
              </div>

            <!-- 展开/收起走全站统一的 .fold（双向高度过渡），不再用「只淡入、收起瞬变」的 animation -->
            <div class="fold" :class="{ 'fold--open': literatureOpen }">
              <div class="nav__children">
                <RouterLink
                  v-for="child in item.children"
                  :key="child.key"
                  class="nav__item nav__item--child"
                  :class="{ 'nav__item--on': activeModule === child.key }"
                  :to="child.path"
                >
                  <span class="nav__dot" />
                  {{ child.label }}
                </RouterLink>
              </div>
            </div>
            </div>

            <!-- 普通项：开始使用 / 知识库 -->
            <div v-else class="nav__row" :class="{ 'nav__row--on': activeKey === item.key }">
              <RouterLink class="nav__item" :to="item.path">
                <span class="nav__icon">
                  <!-- 开始使用 -->
                  <svg
                    v-if="item.key === 'home'"
                    width="17"
                    height="17"
                    viewBox="0 0 16 16"
                    fill="none"
                    aria-hidden="true"
                  >
                    <path
                      d="M8 1.9c2.5 1.3 3.7 3.4 3.7 6.3v2.9H4.3V8.2c0-2.9 1.2-5 3.7-6.3Z"
                      stroke="currentColor"
                      stroke-width="1.3"
                      stroke-linejoin="round"
                    />
                    <circle cx="8" cy="7.1" r="1.5" stroke="currentColor" stroke-width="1.3" />
                    <path
                      d="M6 11.1 4.6 13.6M10 11.1l1.4 2.5"
                      stroke="currentColor"
                      stroke-width="1.3"
                      stroke-linecap="round"
                    />
                  </svg>
                  <!-- 知识库 -->
                  <svg v-else width="17" height="17" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path
                      d="M2.8 3.2h3.9a1.5 1.5 0 0 1 1.5 1.5v7.9a1.2 1.2 0 0 0-1.2-1.2H2.8V3.2Z"
                      stroke="currentColor"
                      stroke-width="1.3"
                      stroke-linejoin="round"
                    />
                    <path
                      d="M13.2 3.2H9.3a1.5 1.5 0 0 0-1.5 1.5v7.9a1.2 1.2 0 0 1 1.2-1.2h4.2V3.2Z"
                      stroke="currentColor"
                      stroke-width="1.3"
                      stroke-linejoin="round"
                    />
                  </svg>
                </span>
                {{ item.label }}
              </RouterLink>
              <button
                v-if="item.key === 'home'"
                class="icon-btn nav__plus"
                type="button"
                title="新建对话"
                aria-label="新建对话"
                @click="startNewConversation"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </template>
        </nav>
      </div>

      <!-- 分块线：固定区到此为止，下面是唯一会滚的那块 -->
      <div class="rail__divider" />

      <div ref="railScrollEl" class="rail__scroll">
        <nav class="nav nav--modules" aria-label="模块导航">
          <RouterLink
            v-for="item in MODULE_NAV"
            :key="item.key"
            class="nav__item nav__item--solo"
            :class="{ 'nav__item--on': activeModule === item.key }"
            :to="item.path"
          >
            <span class="nav__icon">
              <!-- 研究构想 -->
              <svg
                v-if="item.key === 'ideas'"
                width="17"
                height="17"
                viewBox="0 0 16 16"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M8 2.4a4.2 4.2 0 0 0-2.4 7.6v1.4h4.8V10A4.2 4.2 0 0 0 8 2.4Z"
                  stroke="currentColor"
                  stroke-width="1.3"
                  stroke-linejoin="round"
                />
                <path d="M6.4 13.4h3.2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
              </svg>
              <!-- 流水线工作台 -->
              <svg v-else width="17" height="17" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <circle cx="3.6" cy="8" r="1.7" stroke="currentColor" stroke-width="1.3" />
                <circle cx="12.4" cy="4.4" r="1.7" stroke="currentColor" stroke-width="1.3" />
                <circle cx="12.4" cy="11.6" r="1.7" stroke="currentColor" stroke-width="1.3" />
                <path
                  d="M5.2 7.2 10.8 5M5.2 8.8l5.6 2.2"
                  stroke="currentColor"
                  stroke-width="1.3"
                  stroke-linecap="round"
                />
              </svg>
            </span>
            {{ item.label }}
          </RouterLink>
        </nav>

        <!-- 未分组对话 -->
        <div class="group" :class="{ 'group--closed': !ungroupedOpen }">
          <div class="group__head group__head--foldable">
            <button
              class="group__chev"
              type="button"
              :aria-expanded="ungroupedOpen"
              aria-label="折叠或展开未分组"
              @click="toggleGroup('ungrouped')"
            >
              <svg
                class="group__caret"
                :class="{ 'group__caret--open': ungroupedOpen }"
                width="10"
                height="10"
                viewBox="0 0 10 10"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M3 1.5 6.5 5 3 8.5"
                  stroke="currentColor"
                  stroke-width="1.4"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </button>
            <span class="group__title">未分组</span>
          </div>
          <p v-if="conversations.ungrouped.length === 0" class="group__empty">
            {{ conversations.error || '暂无未分组对话' }}
          </p>
          <div v-for="c in conversations.ungrouped" :key="c.id" class="crow-block">
            <div
              class="crow"
              :class="{ 'crow--on': isCurrentConversation(c.id), 'crow--open': convMoreOpen === c.id }"
            >
            <button
              class="crow__item"
              :class="{ 'crow__item--on': isCurrentConversation(c.id) }"
              type="button"
              :title="c.title || '未命名对话'"
              @click="openConversation(c)"
            >
              {{ c.title || '未命名对话' }}
            </button>
            <span class="crow__acts">
              <button
                class="icon-btn"
                type="button"
                title="移入项目"
                aria-label="移入项目"
                @click.stop="openMove(c)"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <path
                    d="M2.4 4.4h3.6l1.2 1.5h6.4v6.1a.8.8 0 0 1-.8.8H3.2a.8.8 0 0 1-.8-.8V4.4Z"
                    stroke="currentColor"
                    stroke-width="1.3"
                    stroke-linejoin="round"
                  />
                  <path d="M8 8.4v3.4M6.4 10h3.2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                </svg>
              </button>
              <button
                class="icon-btn"
                type="button"
                title="更多"
                aria-label="更多"
                aria-haspopup="menu"
                :aria-expanded="convMoreOpen === c.id ? 'true' : 'false'"
                @click="toggleConvMore(c.id, $event)"
              >
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                  <circle cx="3.4" cy="8" r="1.3" fill="currentColor" />
                  <circle cx="8" cy="8" r="1.3" fill="currentColor" />
                  <circle cx="12.6" cy="8" r="1.3" fill="currentColor" />
                </svg>
              </button>
            </span>
            </div>

            <!-- 行内展开的菜单：与项目行同一做法（.fold 与 .crow 平级，不做绝对定位浮层） -->
            <div class="fold" :class="{ 'fold--open': convMoreOpen === c.id }">
              <ul class="pmenu" role="menu">
                <li>
                  <button class="pmenu__item" type="button" role="menuitem" @click="openConvRename(c)">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path
                        d="M11.4 2.6a1.35 1.35 0 0 1 1.9 1.9l-7.6 7.6-2.7.8.8-2.7 7.6-7.6Z"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linejoin="round"
                      />
                      <path d="M10.3 3.7 12.3 5.7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                    </svg>
                    重命名
                  </button>
                </li>
                <li>
                  <button
                    class="pmenu__item"
                    type="button"
                    role="menuitem"
                    @click="closeConvMore(); askArchive(c)"
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path
                        d="M2.6 5.6h10.8v7a.8.8 0 0 1-.8.8H3.4a.8.8 0 0 1-.8-.8v-7Z"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linejoin="round"
                      />
                      <path d="M2 3.4h12v2.2H2z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
                      <path d="M6.6 8.4h2.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                    </svg>
                    归档
                  </button>
                </li>
              </ul>
            </div>
          </div>
        </div>

        <!-- 项目（1 : N 对话） -->
        <div class="group" :class="{ 'group--closed': !projectsOpen }">
          <div class="group__head group__head--foldable">
            <button
              class="group__chev"
              type="button"
              :aria-expanded="projectsOpen"
              aria-label="折叠或展开项目"
              @click="toggleGroup('projects')"
            >
              <svg
                class="group__caret"
                :class="{ 'group__caret--open': projectsOpen }"
                width="10"
                height="10"
                viewBox="0 0 10 10"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M3 1.5 6.5 5 3 8.5"
                  stroke="currentColor"
                  stroke-width="1.4"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>
            </button>
            <span class="group__title">项目</span>
            <button
              class="icon-btn"
              type="button"
              title="新建项目"
              aria-label="新建项目"
              @click="createProjectOpen = true"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
          </div>
          <p v-if="session.projects.length === 0" class="group__empty">
            {{ session.projectsError || '暂无项目' }}
          </p>
          <template v-for="p in displayProjects" :key="p.id">
            <div class="crow crow--project" :class="{ 'crow--open': moreOpen === p.id }">
              <button
                class="crow__item crow__item--project"
                type="button"
                :title="p.name"
                :aria-expanded="isExpanded(p.id)"
                @click="toggleProject(p.id)"
              >
                <span class="crow__caret" :class="{ 'crow__caret--open': isExpanded(p.id) }">
                  <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                    <path
                      d="M4.4 2.6 7.8 6l-3.4 3.4"
                      stroke="currentColor"
                      stroke-width="1.5"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                </span>
                <span class="crow__label">{{ p.name }}</span>
              </button>
              <span class="crow__acts">
                <button
                  class="icon-btn"
                  type="button"
                  title="在项目内新建对话"
                  aria-label="在项目内新建对话"
                  @click="startConversationIn(p.id)"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                  </svg>
                </button>
                <button
                  class="icon-btn"
                  type="button"
                  title="更多"
                  aria-label="更多"
                  aria-haspopup="menu"
                  :aria-expanded="moreOpen === p.id ? 'true' : 'false'"
                  @click="toggleMore(p.id, $event)"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <circle cx="3.4" cy="8" r="1.3" fill="currentColor" />
                    <circle cx="8" cy="8" r="1.3" fill="currentColor" />
                    <circle cx="12.6" cy="8" r="1.3" fill="currentColor" />
                  </svg>
                </button>
              </span>
            </div>

            <!-- 「更多」菜单做成**行内块**而不是绝对定位浮层：左栏滚动区是 overflow:auto，
                 浮层贴边时会被裁掉；行内块不会被裁，也不用算坐标。
                 展开/收起同样走统一 .fold。 -->
            <div class="fold" :class="{ 'fold--open': moreOpen === p.id }">
              <ul class="pmenu" role="menu">
                <li>
                  <button
                    class="pmenu__item"
                    type="button"
                    role="menuitem"
                    @click="openProjectWorkbench(p.id)"
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <circle cx="3.6" cy="8" r="1.7" stroke="currentColor" stroke-width="1.3" />
                      <circle cx="12.4" cy="4.4" r="1.7" stroke="currentColor" stroke-width="1.3" />
                      <circle cx="12.4" cy="11.6" r="1.7" stroke="currentColor" stroke-width="1.3" />
                      <path
                        d="M5.2 7.2 10.8 5M5.2 8.8l5.6 2.2"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linecap="round"
                      />
                    </svg>
                    流水线工作台
                  </button>
                </li>
                <li>
                  <button class="pmenu__item" type="button" role="menuitem" @click="closeMore(); openRename(p)">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path
                        d="M11.4 2.6a1.35 1.35 0 0 1 1.9 1.9l-7.6 7.6-2.7.8.8-2.7 7.6-7.6Z"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linejoin="round"
                      />
                      <path d="M10.3 3.7 12.3 5.7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                    </svg>
                    重命名
                  </button>
                </li>
                <li>
                  <button class="pmenu__item" type="button" role="menuitem" @click="askArchiveProject(p)">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path
                        d="M2.6 5.6h10.8v7a.8.8 0 0 1-.8.8H3.4a.8.8 0 0 1-.8-.8v-7Z"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linejoin="round"
                      />
                      <path d="M2 3.4h12v2.2H2z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
                      <path d="M6.6 8.4h2.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                    </svg>
                    归档
                  </button>
                </li>
              </ul>
            </div>

            <div class="fold" :class="{ 'fold--open': isExpanded(p.id) }">
              <div class="ckids">
                <p v-if="conversations.forProject(p.id).length === 0" class="group__empty group__empty--child">
                  该项目暂无对话
                </p>
                <div v-for="c in conversations.forProject(p.id)" :key="c.id" class="crow-block">
                  <div
                    class="crow crow--child"
                    :class="{ 'crow--on': isCurrentConversation(c.id), 'crow--open': convMoreOpen === c.id }"
                  >
                  <button
                    class="crow__item"
                    :class="{ 'crow__item--on': isCurrentConversation(c.id) }"
                    type="button"
                    :title="c.title || '未命名对话'"
                    @click="openConversation(c)"
                  >
                    {{ c.title || '未命名对话' }}
                  </button>
                  <span class="crow__acts">
                    <!-- 已在项目里，所以行内只留「更多」（重命名 / 归档）；移入别的项目走「更多」之外不做，避免误操作 -->
                    <button
                      class="icon-btn"
                      type="button"
                      title="更多"
                      aria-label="更多"
                      aria-haspopup="menu"
                      :aria-expanded="convMoreOpen === c.id ? 'true' : 'false'"
                      @click="toggleConvMore(c.id, $event)"
                    >
                      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                        <circle cx="3.4" cy="8" r="1.3" fill="currentColor" />
                        <circle cx="8" cy="8" r="1.3" fill="currentColor" />
                        <circle cx="12.6" cy="8" r="1.3" fill="currentColor" />
                      </svg>
                    </button>
                  </span>
                  </div>

                  <div class="fold" :class="{ 'fold--open': convMoreOpen === c.id }">
                    <ul class="pmenu pmenu--child" role="menu">
                      <li>
                        <button class="pmenu__item" type="button" role="menuitem" @click="openConvRename(c)">
                          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                            <path
                              d="M11.4 2.6a1.35 1.35 0 0 1 1.9 1.9l-7.6 7.6-2.7.8.8-2.7 7.6-7.6Z"
                              stroke="currentColor"
                              stroke-width="1.3"
                              stroke-linejoin="round"
                            />
                            <path d="M10.3 3.7 12.3 5.7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                          </svg>
                          重命名
                        </button>
                      </li>
                      <li>
                        <button
                          class="pmenu__item"
                          type="button"
                          role="menuitem"
                          @click="closeConvMore(); askArchive(c)"
                        >
                          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                            <path
                              d="M2.6 5.6h10.8v7a.8.8 0 0 1-.8.8H3.4a.8.8 0 0 1-.8-.8v-7Z"
                              stroke="currentColor"
                              stroke-width="1.3"
                              stroke-linejoin="round"
                            />
                            <path d="M2 3.4h12v2.2H2z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
                            <path d="M6.6 8.4h2.8" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
                          </svg>
                          归档
                        </button>
                      </li>
                    </ul>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>

        <!-- 已归档：折叠区，分「项目 / 对话」两块，都可展开、可取消归档 -->
        <div class="group">
          <div class="group__head">
            <button
              class="group__toggle"
              type="button"
              :aria-expanded="archivedOpen"
              @click="toggleArchived"
            >
              <span class="crow__caret" :class="{ 'crow__caret--open': archivedOpen }">
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                  <path
                    d="M4.4 2.6 7.8 6l-3.4 3.4"
                    stroke="currentColor"
                    stroke-width="1.5"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                  />
                </svg>
              </span>
              <span class="group__title">已归档</span>
              <span class="group__count">{{ archivedTotal }}</span>
            </button>
          </div>
          <div class="fold" :class="{ 'fold--open': archivedOpen }">
            <div class="ckids">
            <p class="group__title group__title--nested">项目</p>
            <p v-if="session.archivedProjects.length === 0" class="group__empty group__empty--child">
              {{ session.archivedProjectsError || '暂无已归档项目' }}
            </p>
            <template v-for="p in session.archivedProjects" :key="`archived-project-${p.id}`">
              <div class="crow crow--child">
                <button
                  class="crow__item"
                  type="button"
                  :title="p.name"
                  :aria-expanded="isExpanded(p.id)"
                  @click="toggleProject(p.id)"
                >
                  <span class="crow__caret" :class="{ 'crow__caret--open': isExpanded(p.id) }">
                    <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                      <path
                        d="M4.4 2.6 7.8 6l-3.4 3.4"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      />
                    </svg>
                  </span>
                  <span class="crow__label">{{ p.name }}</span>
                </button>
                <span class="crow__acts crow__acts--always">
                  <button
                    class="icon-btn"
                    type="button"
                    title="取消归档"
                    aria-label="取消归档"
                    @click="unarchiveProject(p.id)"
                  >
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path
                        d="M3.6 8a4.4 4.4 0 1 0 1.4-3.2M3.4 3.2v3h3"
                        stroke="currentColor"
                        stroke-width="1.3"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                      />
                    </svg>
                  </button>
                </span>
              </div>
              <div class="fold" :class="{ 'fold--open': isExpanded(p.id) }">
                <div class="ckids ckids--deep">
                  <p v-if="projectConversations(p.id).length === 0" class="group__empty group__empty--child">
                    该项目暂无对话
                  </p>
                  <div v-for="c in projectConversations(p.id)" :key="c.id" class="crow crow--child">
                    <button
                      class="crow__item" :class="{ 'crow__item--on': isCurrentConversation(c.id) }"
                      type="button"
                      :title="c.title || '未命名对话'"
                      @click="openConversation(c)"
                    >
                      {{ c.title || '未命名对话' }}
                    </button>
                    <span v-if="c.archived" class="crow__acts crow__acts--always">
                      <button
                        class="icon-btn"
                        type="button"
                        title="取消归档"
                        aria-label="取消归档"
                        @click="unarchive(c)"
                      >
                        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                          <path
                            d="M3.6 8a4.4 4.4 0 1 0 1.4-3.2M3.4 3.2v3h3"
                            stroke="currentColor"
                            stroke-width="1.3"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                          />
                        </svg>
                      </button>
                    </span>
                  </div>
                </div>
              </div>
            </template>

            <p class="group__title group__title--nested">对话</p>
            <p v-if="archivedLooseConversations.length === 0" class="group__empty group__empty--child">
              {{ conversations.archivedError || '暂无已归档对话' }}
            </p>
            <div v-for="c in archivedLooseConversations" :key="c.id" class="crow crow--child">
              <button class="crow__item" :class="{ 'crow__item--on': isCurrentConversation(c.id) }" type="button" :title="c.title || '未命名对话'" @click="openConversation(c)">
                {{ c.title || '未命名对话' }}
              </button>
              <span class="crow__acts crow__acts--always">
                <button
                  class="icon-btn"
                  type="button"
                  title="取消归档"
                  aria-label="取消归档"
                  @click="unarchive(c)"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path
                      d="M3.6 8a4.4 4.4 0 1 0 1.4-3.2M3.4 3.2v3h3"
                      stroke="currentColor"
                      stroke-width="1.3"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                </button>
              </span>
            </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 拖拽热区：贴在左栏右边缘（`.rail` 是 position:relative，整块滑出视口时它跟着走，
           所以折叠态自然拖不到 —— 折叠与调宽职责分开，不会误触）。
           键盘可达：Tab 到它以后 ←/→ 微调，符合分隔条（separator）的 ARIA 惯例。 -->
      <div
        class="rail-resizer"
        :class="{ 'rail-resizer--on': railResizing }"
        role="separator"
        aria-orientation="vertical"
        data-focus-plain
        :aria-label="`调整左侧导航宽度（${RAIL_MIN} 到 ${RAIL_MAX} 像素）`"
        :aria-valuenow="railWidth"
        :aria-valuemin="RAIL_MIN"
        :aria-valuemax="RAIL_MAX"
        :tabindex="railCollapsed ? -1 : 0"
        @pointerdown="startRailResize"
        @pointermove="moveRailResize"
        @pointerup="endRailResize"
        @pointercancel="endRailResize"
        @lostpointercapture="endRailResize"
        @keydown.left.prevent="nudgeRail(-RAIL_STEP)"
        @keydown.right.prevent="nudgeRail(RAIL_STEP)"
      />
    </aside>

    <div
      class="main"
      :style="{
        '--rfd-shift': isHomeLike && pipelineDrawer.open ? `${pipelineDrawer.width}px` : '0px',
      }"
    >
      <header class="topbar">
        <!-- 折叠后，同一个开关挪到这里：搜索框左边。展开时它回左栏品牌行右侧。 -->
        <button
          v-if="railCollapsed"
          class="rail-toggle pop-in"
          type="button"
          title="展开左栏"
          aria-label="展开左栏"
          aria-controls="rail"
          :aria-expanded="!railCollapsed"
          @click="toggleRail"
        >
          <svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true">
            <rect
              x="2"
              y="3"
              width="16"
              height="14"
              rx="4"
              stroke="currentColor"
              stroke-width="1.6"
            />
            <rect x="5.2" y="6.2" width="2.2" height="7.6" rx="1.1" fill="currentColor" />
          </svg>
        </button>

        <div class="search">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="4.6" stroke="currentColor" stroke-width="1.4" />
            <path d="M10.6 10.6 14 14" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          </svg>
          <input
            v-model="keyword"
            type="search"
            placeholder="搜索论文标题 / 摘要"
            aria-label="搜索论文（标题或摘要）"
            @keyup.enter="search"
          />
        </div>

        <div class="topbar__spacer" />

        <button
          class="theme-toggle theme-toggle--task"
          :class="{ 'theme-toggle--running': tasks.hasRunning }"
          type="button"
          :title="tasks.hasRunning ? '任务（有任务正在运行）' : '任务'"
          aria-label="任务"
          @click="historyOpen = true"
        >
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path
              d="M3.2 9a5.8 5.8 0 1 0 1.9-4.3"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
            />
            <path
              d="M5.1 1.9v2.8h2.8"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
            <path
              d="M9 6.2V9l2.1 1.3"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </button>

        <button
          class="theme-toggle"
          type="button"
          :aria-label="session.theme === 'dark' ? '切换为浅色' : '切换为深色'"
          :title="session.theme === 'dark' ? '切换为浅色' : '切换为深色'"
          @click="session.toggleTheme()"
        >
          <svg v-if="session.theme === 'dark'" width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <circle cx="9" cy="9" r="3.4" stroke="currentColor" stroke-width="1.5" />
            <path
              d="M9 2.2v1.6M9 14.2v1.6M2.2 9h1.6M14.2 9h1.6M4.3 4.3l1.2 1.2M12.5 12.5l1.2 1.2M13.7 4.3l-1.2 1.2M5.5 12.5l-1.2 1.2"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linecap="round"
            />
          </svg>
          <svg v-else width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
            <path
              d="M14.6 11.3A5.7 5.7 0 0 1 6.7 3.4a5.9 5.9 0 1 0 7.9 7.9Z"
              stroke="currentColor"
              stroke-width="1.5"
              stroke-linejoin="round"
            />
          </svg>
        </button>

        <div class="avatar-wrap">
          <!-- 身份说明**只放在悬停菜单里**（.menu__meta），不再给头像挂 title ——
               2026-09-24 实测：title 会被全局 tooltip 渲染成气泡，而它和悬停菜单是同一个落点
               （都在头像下方 48px 内），气泡正好压在菜单右上角，看着像"菜单没出来"。
               读屏需要的那份说明改用 aria-label 带（视觉不重复、也不打架）。 -->
          <div
            class="avatar"
            tabindex="0"
            role="button"
            aria-haspopup="menu"
            :aria-label="`账号：${session.accessLabel}`"
          >
            Y
          </div>
          <div class="menu" role="menu">
            <button class="menu__item" type="button" role="menuitem" @click="openSettings">设置</button>
            <div class="menu__sep" />
            <div class="menu__meta">
              {{ session.isOwner ? '研究者身份（可写）' : '只读浏览' }}
            </div>
          </div>
        </div>
      </header>

      <main class="content" :class="{ 'content--page': !isHomeLike }">
        <RouterView />
      </main>

      <!-- 研究流程入口：贴在内容区右上角（顶栏下方）。抽屉打开后它就让位给抽屉右上角那个折叠按钮。
           放在 .content 之外（.content 是滚动容器，绝对定位子元素会跟着滚走）。
           无外框，只有图标；随抽屉宽度左移。 -->
      <button
        v-if="isHomeLike && !pipelineDrawer.open"
        class="panel-entry"
        type="button"
        aria-label="研究流程"
        @click="pipelineDrawer.toggle()"
      >
        <svg width="18" height="18" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <rect x="1.9" y="3.1" width="12.2" height="9.8" rx="3" stroke="currentColor" stroke-width="1.5" />
          <rect x="10.2" y="5.9" width="1.8" height="4.2" rx="0.9" fill="currentColor" />
        </svg>
      </button>
    </div>

    <ProjectRenameDialog
      v-model="renameOpen"
      :project-id="renameTargetId"
      :project-name="renameTargetName"
    />
    <ConversationRenameDialog
      v-model="convRenameOpen"
      :conversation-id="convRenameTarget?.id ?? null"
      :conversation-title="convRenameTarget?.title ?? ''"
      @renamed="onConversationRenamed"
    />
    <ProjectNameDialog v-model="createProjectOpen" @created="onProjectCreated" />
    <MoveConversationDialog
      v-model="moveOpen"
      :conversation-id="moveTarget?.id ?? null"
      :current-project-id="moveTarget?.project_id ?? null"
      :projects="session.projects"
      @moved="onMoved"
    />
    <ConfirmDialog
      v-model="archiveOpen"
      title="归档对话"
      :message="`「${archiveTarget?.title || '未命名对话'}」将从主列表收起并移入「已归档」。归档后仍可继续对话。`"
      confirm-text="归档"
      :busy="archiveBusy"
      @confirm="confirmArchive"
    />
    <ConfirmDialog
      v-model="projectArchiveOpen"
      title="归档项目"
      :message="`「${projectArchiveTarget?.name || '该项目'}」将从「项目」收起并移入「已归档」，项目下的对话一并收起；归档后仍可从「已归档」展开并继续对话。`"
      confirm-text="归档"
      :busy="projectArchiveBusy"
      @confirm="confirmArchiveProject"
    />
    <TaskHistoryDialog v-model="historyOpen" @open="openTaskMonitor" />
    <TaskMonitorDialog
      v-model="monitorOpen"
      :job="tasks.activeJob"
      @retry="retryFetch"
      @pause="controlTask('pause')"
      @resume="controlTask('resume')"
      @cancel="controlTask('cancel')"
    />
  </div>
</template>

<style scoped>
/* 壳层：整页不滚动，只有中间内容区滚动（左栏与顶栏固定）
   —— `height`（而非 `min-height`）+ `overflow: hidden` 是必须的：
   只要留着 `min-height: 100vh`，内容变高时根容器会被撑高，整页就会出现第二条滚动条。 */
.sl-home {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--h-page-bg);
  color: var(--h-fg);
  font-family: 'Open Sans', ui-sans-serif, system-ui, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  font-size: var(--font-size-lg);
  line-height: 1.5;
}

/* 换色渐变：所有会随主题变化的表面统一 300ms 过渡（n8n motion 300ms） */
.sl-home,
.rail,
.topbar,
.search,
.theme-toggle,
.avatar,
.menu,
.content {
  transition:
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}

/* ---------- 左栏 ---------- */
/* 两块结构：固定区（不滚）+ 分块线 + 独立滚动区。
   整页不滚（.sl-home 已 height:100vh/overflow:hidden），左栏自己也不再整列滚动，
   只有 .rail__scroll 会滚——否则"哪块在滚"会随内容长度漂移。 */
.rail {
  /* 宽度的**默认值**只在这里定义一次；拖拽时由行内样式覆盖同一个自定义属性
     （行内样式优先级高于类规则），折叠位移继续用 calc 取负 —— 一处定义、三处复用。 */
  --rail-w: 248px;
  position: relative;
  width: var(--rail-w);
  flex: none;
  overflow: hidden;
  padding: 24px 16px 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--h-surface);
  border-right: 1px solid var(--h-line);
  /* 这里必须把换色那三档也一并写上：本规则在共享换色规则之后，
     `transition` 是简写、会整体覆盖，漏掉就会让左栏切换主题时硬跳。 */
  transition:
    width var(--motion-dur) var(--motion-ease),
    margin-left var(--motion-dur) var(--motion-ease),
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}
/* 拖拽过程中掐掉过渡：否则宽度会追着鼠标做 260ms 缓动，手感发黏（像拖着一根橡皮筋）。
   这也正是上一条要单独列出 `width` 过渡的原因 —— 双击/键盘微调时它又需要平滑。 */
.rail--dragging {
  transition: none;
}
/* 折叠：整块向左滑出（位移而不是压宽度 —— 压宽会把栏内文字挤成换行）。
   边框同时转透明，否则归位到 x=0 时会在最左边留一条 1px 竖线。 */
.rail--collapsed {
  margin-left: calc(var(--rail-w) * -1);
  border-right-color: transparent;
}
/* 拖拽分隔条：8px 热区叠在左栏内边距上（16px 的右内边距够放），不遮挡任何内容。
   平时完全透明，hover / 拖拽中 / 键盘聚焦时显一条 2px 品牌色细线作为抓手提示。 */
.rail-resizer {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 8px;
  cursor: col-resize;
  touch-action: none;
}
.rail-resizer::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  border-radius: 2px;
  background: var(--h-primary);
  opacity: 0;
  transition: opacity var(--motion-dur-fast) var(--motion-ease);
}
/* 焦点态复用同一条品牌色细线当指示器（元素挂了 `data-focus-plain`，
   走站内既有的「自带焦点指示」机制）：8px 宽、通高的元素套一圈全局描边会很怪。 */
.rail-resizer:hover::after,
.rail-resizer--on::after,
.rail-resizer:focus-visible::after {
  opacity: 1;
}
/* 拖拽中把光标与选区锁住：指针很容易甩出那 8px，
   不锁的话光标会在 col-resize 与默认之间闪，而且会误选中栏内文字。
   `cursor`/`user-select` 都是可继承属性，写在父级即可覆盖整棵子树——
   不用 `* { … !important }`（那会顺带把其它过渡也打死）。 */
.sl-home--resizing {
  cursor: col-resize;
  user-select: none;
}
.rail__fixed {
  flex: none;
}
.rail__divider {
  flex: none;
  height: 1px;
  margin: 2px 8px;
  background: var(--h-line);
}
.rail__scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 8px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  padding: 0 8px;
}
.brand__mark {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--h-primary);
  color: var(--h-primary-fg);
  font-size: var(--font-size-md);
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.brand__name {
  font-size: var(--font-size-xl);
  font-weight: 600;
  letter-spacing: 0.2px;
}
/* 折叠开关（展开时在品牌行右侧，折叠后同一颗挪到顶栏搜索框左边）。
   靠 margin-left:auto 顶到行尾 —— 不用 space-between，那会把「SL」和「SciLoop」拆开。 */
.brand > .rail-toggle {
  margin-left: auto;
}
.rail-toggle {
  width: 30px;
  height: 30px;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  /* 用 fg-muted 而不是 fg-subtle：它是这套交互唯一的入口，
     太浅会读成装饰而不是按钮（与左栏导航图标同一档）。 */
  color: var(--h-fg-muted);
  cursor: pointer;
  transition:
    transform var(--motion-dur-fast) var(--motion-ease-out),
    background-color var(--motion-dur) var(--motion-ease),
    border-color var(--motion-dur) var(--motion-ease),
    color var(--motion-dur) var(--motion-ease);
}
.rail-toggle:hover {
  border-color: var(--h-line);
  background: var(--h-surface-input);
  color: var(--h-fg);
}
.rail-toggle:active {
  transform: scale(var(--motion-press));
}
.nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
/* 每行 = 导航项 + 可选的行内操作（如「开始使用」右侧的 ＋） */
.nav__row {
  display: flex;
  align-items: center;
  gap: 4px;
  border-radius: 8px;
  transition: background-color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}
.nav__row:hover {
  background: var(--h-hover);
}
/* 当前所在页：只用中性底色 + 加粗，不用橙色（橙色留给 hover） */
.nav__row--on {
  background: var(--h-hover);
}
.nav__item {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  color: var(--h-fg-muted);
  font-size: var(--font-size-md);
  text-decoration: none;
  transition: color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}
/* 二级入口（模块功能页）：直接是导航项本身，铺满整行 */
.nav--modules {
  padding-top: 2px;
}
/* 分组（文献调研）：父项是按钮，右侧带下角标 */
.nav__block {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav__item--group {
  width: 100%;
  border: 0;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.nav__caret {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  color: var(--h-fg-subtle);
  transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1), color 180ms;
}
.nav__caret--open {
  transform: rotate(180deg);
  color: var(--h-primary);
}
.nav__row:hover .nav__caret {
  color: var(--h-fg);
}
/* 子项：缩进 + 前导小圆点，展开/收起用轻微下滑动效 */
.nav__children {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 0 4px 14px;
}
.nav__item--child {
  padding: 7px 12px;
  font-size: var(--font-size-sm);
  border-radius: 8px;
}
.nav__dot {
  width: 5px;
  height: 5px;
  flex: none;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.5;
  transition: opacity 180ms, transform 180ms cubic-bezier(0.16, 1, 0.3, 1);
}
.nav__item--child:hover .nav__dot,
.nav__item--child.nav__item--on .nav__dot {
  opacity: 1;
  transform: scale(1.2);
}
.nav__item--solo {
  width: 100%;
}
.nav__item--on {
  background: var(--h-hover);
  color: var(--h-fg);
  font-weight: 500;
}
.nav__row:hover .nav__item {
  color: var(--h-fg);
}
.nav__row--on .nav__item {
  color: var(--h-fg);
  font-weight: 500;
}
.nav__plus {
  margin-right: 6px;
}
.nav__row .nav__plus:hover {
  color: var(--h-primary);
}
/* 三个导航图标：默认低饱和，hover 才变橙 */
.nav__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  flex: none;
  color: var(--h-fg-subtle);
  transition:
    color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    transform 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.nav__row:hover .nav__icon {
  color: var(--h-primary);
  transform: translateY(-1px);
}
/* ---------- 未分组 / 项目 / 已归档 分组 ---------- */
.group {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* 可折叠的分组头：把左侧 12px 内边距让给折叠角，**标题位置保持不动**
   （原来是 padding-left:12px，现在 = 0 + 折叠角 12px + gap 0） */
.group__head--foldable {
  padding-left: 0;
  gap: 0;
}

.group__chev {
  width: 12px;
  height: 18px;
  flex: none;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--h-fg-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.group__chev:hover {
  color: var(--h-fg);
}

.group__caret {
  transition: transform 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.group__caret--open {
  transform: rotate(90deg);
}

/* 收起分组：只藏**直接子级**（除标题行外全藏），项目下嵌套的对话归项目自己的箭头管。
   ⚠️ 别按具体类名写（`.crow-block`）：两个分组的下挂结构并不一样 ——
   未分组是 `.crow-block`，项目是 `.crow` + 每个项目各自的 `.fold`；
   按类名写就会"未分组生效、项目纹丝不动"（2026-09-24 实测踩过）。 */
.group--closed > *:not(.group__head) {
  display: none;
}

.group__head {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px 4px 12px;
}

.group__title {
  flex: 1;
  min-width: 0;
  font-size: var(--font-size-xs);
  letter-spacing: 0.4px;
  color: var(--h-fg-subtle);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 「已归档」的折叠头：整行可点，chevron 旋转 */
.group__toggle {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.group__toggle:hover .group__title {
  color: var(--h-fg-muted);
}

.group__count {
  flex: none;
  padding: 0 6px;
  border: 1px solid var(--h-line);
  border-radius: 999px;
  color: var(--h-fg-subtle);
  font-size: var(--font-size-xs);
}

.group__empty {
  margin: 0;
  padding: 4px 12px;
  color: var(--h-fg-subtle);
  font-size: var(--font-size-xs);
}

.group__empty--child {
  padding-left: 26px;
}

/* 对话 / 项目行：hover 才露出行内图标（够得着 + 移开就走） */
.crow {
  display: flex;
  align-items: center;
  gap: 2px;
  border-radius: 8px;
  transition: background-color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

/* 每行外面包一层块，让行内菜单（.fold）落在行的下一行而不是挤进 flex 行 */
.crow-block {
  display: block;
}

.crow:hover {
  background: var(--h-hover);
}

.crow__item {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 7px 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  font-size: var(--font-size-md);
  text-align: left;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}

.crow__item--project {
  font-weight: 500;
  color: var(--h-fg);
}

.crow__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.crow__caret {
  flex: none;
  display: inline-flex;
  align-items: center;
  color: var(--h-fg-subtle);
  transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1);
}

.crow__caret--open {
  transform: rotate(90deg);
  color: var(--h-primary);
}

.crow:hover .crow__item {
  color: var(--h-fg);
}

.crow__acts {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: none;
  padding-right: 6px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.crow:hover .crow__acts,
.crow:focus-within .crow__acts,
.crow--open .crow__acts,
.crow--on .crow__acts,
.crow__acts--always {
  opacity: 1;
  pointer-events: auto;
}

/* 菜单打开时让项目行保持「悬停态」：指针移到行内菜单上会离开行的盒子 */
.crow--open {
  background: var(--h-hover);
}

/* 当前正在看的那条对话：**整块中性泛白**（与「开始使用」同一档 --h-hover）。
   以前是品牌色底 + 左侧 3px 橙条，且高亮只加在标题按钮上 —— 右侧两个行内图标看着"在块外"。
   现在高亮加在 .crow 上（整行，含图标），也不再用品牌色做底。 */
.crow--on {
  background: var(--h-hover);
}

.crow__item--on {
  color: var(--h-fg);
  font-weight: 600;
}

/* 项目行的「更多」菜单：行内块（不做绝对定位浮层，避免被滚动区裁掉） */
.pmenu {
  margin: 2px 0 4px 14px;
  padding: 4px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border: 1px solid var(--h-line-strong);
  border-radius: 10px;
  background: var(--h-surface-raised);
}

/* 项目内对话的菜单：外层 .ckids 已经缩进过，这里不再加左边距（避免双重缩进） */
.pmenu--child {
  margin-left: 0;
}

.pmenu__item {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-sm);
  text-align: left;
  cursor: pointer;
  transition:
    background-color 160ms cubic-bezier(0.4, 0, 0.2, 1),
    color 160ms cubic-bezier(0.4, 0, 0.2, 1);
}

.pmenu__item:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

/* 「已归档」里的二级小标题（项目 / 对话）：复用分组标题的排版，只调缩进 */
.group__title--nested {
  padding: 4px 12px 2px 16px;
}

/* 已归档项目展开后的对话：比项目再缩进一级 */
.ckids--deep {
  margin-left: 14px;
}

/* 项目下的对话：缩进一级 */
.ckids {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-left: 14px;
}

.crow--child .crow__item {
  font-size: var(--font-size-sm);
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--h-fg-subtle);
  cursor: pointer;
  transition:
    background-color 180ms cubic-bezier(0.4, 0, 0.2, 1),
    color 180ms cubic-bezier(0.4, 0, 0.2, 1);
}

.icon-btn:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}

/* ---------- 主区 ---------- */
.main {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.topbar {
  height: 64px;
  flex: none;
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 32px;
  border-bottom: 1px solid var(--h-line);
  /* 研究流程抽屉滑出时，右侧按钮组跟着左移 —— 否则主题/任务/入口会被抽屉盖住点不到 */
  padding-right: calc(32px + var(--rfd-shift, 0px));
  transition: padding-right 340ms var(--motion-ease-out);
}
.search {
  flex: 1;
  max-width: 560px;
  display: flex;
  align-items: center;
  gap: 12px;
  height: 40px;
  padding: 0 16px;
  background: var(--h-surface-input);
  border: 1px solid var(--h-line);
  border-radius: 12px;
  color: var(--h-fg-subtle);
}
.search:focus-within {
  border-color: var(--h-line-strong);
}
.search input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--h-fg);
  font: inherit;
  outline: none;
}
.search input::placeholder {
  color: var(--h-fg-subtle);
}
.topbar__spacer {
  flex: 1;
}

/* 无框图标按钮（与旁边带框的主题/任务按钮区分：这里刻意不加边框与底色） */
.panel-entry {
  position: absolute;
  /* 顶栏高 64px：入口贴在顶栏下方的内容区右上角 */
  top: 78px;
  right: calc(20px + var(--rfd-shift, 0px));
  z-index: 5;
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 0;
  background: transparent;
  color: var(--h-fg-muted);
  cursor: pointer;
  transition: color 160ms var(--motion-ease), right 340ms var(--motion-ease-out);
}

.panel-entry:hover {
  color: var(--h-fg);
}
.theme-toggle {
  width: 36px;
  height: 36px;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--h-line);
  border-radius: 40px;
  background: transparent;
  color: var(--h-fg-muted);
  cursor: pointer;
}
.theme-toggle:hover {
  border-color: var(--h-line-strong);
  color: var(--h-fg);
}
/* 任务按钮：有任务在跑时发亮（品牌色描边 + 光晕）并让图标旋转 */
.theme-toggle--task svg {
  transition: transform 200ms cubic-bezier(0.16, 1, 0.3, 1);
}
.theme-toggle--task:hover svg {
  transform: rotate(-30deg);
}
.theme-toggle--running,
.theme-toggle--running:hover {
  color: var(--h-primary);
  border-color: var(--h-primary);
  box-shadow: 0 0 14px var(--h-primary);
}
.theme-toggle--running svg {
  animation: task-spin 1600ms linear infinite;
}
.theme-toggle--running:hover svg {
  transform: none;
}
@keyframes task-spin {
  to {
    transform: rotate(360deg);
  }
}
.avatar-wrap {
  position: relative;
}
.avatar {
  width: 36px;
  height: 36px;
  border-radius: 40px;
  background: var(--h-secondary);
  color: var(--color-text-inverse); /* ui-polish-allow: 品牌底上的白字 */
  font-size: var(--font-size-md);
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--h-line-strong);
  cursor: pointer;
  transition: transform 180ms cubic-bezier(0.4, 0, 0.2, 1);
}
.avatar-wrap:hover .avatar {
  transform: scale(1.04);
}
.menu {
  position: absolute;
  top: 48px;
  right: 0;
  width: 208px;
  padding: 6px;
  background: var(--h-surface-raised);
  border: 1px solid var(--h-line-strong);
  border-radius: 12px;
  opacity: 0;
  visibility: hidden;
  transform: translateY(-4px);
  transition:
    opacity 180ms cubic-bezier(0.4, 0, 0.2, 1),
    transform 180ms cubic-bezier(0.4, 0, 0.2, 1),
    visibility 180ms,
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    border-color 300ms cubic-bezier(0.4, 0, 0.2, 1);
  z-index: 20;
}
/* 悬停**和键盘聚焦**都能开菜单（原来只有 :hover —— 头像有 tabindex 但键盘用户永远进不去菜单） */
.avatar-wrap:hover .menu,
.avatar-wrap:focus-within .menu,
.menu:hover {
  opacity: 1;
  visibility: visible;
  transform: translateY(0);
}
.menu__item {
  width: 100%;
  padding: 10px 12px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--h-fg-muted);
  font: inherit;
  font-size: var(--font-size-md);
  text-align: left;
  cursor: pointer;
}
.menu__item:hover {
  background: var(--h-hover);
  color: var(--h-fg);
}
.menu__sep {
  height: 1px;
  margin: 6px 4px;
  background: var(--h-line);
}
.menu__meta {
  padding: 6px 12px;
  font-size: var(--font-size-xs);
  color: var(--h-fg-subtle);
}
/* 内容区：左栏常驻，只有中间这块随路由切换；也是全站唯一的一级滚动容器 */
.content {
  flex: 1;
  min-width: 0;
  min-height: 0; /* flex 子项默认可被内容撑高，必须归零才能让 overflow 生效 */
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  /* 研究流程抽屉滑出时，把它的宽度从内容区里让出来 —— 子元素（正文列）据此重新定位 */
  padding-right: var(--rfd-shift, 0px);
  transition: padding-right 340ms var(--motion-ease-out);
}

/* 非首页（文献调研 / 知识库 / 工作台等）：铺满 + 与原模块壳层一致的内边距 */
.content--page {
  align-items: stretch;
  padding: var(--space-5);
}

@media (max-width: 900px) {
  .rail {
    display: none;
  }
}
</style>