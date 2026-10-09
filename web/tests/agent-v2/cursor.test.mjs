/**
 * 游标算法测试（WP-03 的 `cursor.ts`）。
 *
 * 去重、乱序、排空三件事脱离业务单独验证，保证 reducer 的编排建立在这层之上。
 */

import {
  bufferEvent,
  drainContiguous,
  isDuplicate,
  isOutOfOrder,
  lowestBuffered,
} from './.test-build/src/agent-v2/cursor.js'
import { assert, assertDeepEqual, assertEqual, test } from './_harness.mjs'

export function register() {
  test('重复判定：序号小于等于游标即为重复', () => {
    assert(isDuplicate(3, 3))
    assert(isDuplicate(2, 3))
    assert(!isDuplicate(4, 3))
  })

  test('乱序判定：序号大于游标 + 1 才是缺口', () => {
    assert(!isOutOfOrder(4, 3), '紧邻的一条不算缺口')
    assert(isOutOfOrder(5, 3))
    assert(!isOutOfOrder(1, 0))
  })

  test('排空：连续的事件一次性应用', () => {
    const pending = { 4: 'D', 5: 'E', 6: 'F' }
    const drained = drainContiguous(pending, 3, 3)
    assertDeepEqual(drained.applied, ['D', 'E', 'F'])
    assertDeepEqual(drained.appliedSequences, [4, 5, 6])
    assertEqual(drained.cursor, 6)
    assertEqual(drained.gapFrom, null)
    assertDeepEqual(drained.remaining, {})
  })

  test('排空：遇到空洞就停，并给出缺口起点', () => {
    const pending = { 4: 'D', 6: 'F' }
    const drained = drainContiguous(pending, 3, 3)
    assertDeepEqual(drained.applied, ['D'])
    assertEqual(drained.cursor, 4)
    assertEqual(drained.gapFrom, 5)
    assertDeepEqual(Object.keys(drained.remaining), ['6'])
  })

  test('排空：不改动入参（纯函数）', () => {
    const pending = { 2: 'B' }
    drainContiguous(pending, 1, 1)
    assertDeepEqual(pending, { 2: 'B' })
  })

  test('缓冲：同序号只保留第一份', () => {
    const first = bufferEvent({}, 5, 'first')
    assert(first.duplicated === false)
    const second = bufferEvent(first.pending, 5, 'second')
    assert(second.duplicated === true)
    assertEqual(second.pending[5], 'first')
  })

  test('最低缓冲序号用于提示「还差哪一条」', () => {
    assertEqual(lowestBuffered({ 9: 1, 3: 2, 5: 3 }, 0), 3)
    assertEqual(lowestBuffered({}, 7), 7)
  })
}
