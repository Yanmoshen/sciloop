/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 路由表（WP01 独占维护，其他工作包**禁止**直接修改本文件；新增路由须回到 WP01 变更）。
 * 全部路由在 WP01 阶段即预置占位视图，保证导航不跳页、构建期懒加载路径可解析。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import HomeLayout from '@/layouts/HomeLayout.vue'

export interface NavItem {
  /** 导航 key（与 module 一一对应） */
  key: string
  /** 左栏显示名 */
  label: string
  /** 目标路径（含占位参数） */
  path: string
  /** 一句话说明 */
  hint: string
}

/** 模块内的导航项（**同级并列**） */
export interface NavItem {
  /** 导航 key（与 module 一一对应） */
  key: string
  /** 左栏显示名 */
  label: string
  /** 目标路径（含占位参数） */
  path: string
  /** 一句话说明 */
  hint: string
}

/** 模块分组：每个模块只展示自己的页面，互不混排 */
export interface ModuleGroup {
  key: string
  /** 侧栏左上角的大字标题 */
  title: string
  items: NavItem[]
}

/**
 * 模块与其侧栏导航。
 *
 * 「文献总览」（`/papers`）仍在本地预览、未落地，所以暂时以「论文库」作为
 * 文献调研的落地页；该页落地后插到 `literature.items` 最前面即可。
 */
export const MODULE_GROUPS: ModuleGroup[] = [
  {
    key: 'literature',
    title: '文献调研',
    items: [
      { key: 'papers', label: '文献总览', path: '/papers', hint: '论文库总览与解析入口' },
      { key: 'feed', label: '论文库', path: '/papers/feed', hint: '三视图推荐（口径分离）' },
      { key: 'parse', label: '论文解析', path: '/papers/parse', hint: '解析首屏：最近解析与聚合' },
      {
        key: 'aggregate',
        label: '聚合对比',
        path: '/papers/aggregate/demo',
        hint: '对比矩阵 + 方法演进 + 空白',
      },
    ],
  },
  {
    key: 'ideation',
    title: '研究构思',
    items: [{ key: 'ideas', label: '研究构思', path: '/ideas', hint: 'idea 生成与证据抽屉' }],
  },
  {
    key: 'pipeline',
    title: '流水线',
    items: [
      {
        key: 'workbench',
        label: '流水线工作台',
        path: '/workbench/demo',
        hint: '六环节看板 + 决策日志',
      },
    ],
  },
  {
    key: 'knowledge',
    title: '知识库',
    items: [{ key: 'knowledge', label: '知识库', path: '/knowledge', hint: '文件 / 摘录 / 技能 / 记忆' }],
  },
  {
    key: 'skills',
    title: '技能',
    items: [{ key: 'skills', label: '技能库', path: '/skills', hint: '装了什么 / 开关 / 挂载 / 自己写' }],
  },
  {
    key: 'settings',
    title: '设置',
    items: [
      {
        key: 'settings',
        label: '设置',
        path: '/settings',
        hint: '模型供应商 / 路由 / 成本 / 数据源健康',
      },
    ],
  },
]

/** 按模块 key 取分组（取不到时回落到文献调研） */
export function moduleGroup(key: string | undefined): ModuleGroup {
  return MODULE_GROUPS.find((group) => group.key === key) ?? MODULE_GROUPS[0]!
}

/**
 * 单一壳层（HomeLayout）：左侧导航栏**常驻不变**（开始使用 / 文献调研 / 知识库 + 最近打开），
 * 点不同入口只切换中间内容区，因此**不需要返回箭头**之类的层级导航。
 *
 * `homeNav` 决定左栏哪一项高亮；未标注的页面（研究构思 / 工作台 / 设置）不高亮任何一项。
 */
export const routes: RouteRecordRaw[] = [
  // ---- 独立整页阅读器（不带壳层，整屏只放内容）----
  // 知识库里「在新页面打开」落到这里（新标签页），因此它必须是**顶层路由**，
  // 否则会被 HomeLayout 包住、左栏导航还在，"全屏"就名不副实。
  {
    path: '/knowledge/read/:entryId',
    name: 'knowledge-read',
    component: () => import('@/views/KnowledgeReaderPage.vue'),
    meta: { title: '阅读' },
  },
  {
    path: '/',
    component: HomeLayout,
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/views/HomeView.vue'),
        meta: { title: '开始使用', homeNav: 'home' },
      },
      // 打开左栏里的某条对话（同一个视图，靠路由参数区分「新对话」与「已有对话」）。
      // `?project=<id>` 只对 path 为空的「新对话」生效：表示这条新对话建在该项目下。
      {
        path: 'c/:conversationId',
        name: 'conversation',
        component: () => import('@/views/HomeView.vue'),
        meta: { title: '对话', homeNav: 'home' },
      },
      // ---- 文献调研：/papers/* ----
      {
        path: 'papers',
        name: 'papers',
        component: () => import('@/views/PapersView.vue'),
        meta: { title: '文献总览', module: 'papers', homeNav: 'literature' },
      },
      {
        path: 'papers/feed',
        name: 'feed',
        component: () => import('@/views/FeedView.vue'),
        meta: { title: '论文库', module: 'feed' },
      },
      {
        path: 'papers/parse',
        name: 'parse-home',
        component: () => import('@/views/ParseHomeView.vue'),
        meta: { title: '论文解析', module: 'parse', homeNav: 'literature' },
      },
      // 旧的占位路径（导航曾直接指向 /papers/parse/demo，会被 :paperId 当成论文 id 吞掉）
      {
        path: 'papers/parse/demo',
        redirect: { path: '/papers/parse' },
      },
      {
        path: 'papers/parse/:paperId',
        name: 'parse',
        component: () => import('@/views/ParseView.vue'),
        meta: { title: '论文解析', module: 'parse' },
      },
      {
        path: 'papers/aggregate/:id',
        name: 'aggregate',
        component: () => import('@/views/AggregateView.vue'),
        meta: { title: '聚合对比', module: 'aggregate' },
      },
      // ---- 知识库 ----
      // 2026-09-22：入口交给用户自己管的知识库（文件 / 论文与 Idea 摘录 / 技能 / 记忆）。
      // 原「知识资产」四页签（论文卡片 / 证据 / 决策 / Passport）不在导航里出现了，
      // 组件 `KnowledgeView.vue` 保留在仓库中未删，需要时再挂回一条路由即可。
      {
        path: 'knowledge',
        name: 'knowledge',
        component: () => import('@/views/KnowledgeBaseView.vue'),
        meta: { title: '知识库', module: 'knowledge', homeNav: 'knowledge' },
      },
      // 技能库（2026-09-23）：独立入口，与知识库平级
      {
        path: 'skills',
        name: 'skills',
        component: () => import('@/views/SkillsView.vue'),
        meta: { title: '技能库', module: 'skills', homeNav: 'skills' },
      },
      // ---- 四个核心模块（EasyPaper 风格，本轮前端对接）----
      // 论文导入已并入「文献总览」的弹窗（2026-09-20）：旧路径保留为重定向，
      // 免得旧书签 / 演示脚本里的 /papers/import 直接 404。
      {
        path: 'papers/import',
        name: 'import',
        redirect: { path: '/papers', query: { import: '1' } },
        meta: { title: '论文导入', module: 'papers', homeNav: 'literature' },
      },
      {
        path: 'papers/translate',
        name: 'translate',
        component: () => import('@/views/TranslateView.vue'),
        meta: { title: '论文翻译', module: 'translate' },
      },
      {
        path: 'papers/reader',
        name: 'reader',
        component: () => import('@/views/ReaderView.vue'),
        meta: { title: '全文阅读', module: 'reader' },
      },
      {
        path: 'papers/reader/:documentId',
        name: 'reader-document',
        component: () => import('@/views/ReaderView.vue'),
        meta: { title: '全文阅读', module: 'reader' },
      },
      {
        path: 'papers/export',
        name: 'export',
        component: () => import('@/views/ExportView.vue'),
        meta: { title: '多格式导出', module: 'export' },
      },
      // ---- 其余工作页（从左栏「最近打开」、卡片或头像菜单进入；左栏保持常驻）----
      {
        path: 'ideas',
        name: 'ideas',
        component: () => import('@/views/IdeaView.vue'),
        meta: { title: '研究构思', module: 'ideas' },
      },
      {
        path: 'workbench/:projectId',
        name: 'workbench',
        component: () => import('@/views/WorkbenchView.vue'),
        meta: { title: '流水线工作台', module: 'workbench' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SettingsView.vue'),
        meta: { title: '设置', module: 'settings' },
      },
      // ---- 旧路径兼容（书签/历史链接不失效）----
      { path: 'feed', redirect: '/papers/feed' },
      { path: 'parse/:paperId', redirect: (to) => `/papers/parse/${to.params.paperId}` },
      { path: 'aggregate/:id', redirect: (to) => `/papers/aggregate/${to.params.id}` },
      {
        // 兜底：未知路径回首页（不跳页、不报错）
        path: ':pathMatch(.*)*',
        name: 'not-found',
        redirect: '/',
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  const title = (to.meta?.title as string | undefined) ?? ''
  document.title = title ? `${title} · SciLoop` : 'SciLoop · AI 科研工作台'
})

export default router
