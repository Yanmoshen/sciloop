/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * 类型声明：Vite 客户端环境 + .vue 单文件组件模块。
 */
/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string
  readonly VITE_DEFAULT_THEME?: 'light' | 'dark'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
