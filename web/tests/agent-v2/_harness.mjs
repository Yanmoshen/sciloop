/**
 * 极简断言与测试收集器（零依赖）。
 *
 * 为什么不用 vitest / node:test：前端测试只需要「断言 + 汇总」两件事，
 * 引入测试框架还会连带 @types/node、jsdom 之类的配置成本。这里几十行搞定，
 * 任何 Node ≥ 18 都能跑。
 *
 * 每个套件模块导出 `register()`，在里面调用 `test(name, fn)` 注册用例。
 */

const cases = []

export function test(name, fn) {
  cases.push({ name, fn })
}

export class AssertionError extends Error {}

function fail(message) {
  throw new AssertionError(message)
}

function show(value) {
  if (typeof value === 'string') return JSON.stringify(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

export function assert(condition, message = '断言失败') {
  if (!condition) fail(message)
}

export function assertEqual(actual, expected, message = '') {
  if (actual !== expected) {
    fail(`${message || '值不相等'}：期望 ${show(expected)}，实际 ${show(actual)}`)
  }
}

export function assertDeepEqual(actual, expected, message = '') {
  const left = JSON.stringify(actual)
  const right = JSON.stringify(expected)
  if (left !== right) {
    fail(`${message || '结构不相等'}：期望 ${right}，实际 ${left}`)
  }
}

export function assertThrows(fn, message = '期望抛异常但没有抛') {
  try {
    fn()
  } catch {
    return
  }
  fail(message)
}

/** 跑所有套件，返回进程退出码（0 全过 / 1 有失败）。支持异步用例。 */
export async function runSuites(suiteModules) {
  for (const module of suiteModules) {
    if (typeof module.register === 'function') module.register()
  }
  let passed = 0
  const failures = []
  const started = Date.now()
  for (const item of cases) {
    try {
      await item.fn()
      passed += 1
    } catch (error) {
      failures.push({ name: item.name, error })
    }
  }
  const elapsed = Date.now() - started
  for (const failure of failures) {
    console.error(`FAIL  ${failure.name}`)
    console.error(`      ${failure.error && failure.error.message}`)
  }
  console.log(`agent-v2 前端测试：${passed} passed, ${failures.length} failed (${elapsed} ms)`)
  return failures.length === 0 ? 0 : 1
}

export async function wait(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms))
}
