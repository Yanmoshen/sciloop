/**
 * 事件游标与乱序缓冲（WP-03 的 `cursor.ts`）。
 *
 * 单独成模块的原因：去重、乱序、缺口这三件事是**纯算法**，与业务渲染无关，
 * 拆出来可以单独测（`web/tests/agent-v2/cursor.test.mjs`），reducer 只负责编排。
 *
 * 三条规则：
 * 1. `sequence <= cursor`：重复，丢弃；
 * 2. `sequence > cursor + 1`：乱序，进入缓冲并暴露缺口起点；
 * 3. 缓冲里与游标连续的事件要一次性排空（保证严格按序应用）。
 */

/** 判定是否为重复事件。 */
export function isDuplicate(sequence: number, cursor: number): boolean {
  return sequence <= cursor
}

/** 判定是否出现缺口（乱序）。 */
export function isOutOfOrder(sequence: number, cursor: number): boolean {
  return sequence > cursor + 1
}

export interface DrainResult<T> {
  /** 按序应用的事件。 */
  applied: T[]
  appliedSequences: number[]
  /** 剩下的缓冲。 */
  remaining: Record<number, T>
  /** 排空后的游标。 */
  cursor: number
  /** 仍然存在的缺口起点（null 表示连续）。 */
  gapFrom: number | null
}

/**
 * 从 `cursor` 之后按序排空缓冲。
 *
 * @param start 刚应用的事件序号（排空的起点）
 * @param cursor 当前游标
 * @param pending 缓冲（不会被就地修改）
 */
export function drainContiguous<T>(
  pending: Record<number, T>,
  start: number,
  cursor: number,
): DrainResult<T> {
  const applied: T[] = []
  const appliedSequences: number[] = []
  const remaining: Record<number, T> = { ...pending }
  let expected = start + 1
  let current = cursor
  while (remaining[expected] !== undefined) {
    applied.push(remaining[expected])
    appliedSequences.push(expected)
    delete remaining[expected]
    current = expected
    expected += 1
  }
  const hasPendingAhead = Object.keys(remaining).some((key) => Number(key) > current)
  return {
    applied,
    appliedSequences,
    remaining,
    cursor: current,
    gapFrom: hasPendingAhead ? current + 1 : null,
  }
}

/** 把事件放进缓冲（重复的不覆盖）。 */
export function bufferEvent<T>(
  pending: Record<number, T>,
  sequence: number,
  event: T,
): { pending: Record<number, T>; duplicated: boolean } {
  if (pending[sequence] !== undefined) {
    return { pending, duplicated: true }
  }
  const next = { ...pending, [sequence]: event }
  return { pending: next, duplicated: false }
}

/** 缓冲里最小的序号（用于诊断与「等第 N 条」提示）。 */
export function lowestBuffered<T>(pending: Record<number, T>, fallback: number): number {
  const keys = Object.keys(pending)
    .map(Number)
    .filter((value) => Number.isFinite(value))
  return keys.length ? Math.min(...keys) : fallback
}
