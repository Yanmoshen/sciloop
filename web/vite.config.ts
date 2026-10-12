/**
 * Copyright 2026 SciLoop contributors
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 */
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig, type Plugin } from 'vite'

/**
 * `index.html` 用 `%VITE_DEFAULT_THEME%` 做首屏前的主题预置（避免深色模式闪白）。
 * Docker 构建由 Dockerfile 的 `ENV VITE_DEFAULT_THEME` 提供该值；本地构建若未设置，
 * Vite 只会把占位符原样留在产物里并打 warning。这里补一个构建期默认值，
 * 使「本机构建」与「容器构建」得到同一份产物，不新增 `.env` 文件（`.env` 禁止入库）。
 */
process.env.VITE_DEFAULT_THEME ??= 'light'

function buildIdentity(): Plugin {
  return {
    name: 'sciloop-build-identity',
    generateBundle(_options: unknown, bundle: Record<string, { type: string }>) {
      const assets = Object.keys(bundle).sort()
      this.emitFile({
        type: 'asset',
        fileName: 'build-manifest.json',
        source: JSON.stringify(
          {
            build_id: process.env.VITE_BUILD_ID ?? new Date().toISOString(),
            source_revision: process.env.VITE_SOURCE_REVISION ?? 'working-tree',
            assets,
          },
          null,
          2,
        ),
      })
    },
  }
}

export default defineConfig({
  plugins: [vue(), buildIdentity()],
  resolve: {
    alias: {
      // 全项目统一使用 @ -> src 别名（WP02 等已按此约定引用）
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    // The application serves the built frontend and API together on 8000.
    // Keep Vite on the same fixed port so it cannot silently create another port.
    port: 8000,
    strictPort: true,
    proxy: {
      // 本地开发同源转发到 FastAPI，SSE 需关闭缓冲
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 8000,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
})
