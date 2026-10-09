/**
 * Node ESM 解析钩子：让编译产物能按**仓库既有风格**（相对导入不写扩展名）被加载。
 *
 * 背景：仓库里所有相对导入都是 extensionless（`from './protocol'`），Vite 负责解析。
 * 但 Node 的 ESM 加载器要求显式扩展名。与其在每个源文件里加 `.js`（破坏仓库风格），
 * 不如在测试这一侧兜住：凡是「相对路径且没有扩展名」的说明符，先尝试补 `.js`。
 */

export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('.') && !/\.[cm]?[jt]s$/.test(specifier)) {
    try {
      return await nextResolve(`${specifier}.js`, context)
    } catch {
      // 落到默认解析（目录 index.js、无扩展名文件等情形）
    }
  }
  return nextResolve(specifier, context)
}
