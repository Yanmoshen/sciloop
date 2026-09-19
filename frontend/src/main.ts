/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
// Element Plus 暗色变量表（挂在 html.dark 下）——缺它时深色模式下所有 el-* 组件仍是浅色
import 'element-plus/theme-chalk/dark/css-vars.css'

import App from '@/App.vue'
import router from '@/router'
import { installGlobalTooltip } from '@/utils/globalTooltip'
import '@/styles/tokens.css'
// 总览页专用设计层（n8n design language，作用域限定在 .sl-home，不影响模块页）
import '@/styles/home-theme.css'
// 全站换色过渡层（浅色 ↔ 深色渐变切换，模块页同样生效）
import '@/styles/motion.css'
// 全局 tooltip 浮层样式（接管原生 title 提示）
import '@/styles/tooltip.css'
// 无障碍基线（键盘焦点环 + 减少动效降级），必须排在 motion.css 之后
import '@/styles/a11y.css'
// 全站滚动条统一样式（给滚动容器加 class="scroll-y" 即可，别再各写一份）
import '@/styles/scrollbar.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
// Element Plus 全局注册（同伴组件按需使用 el-* 组件，无需各自 import 插件）
app.use(ElementPlus, { locale: zhCn })

// 接管所有 title 原生提示（两套壳层、所有组件通用）
installGlobalTooltip()

app.mount('#app')
