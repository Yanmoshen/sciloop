#!/usr/bin/env node
/**
 * agent-v2 前端测试入口（零新增依赖）。
 *
 * 思路：仓库里已经有 `typescript`（web 的 devDependency），所以直接用 `tsc` 把
 * 「协议层 + reducer + 客户端」编译成 ESM，再用本目录的极简断言集（`_harness.mjs`）跑断言
 * ——不需要 vitest / jsdom / @types/node。
 *
 * 测试文件本身是普通的 `.mjs`，**不参与编译**，只 import 编译产物。
 *
 * 用法：
 *   node web/tests/agent-v2/run.mjs
 *   TSC=/abs/path/to/tsc.js node web/tests/agent-v2/run.mjs
 *
 * 退出码：0 全过；1 有失败；2 环境不满足（找不到 typescript / 编译失败）。
 */

import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { register } from 'node:module'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { runSuites } from './_harness.mjs'

// 仓库的相对导入不写扩展名（Vite 负责解析）；Node 需要显式扩展名，这里用解析钩子兜住。
register('./_resolve-hook.mjs', import.meta.url)

const HERE = dirname(fileURLToPath(import.meta.url))
const WEB_ROOT = resolve(HERE, '..', '..')
//: 编译产物目录（由本目录 .gitignore 忽略；失败时保留以便排查）
const BUILD_DIR = join(HERE, '.test-build')
const SUITES = ['protocol', 'cursor', 'recovery', 'reducer', 'client']

function findTypeScript() {
  if (process.env.TSC && existsSync(process.env.TSC)) return process.env.TSC
  const tscIn = (webRoot) => join(webRoot, 'node_modules', 'typescript', 'lib', 'tsc.js')
  const candidates = [
    tscIn(WEB_ROOT),
    // 在仓库根目录安装依赖的情况
    tscIn(resolve(WEB_ROOT, '..')),
    // 并行 worktree：主工作区（<父目录>/aicoding竞赛/web）已装过依赖就直接复用
    tscIn(resolve(WEB_ROOT, '..', '..', '..', 'aicoding竞赛', 'web')),
  ]
  return candidates.find((candidate) => existsSync(candidate)) ?? null
}

function compile(tscPath) {
  try {
    execFileSync(process.execPath, [tscPath, '-p', join(HERE, 'tsconfig.json')], {
      stdio: 'inherit',
      cwd: WEB_ROOT,
    })
    return true
  } catch (error) {
    console.error('[agent-v2] 编译失败：', error.message)
    return false
  }
}

async function main() {
  const tsc = findTypeScript()
  if (!tsc) {
    console.error(
      '[agent-v2] 找不到 typescript。请先在 web/ 执行 npm ci，或用 TSC=<tsc.js 路径> 指定。',
    )
    process.exit(2)
  }
  if (!compile(tsc)) process.exit(2)

  const loaded = []
  for (const name of SUITES) {
    loaded.push(await import(pathToFileURL(join(HERE, `${name}.test.mjs`)).href))
  }

  // 注意：不在这里删除 `.test-build/`。
  // 一是删除在部分环境会被安全钩子拦截（本项目已知问题），
  // 二是该目录属于编译产物，已由本目录 .gitignore 忽略，留着便于失败后排查。
  process.exit(await runSuites(loaded))
}

await main()
