/**
 * 协议常量测试：版本、方法名、幂等键纪律。
 *
 * 这些常量必须与服务端注册表（`server/api/v2/agent_protocol/methods.py`）保持一致；
 * 这里断言的是「前端不会漏标变更类方法」，避免断线重试悄悄产生第二次副作用。
 */

import {
  CONTRACT_VERSION,
  METHOD,
  MUTATING_METHODS,
  PROTOCOL_VERSION,
  isMutating,
} from './.test-build/src/agent-v2/protocol.js'
import { assert, assertEqual, test } from './_harness.mjs'

export function register() {
  test('版本常量与服务端一致', () => {
    assertEqual(PROTOCOL_VERSION, 'agent.v2.protocol.v1')
    assertEqual(CONTRACT_VERSION, 'agent.v2.contract.v1')
  })

  test('方法名覆盖计划书要求的全部方法', () => {
    const required = [
      'thread/start',
      'thread/resume',
      'thread/settings/update',
      'turn/start',
      'turn/steer',
      'turn/continue',
      'turn/interrupt',
      'turn/recover',
      'thread/subscribe',
      'thread/events/replay',
      'approval/resolve',
      'thread/compact',
      'thread/compaction/edit',
      'thread/compaction/restore',
      'memory/list',
      'memory/update',
      'memory/delete',
      'agent/list',
      'agent/wait',
      'agent/interrupt',
    ]
    const known = new Set(Object.values(METHOD))
    const missing = required.filter((name) => !known.has(name))
    assertEqual(missing.length, 0, `缺少方法：${missing.join(', ')}`)
  })

  test('方法名统一使用 命名空间/动作 形式', () => {
    for (const name of Object.values(METHOD)) {
      assert(/^[a-z][a-z0-9]*(\/[a-z0-9_]+)+$/.test(name), `方法名格式不对：${name}`)
    }
  })

  test('变更类方法必须要求幂等键', () => {
    const mustBeMutating = [
      METHOD.threadStart,
      METHOD.threadSettingsUpdate,
      METHOD.threadDelete,
      METHOD.turnStart,
      METHOD.turnSteer,
      METHOD.turnContinue,
      METHOD.turnInterrupt,
      METHOD.turnRecover,
      METHOD.approvalResolve,
      METHOD.threadCompact,
      METHOD.compactionEdit,
      METHOD.compactionRestore,
      METHOD.memoryUpdate,
      METHOD.memoryDelete,
      METHOD.agentInterrupt,
    ]
    for (const name of mustBeMutating) {
      assert(isMutating(name), `${name} 必须被标记为变更类`)
    }
  })

  test('只读方法不得要求幂等键', () => {
    const readOnly = [
      METHOD.describe,
      METHOD.threadList,
      METHOD.threadResume,
      METHOD.threadSubscribe,
      METHOD.threadUnsubscribe,
      METHOD.threadReplay,
      METHOD.compactionList,
      METHOD.memoryList,
      METHOD.agentList,
      METHOD.agentWait,
    ]
    for (const name of readOnly) {
      assert(!isMutating(name), `${name} 不应被标记为变更类`)
    }
  })

  test('MUTATING_METHODS 无重复项', () => {
    assertEqual(new Set(MUTATING_METHODS).size, MUTATING_METHODS.length)
  })
}
