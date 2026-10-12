/** User-facing tool details. Keep the proposed action, never protocol identifiers. */
export function actionDetails(value: Record<string, unknown>): string {
  const rows: string[] = []
  const fields: Record<string, string> = {
    argv: '命令', command: '命令', cwd: '工作目录', workdir: '工作目录',
    path: '文件', source: '来源', destination: '目标', content: '写入内容',
    text: '内容', task: '任务', message: '消息', name: '名称', query: '搜索内容', url: '地址',
  }
  for (const [key, label] of Object.entries(fields)) {
    const item = value[key]
    if (item === undefined || item === null) continue
    rows.push(`${label}：${Array.isArray(item) ? item.map(String).join(' ') : String(item)}`)
  }
  return rows.join('\n') || '执行上方所述操作'
}
