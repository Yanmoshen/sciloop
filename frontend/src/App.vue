<script setup lang="ts">
/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * 应用根组件：只负责主题落地与路由出口，布局在 ShellLayout 内。
 */
import { onMounted } from 'vue'
import { RouterView } from 'vue-router'

import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

onMounted(() => {
  // 首屏已由 index.html 内联脚本预设 data-theme，这里同步 pinia 状态并探测后端
  session.applyTheme(session.theme)
  void session.loadHealth()
})
</script>

<template>
  <RouterView />
</template>
