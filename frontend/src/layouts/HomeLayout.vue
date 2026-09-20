<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 总览壳层（双壳层的第一层）：左栏（品牌 / 开始使用·文献调研·知识库 / 最近打开）
 * + 顶部（搜索 / 颜色切换 / 圆形头像悬浮菜单）+ 总览内容。
 *
 * 设计语言：styles/home-theme.css（n8n design language，仅作用于 `.sl-home`）。
 * 主题沿用 session store 的 `sciloop.theme`（localStorage 持久化），此处不再自造一份。
 * 进入项目后由 ShellLayout（模块壳层）接管，7 个模块页保持原样。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import type { CreatedProject } from '@/api/projects'
import ProjectCreateDialog from '@/components/ProjectCreateDialog.vue'
import ProjectDetailDialog from '@/components/ProjectDetailDialog.vue'
import ProjectRenameDialog from '@/components/ProjectRenameDialog.vue'
import TaskHistoryDialog from '@/components/TaskHistoryDialog.vue'
import TaskMonitorDialog from '@/components/TaskMonitorDialog.vue'
import { useSessionStore } from '@/stores/session'
import { useTaskStore } from '@/stores/tasks'

const session = useSessionStore()
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
      { key: 'parse', label: '论文解析', path: '/papers/parse/demo' },
      { key: 'aggregate', label: '聚合对比', path: '/papers/aggregate/demo' },
      { key: 'export', label: '多格式导出', path: '/papers/export' },
    ],
  },
  { key: 'knowledge', label: '知识库', path: '/knowledge' },
]

/** 左栏二级入口（不在分组里的功能页）；设置由右上角头像的悬浮菜单进入 */
const MODULE_NAV = [
  { key: 'ideas', label: '研究构思', path: '/ideas' },
  { key: 'workbench', label: '流水线工作台', path: '/workbench/demo' },
]

const keyword = ref('')

/** 最近打开：真实项目列表（后端按 is_demo 优先返回），不做本地假数据 */
const recent = computed(() =>
  [...session.projects]
    .sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')))
    .slice(0, 6),
)

/** 当前高亮的主入口；未标注 homeNav 的页面不高亮 */
const activeKey = computed(() => (route.meta?.homeNav as string | undefined) ?? '')
/** 当前高亮的模块页（二级入口） */
const activeModule = computed(() => (route.meta?.module as string | undefined) ?? '')

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

function openProject(id: number): void {
  session.selectProject(id)
  void router.push({ name: 'workbench', params: { projectId: String(id) } })
}

function openSettings(): void {
  void router.push({ path: '/settings' })
}

// ---- 新建研究项目（导航「开始使用」右侧的 + 号） ----
const createOpen = ref(false)

function openCreate(): void {
  createOpen.value = true
}

/** 创建成功：刷新项目列表并直接进入该项目的流水线工作台 */
function onCreated(project: CreatedProject): void {
  void session.loadProjects()
  session.selectProject(project.id)
  void router.push({ name: 'workbench', params: { projectId: String(project.id) } })
}

// ---- 同步任务历史（顶栏按钮打开）----
const historyOpen = ref(false)

// ---- 最近打开：每行的重命名 / 详情 ----
const renameOpen = ref(false)
const renameTargetId = ref<number | null>(null)
const renameTargetName = ref('')

const detailOpen = ref(false)
const detailTargetId = ref<number | null>(null)

function openRename(project: { id: number; name: string }): void {
  renameTargetId.value = project.id
  renameTargetName.value = project.name
  renameOpen.value = true
}

function openDetail(id: number): void {
  detailTargetId.value = id
  detailOpen.value = true
}

// ---- 任务：顶栏按钮 → 历史 → 打开某一条的完整监控窗口 ----
const tasks = useTaskStore()
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
        ? 'public_demo 只读面无法触发抓取（服务端 403）：请在「设置」页填入 OWNER_TOKEN 后重试。'
        : (result.message ?? '触发抓取失败'),
    )
  }
}

onMounted(() => {
  void session.loadProjects()
  void session.loadHealth()
  tasks.startWatch()
})

onUnmounted(() => {
  tasks.stopWatch()
  tasks.stopPolling()
})
</script>

<template>
  <div class="sl-home">
    <aside class="rail">
      <div class="brand">
        <div class="brand__mark">SL</div>
        <div class="brand__name">SciLoop</div>
      </div>

      <nav class="nav" aria-label="总览导航">
        <template v-for="item in HOME_NAV" :key="item.key">
          <!-- 可展开分组：文献调研 -->
          <div v-if="item.children" class="nav__block">
            <div class="nav__row" :class="{ 'nav__row--on': literatureActive }">
              <button
                class="nav__item nav__item--group"
                type="button"
                :title="literatureOpen ? '收起文献调研' : '展开文献调研'"
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

            <div v-show="literatureOpen" class="nav__children">
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
              title="新建研究项目"
              aria-label="新建研究项目"
              @click="openCreate"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              </svg>
            </button>
          </div>
        </template>
      </nav>

      <div class="nav__divider" />

      <nav class="nav nav--modules" aria-label="模块导航">
        <RouterLink
          v-for="item in MODULE_NAV"
          :key="item.key"
          class="nav__item nav__item--solo"
          :class="{ 'nav__item--on': activeModule === item.key }"
          :to="item.path"
        >
          <span class="nav__icon">
            <!-- 研究构思 -->
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

      <div class="recent">
        <div class="recent__title">最近打开</div>
        <div v-for="p in recent" :key="p.id" class="recent__row">
          <button
            type="button"
            class="recent__item"
            :title="p.name"
            @click="openProject(p.id)"
          >
            {{ p.name }}
          </button>
          <span class="recent__actions">
            <button
              class="icon-btn"
              type="button"
              title="重命名"
              aria-label="重命名"
              @click="openRename(p)"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M11.4 2.6a1.35 1.35 0 0 1 1.9 1.9l-7.6 7.6-2.7.8.8-2.7 7.6-7.6Z"
                  stroke="currentColor"
                  stroke-width="1.3"
                  stroke-linejoin="round"
                />
                <path d="M10.3 3.7 12.3 5.7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
              </svg>
            </button>
            <button
              class="icon-btn"
              type="button"
              title="项目详情"
              aria-label="项目详情"
              @click="openDetail(p.id)"
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="M2.6 3.9h10.8M2.6 6.6h6.8M2.6 9.4h10.8M2.6 12.1h4.6"
                  stroke="currentColor"
                  stroke-width="1.3"
                  stroke-linecap="round"
                />
              </svg>
            </button>
          </span>
        </div>
        <p v-if="recent.length === 0" class="recent__empty">
          {{ session.projectsError || '暂无项目' }}
        </p>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="search">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="4.6" stroke="currentColor" stroke-width="1.4" />
            <path d="M10.6 10.6 14 14" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          </svg>
          <input
            v-model="keyword"
            type="search"
            placeholder="搜索论文 / 项目 / 决策记录"
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
          <div class="avatar" tabindex="0" :title="session.accessLabel">Y</div>
          <div class="menu" role="menu">
            <button class="menu__item" type="button" role="menuitem" @click="openSettings">设置</button>
            <div class="menu__sep" />
            <div class="menu__meta">
              {{ session.isOwner ? 'owner_mode（可写）' : 'public_demo（只读）' }}
            </div>
          </div>
        </div>
      </header>

      <main class="content" :class="{ 'content--page': route.name !== 'home' }">
        <RouterView />
      </main>
    </div>

    <ProjectCreateDialog v-model="createOpen" @created="onCreated" />
    <ProjectRenameDialog
      v-model="renameOpen"
      :project-id="renameTargetId"
      :project-name="renameTargetName"
    />
    <ProjectDetailDialog v-model="detailOpen" :project-id="detailTargetId" />
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
.rail {
  width: 248px;
  flex: none;
  overflow-y: auto; /* 导航过长时内部滚动；滚动条视觉由 styles/scrollbar.css 全局隐藏 */
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 32px;
  background: var(--h-surface);
  border-right: 1px solid var(--h-line);
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
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
  animation: nav-unfold 200ms cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes nav-unfold {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: none;
  }
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
.nav__divider {
  height: 1px;
  margin: 4px 12px 8px;
  background: var(--h-line);
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
.recent {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.recent__title {
  padding: 0 12px 8px;
  font-size: var(--font-size-xs);
  letter-spacing: 0.4px;
  color: var(--h-fg-subtle);
}
.recent__row {
  display: flex;
  align-items: center;
  gap: 4px;
  border-radius: 8px;
}

.recent__row:hover {
  background: var(--h-hover);
}

.recent__item {
  flex: 1;
  min-width: 0;
  padding: 8px 12px;
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
  transition:
    background-color 300ms cubic-bezier(0.4, 0, 0.2, 1),
    color 300ms cubic-bezier(0.4, 0, 0.2, 1);
}

.recent__row:hover .recent__item {
  color: var(--h-fg);
}

/* 每行项目名右侧两个线性图标（重命名 / 详情），与顶栏项目区的图标同一套样式 */
.recent__actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: none;
  padding-right: 6px;
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
.recent__empty {
  margin: 0;
  padding: 0 12px;
  font-size: var(--font-size-xs);
  color: var(--h-fg-subtle);
}

/* ---------- 主区 ---------- */
.main {
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
  color: #ffffff; /* ui-polish-allow: 品牌底上的白字 */
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
.avatar-wrap:hover .menu,
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