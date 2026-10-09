/**
 * 快照持久化与恢复测试（WP-03 规则 6 / WP-06）。
 *
 * 用内存替身代替 localStorage：验证「页面卸载后保留快照和 cursor」，
 * 以及「重新进入先加载快照、再补事件」这条路径不依赖模型。
 */

import {
  SNAPSHOT_KEY,
  SNAPSHOT_VERSION,
  clearSnapshot,
  fromPersisted,
  loadSnapshot,
  saveSnapshot,
  toPersisted,
} from './.test-build/src/agent-v2/recovery.js'
import { applyNotification, createAgentState, setThreadCursor } from './.test-build/src/agent-v2/reducer.js'
import { assert, assertDeepEqual, assertEqual, test } from './_harness.mjs'

const THREAD = 'th_000000000000000000000001'
const AT = '2026-10-09T00:00:00.000Z'

class MemoryStorage {
  constructor() {
    this.map = new Map()
    this.failing = false
  }

  getItem(key) {
    return this.map.has(key) ? this.map.get(key) : null
  }

  setItem(key, value) {
    if (this.failing) throw new Error('quota exceeded')
    this.map.set(key, value)
  }

  removeItem(key) {
    this.map.delete(key)
  }
}

function eventNote(sequence, type, payload = {}) {
  return {
    contract: 'agent.v2.protocol.v1',
    kind: 'notification',
    method: 'event',
    event_id: `ev_${String(sequence).padStart(23, '0')}`,
    sequence,
    thread_id: THREAD,
    turn_id: null,
    item_id: null,
    call_id: null,
    params: { type, created_at: AT, payload, idempotency_key: null },
  }
}

function stateWithThread() {
  const created = applyNotification(createAgentState(), eventNote(1, 'thread/created', {
    thread: {
      thread_id: THREAD,
      name: '会话',
      status: 'active',
      settings: {},
      cwd: '/w',
      model: null,
      permission_summary: {},
      active_turn_id: null,
      last_sequence: 1,
      parent_thread_id: null,
      path: [THREAD],
      forked_from: null,
      created_at: AT,
      updated_at: AT,
      contract: 'agent.v2.contract.v1',
    },
  }))
  const withItem = applyNotification(created, eventNote(2, 'item/added', {
    item: {
      item_id: 'it_1',
      thread_id: THREAD,
      turn_id: null,
      type: 'assistant_text',
      sequence: 2,
      call_id: null,
      subagent_thread_id: null,
      payload: { text: '上一轮的结论' },
      created_at: AT,
      contract: 'agent.v2.contract.v1',
    },
  }))
  // 当前会话的选择由 store 决定（这里显式指定，模拟用户切到了这个会话）
  return { ...setThreadCursor(withItem, THREAD, 2), currentThreadId: THREAD }
}

export function register() {
  test('快照包含游标与明细，可原样还原', () => {
    const state = stateWithThread()
    const persisted = toPersisted(state, AT)
    assertEqual(persisted.version, SNAPSHOT_VERSION)
    assertEqual(persisted.cursors[THREAD], 2)
    assertEqual(Object.keys(persisted.items).length, 1)

    const restored = fromPersisted(persisted)
    assert(restored !== null, '同版本快照必须能还原')
    assertEqual(restored.currentThreadId, THREAD)
    assertEqual(restored.applied.cursors[THREAD], 2)
    assertEqual(restored.applied.items.it_1.payload.text, '上一轮的结论')
  })

  test('版本不符或结构损坏时安全失败（返回 null，不抛异常）', () => {
    assertEqual(fromPersisted(null), null)
    assertEqual(fromPersisted({ version: 999, threads: {}, cursors: {} }), null)
    assertEqual(fromPersisted({ version: SNAPSHOT_VERSION }), null)
    assertEqual(fromPersisted('not an object'), null)
  })

  test('保存 / 读取 / 清除走同一个键', () => {
    const storage = new MemoryStorage()
    const state = stateWithThread()
    assert(saveSnapshot(storage, state), '保存应成功')
    assert(storage.getItem(SNAPSHOT_KEY) !== null)

    const restored = loadSnapshot(storage)
    assert(restored !== null)
    assertEqual(restored.applied.cursors[THREAD], 2)

    clearSnapshot(storage)
    assertEqual(storage.getItem(SNAPSHOT_KEY), null)
    assertEqual(loadSnapshot(storage), null)
  })

  test('存储不可用时静默降级（不抛、不影响启动）', () => {
    const storage = new MemoryStorage()
    storage.failing = true
    assertEqual(saveSnapshot(storage, stateWithThread()), false)
    assertEqual(loadSnapshot(null), null)
    assertEqual(saveSnapshot(null, createAgentState()), false)
  })

  test('恢复后从快照游标继续补事件（不重复渲染旧事件）', () => {
    const storage = new MemoryStorage()
    const state = stateWithThread()
    saveSnapshot(storage, state)

    const restored = loadSnapshot(storage)
    assert(restored !== null)
    const resumed = { ...createAgentState(), ...restored.applied }

    // 快照之前的旧事件：丢弃
    const replayOld = applyNotification(resumed, eventNote(1, 'thread/created', {}))
    assertEqual(replayOld.duplicates, 1)
    // 快照之后的新事件：正常应用
    const replayNew = applyNotification(resumed, eventNote(3, 'model/delta', { text: '继续' }))
    assertEqual(replayNew.cursors[THREAD], 3)
    assertEqual(replayNew.duplicates, 0)
    assertDeepEqual(Object.keys(replayNew.items), ['it_1'])
  })
}
