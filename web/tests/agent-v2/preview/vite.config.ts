/**
 * 预览构建配置（只用于验收取证，不进入生产构建）。
 *
 * `root` 指向本目录，`outDir` 输出到 `.preview`（已被本目录 .gitignore 忽略）。
 * 依赖从 `web/node_modules` 解析（构建前确保已安装或在同一机器上存在）。
 */

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  build: {
    outDir: '.preview',
    // 不清理输出目录：本机删除会被安全钩子拦截（项目已知约束），
    // 产物文件名带哈希，index.html 始终指向最新的一份。
    emptyOutDir: false,
    chunkSizeWarningLimit: 4000,
  },
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('../../../src', import.meta.url)),
    },
  },
})
