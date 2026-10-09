/**
 * reducer 测试：去重、乱序、补拉、快照重建、选择器。
 *
 * 这是验收书 §6「乱序和重复事件不会重复渲染」「重连使用最后游标补齐事件」的直接证据。
 */

import {
  applyEvent,
  applyNotification,
  applyReplay,
  applyResumeSnapshot,
  applySummaryList,
  assistantTextFor,
  childList,
  createAgentState,
  latestPlan,
  orderedItems,
  pendingApprovals,
  reasoningTextFor,
  setConnection,
  setThreadCursor,
  toolOutputFor,
} from './.test-build/src/agent-v2/reducer.js'
import { assert, assertDeepEqual, assertEqual, test } from './_harness.mjs'

const THREAD = 'th_000000000000000000000001'
const TURN = 'tu_000000000000000000000001'
const ITEM = 'it_000000000000000000000001'
const CALL = 'call_00000000000000000000001'
const AT = '2026-10-09T00:00:00.000Z'

function eventNote(sequence, type, payload = {}, extra = {}) {
  return {
    contract: 'agent.v2.protocol.v1',
    kind: 'notification',
    method: 'event',
    event_id: `ev_${String(sequence).padStart(23, '0')}`,
    sequence,
    thread_id: THREAD,
    turn_id: extra.turn_id ?? null,
    item_id: extra.item_id ?? null,
    call_id: extra.call_id ?? null,
    params: {
      type,
      created_at: AT,
      payload,
      idempotency_key: null,
    },
  }
}

function controlNote(method, sequence, params = {}, threadId = null) {
  return {
    contract: 'agent.v2.protocol.v1',
    kind: 'notification',
    method,
    event_id: `ev_${String(sequence).padStart(23, '0')}`,
    sequence,
    thread_id: threadId,
    params,
  }
}

function threadCreated(sequence = 1) {
  return eventNote(sequence, 'thread/created', {
    thread: {
      thread_id: THREAD,
      name: '会话',
      status: 'active',
      settings: {},
      cwd: null,
      model: null,
      permission_summary: {},
      active_turn_id: null,
      last_sequence: 0,
      parent_thread_id: null,
      path: [THREAD],
      forked_from: null,
      created_at: AT,
      updated_at: AT,
      contract: 'agent.v2.contract.v1',
    },
  })
}

function turnStarted(sequence = 2, status = 'running') {
  return turnEvent(sequence, 'turn/started', status, { from: null })
}

/**
 * 构造 Turn 类事件。
 *
 * 服务端（Agent 1 的 `_transition`）的**每条 Turn 事件都自带完整 Turn 负载**，
 * 所以测试也按这个形状造数据——否则测的就不是真实协议。
 */
function turnEvent(sequence, type, status, extra = {}) {
  return eventNote(
    sequence,
    type,
    {
      ...extra,
      to: status,
      turn: {
        turn_id: TURN,
        thread_id: THREAD,
        status,
        created_at: AT,
        updated_at: AT,
        sequence_start: 2,
        sequence_end: null,
        input_item_ids: [],
        idempotency_key: null,
        attempt: 1,
        error: extra.error ?? null,
        waiting: extra.waiting ?? null,
        cancel_reason: extra.cancel_reason ?? null,
        contract: 'agent.v2.contract.v1',
      },
    },
    { turn_id: TURN },
  )
}

function itemAdded(sequence, item) {
  return eventNote(sequence, 'item/added', { item }, { item_id: item.item_id, turn_id: item.turn_id })
}

function makeItem(overrides = {}) {
  return {
    item_id: ITEM,
    thread_id: THREAD,
    turn_id: TURN,
    type: 'assistant_text',
    sequence: 5,
    call_id: null,
    subagent_thread_id: null,
    payload: { text: '回答' },
    created_at: AT,
    contract: 'agent.v2.contract.v1',
    ...overrides,
  }
}

export function register() {
  test('初始状态为空，游标为 0', () => {
    const state = createAgentState()
    assertEqual(state.applied, 0)
    assertEqual(state.duplicates, 0)
    assertDeepEqual(state.cursors, {})
    assertEqual(state.connection.status, 'idle')
  })

  test('thread/created 建立线程并推进游标', () => {
    const state = applyNotification(createAgentState(), threadCreated())
    assertEqual(state.threads[THREAD].name, '会话')
    assertEqual(state.cursors[THREAD], 1)
    assertEqual(state.applied, 1)
  })

  test('重复事件按序号去重，不重复渲染', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, threadCreated())
    state = applyNotification(state, threadCreated())
    assertEqual(state.applied, 1, '同一序号只应用一次')
    assertEqual(state.duplicates, 2)
    assertEqual(orderedItems(state, THREAD).length, 0)
  })

  test('重复的 item/added 不会产生重复条目', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    const item = makeItem()
    state = applyNotification(state, itemAdded(3, item))
    state = applyNotification(state, itemAdded(3, item))
    assertEqual(orderedItems(state, THREAD).length, 1)
  })

  test('乱序事件先缓冲，缺口记录在 gapFrom', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    // 序号 4 先到，缺 3
    state = applyNotification(state, eventNote(4, 'model/delta', { text: '片段' }, { turn_id: TURN }))
    assertEqual(state.gapFrom[THREAD], 3)
    assertEqual(state.buffered, 1)
    assertEqual(state.cursors[THREAD], 2, '缺口未补上之前游标不得前进')
    assertEqual(state.applied, 2)
  })

  test('补拉后按序排空缓冲', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(state, eventNote(4, 'model/delta', { text: '第四' }, { turn_id: TURN }))
    state = applyNotification(state, eventNote(3, 'model/delta', { text: '第三' }, { turn_id: TURN }))
    assertEqual(state.cursors[THREAD], 4, '连续到达后应一次性排空')
    assertEqual(state.gapFrom[THREAD], null)
    assertEqual(state.applied, 4)
    assertEqual(state.stream[`${THREAD}:${TURN}`].text, '第三' + '第四')
  })

  test('缺口只能由补拉填平，不能靠等待', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(state, eventNote(6, 'model/delta', { text: '第六' }, { turn_id: TURN }))
    assertEqual(state.gapFrom[THREAD], 3)
    const replayed = applyReplay(state, THREAD, [
      {
        contract: 'agent.v2.contract.v1',
        event_id: 'ev_x3',
        sequence: 3,
        type: 'model/delta',
        created_at: AT,
        thread_id: THREAD,
        turn_id: TURN,
        item_id: null,
        call_id: null,
        idempotency_key: null,
        payload: { text: '第三' },
      },
      {
        contract: 'agent.v2.contract.v1',
        event_id: 'ev_x4',
        sequence: 4,
        type: 'model/delta',
        created_at: AT,
        thread_id: THREAD,
        turn_id: TURN,
        item_id: null,
        call_id: null,
        idempotency_key: null,
        payload: { text: '第四' },
      },
      {
        contract: 'agent.v2.contract.v1',
        event_id: 'ev_x5',
        sequence: 5,
        type: 'model/delta',
        created_at: AT,
        thread_id: THREAD,
        turn_id: TURN,
        item_id: null,
        call_id: null,
        idempotency_key: null,
        payload: { text: '第五' },
      },
    ])
    assertEqual(replayed.cursors[THREAD], 6)
    assertEqual(replayed.gapFrom[THREAD], null)
    assertEqual(replayed.backfilled[THREAD], 3)
    assertEqual(replayed.stream[`${THREAD}:${TURN}`].text, '第三第四第五第六')
  })

  test('四条乱序事件按序收敛（3,1,2,4）', () => {
    let state = createAgentState()
    state = applyNotification(state, eventNote(3, 'model/delta', { text: 'C' }, { turn_id: TURN }))
    assertEqual(state.cursors[THREAD] ?? 0, 0, '第一条就乱序，游标不得前进')
    assertEqual(state.gapFrom[THREAD], 1)
    state = applyNotification(state, eventNote(1, 'model/delta', { text: 'A' }, { turn_id: TURN }))
    assertEqual(state.cursors[THREAD], 1, '缺口未填平前只能推进到用连续序号处')
    state = applyNotification(state, eventNote(2, 'model/delta', { text: 'B' }, { turn_id: TURN }))
    assertEqual(state.cursors[THREAD], 3, '2 到了之后应把缓冲的 3 一起排空')
    state = applyNotification(state, eventNote(4, 'model/delta', { text: 'D' }, { turn_id: TURN }))
    assertEqual(state.cursors[THREAD], 4)
    assertEqual(state.stream[`${THREAD}:${TURN}`].text, 'ABCD')
    assertEqual(state.applied, 4)
    assertEqual(state.buffered, 1)
    assertEqual(state.duplicates, 0)
  })

  test('控制通知不参与事件游标', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    const before = state.cursors[THREAD]
    state = applyNotification(state, controlNote('heartbeat', 3))
    state = applyNotification(state, controlNote('subscription/started', 2, { thread_id: THREAD, cursor: 0 }))
    state = applyNotification(state, controlNote('server/shutting_down', 4))
    assertEqual(state.cursors[THREAD], before, '控制通知不得推进线程游标')
    assertEqual(state.applied, 1)
    assertEqual(state.connection.shuttingDown, true)
    assertEqual(state.connection.status, 'closed')
  })

  test('subscription/started 的 cursor 会建立基线', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, threadCreated())
    state = applyNotification(
      state,
      controlNote('subscription/started', 1, { thread_id: THREAD, cursor: 1 }, THREAD),
    )
    assertEqual(state.cursors[THREAD], 1)
    state = applyNotification(state, eventNote(2, 'turn/started', {}, { turn_id: TURN }))
    assertEqual(state.cursors[THREAD], 2, '基线之后的事件必须紧邻')
  })

  test('快照重建后基线来自服务端，旧事件被丢弃', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyResumeSnapshot(state, {
      thread: {
        thread_id: THREAD,
        name: '会话',
        status: 'active',
        settings: {},
        cwd: '/w',
        model: null,
        permission_summary: {},
        active_turn_id: null,
        last_sequence: 9,
        parent_thread_id: null,
        path: [THREAD],
        forked_from: null,
        created_at: AT,
        updated_at: AT,
        contract: 'agent.v2.contract.v1',
      },
      turns: [],
      items: [makeItem({ sequence: 3 })],
      last_sequence: 9,
    })
    assertEqual(state.cursors[THREAD], 9)
    assertEqual(state.currentThreadId, THREAD)
    assertEqual(orderedItems(state, THREAD).length, 1)
    state = applyNotification(state, eventNote(5, 'model/delta', { text: '旧事件' }, { turn_id: TURN }))
    assertEqual(state.duplicates, 1, '快照之前的旧事件必须被丢弃')
  })

  test('setThreadCursor 只前进不回退', () => {
    let state = createAgentState()
    state = setThreadCursor(state, THREAD, 10)
    state = setThreadCursor(state, THREAD, 4)
    assertEqual(state.cursors[THREAD], 10)
  })

  test('流式增量累加，Item 落地后用权威文本', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(state, eventNote(3, 'model/delta', { text: '你' }, { turn_id: TURN }))
    state = applyNotification(state, eventNote(4, 'model/delta', { text: '好' }, { turn_id: TURN }))
    assertEqual(assistantTextFor(state, THREAD, TURN), '你好')
    state = applyNotification(state, itemAdded(5, makeItem({ sequence: 5, payload: { text: '你好，我是助手。' } })))
    assertEqual(assistantTextFor(state, THREAD, TURN), '你好，我是助手。', 'Item 是权威文本')
  })

  test('推理增量与推理 Item', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(state, eventNote(3, 'model/reasoning_delta', { text: '先看' }, { turn_id: TURN }))
    assertEqual(reasoningTextFor(state, THREAD, TURN), '先看')
    state = applyNotification(
      state,
      itemAdded(4, makeItem({ item_id: 'it_r1', sequence: 4, type: 'reasoning', payload: { text: '先看材料再回答' } })),
    )
    assertEqual(reasoningTextFor(state, THREAD, TURN), '先看材料再回答')
  })

  test('工具事件：增量输出累加、失败带真实原因、耗时可见', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    const call = {
      call_id: CALL,
      name: 'read_file',
      kind: 'read_only',
      status: 'running',
      thread_id: THREAD,
      turn_id: TURN,
      arguments: { path: 'a.md' },
      output: null,
      error: null,
      requested_at: AT,
      started_at: AT,
      finished_at: null,
      duration_ms: null,
      attempt: 1,
      truncated: false,
      contract: 'agent.v2.contract.v1',
    }
    state = applyNotification(state, eventNote(2, 'tool/started', { tool_call: call }, { turn_id: TURN, call_id: CALL }))
    state = applyNotification(state, eventNote(3, 'tool/output', { chunk: '第一行' }, { turn_id: TURN, call_id: CALL }))
    state = applyNotification(state, eventNote(4, 'tool/output', { chunk: '第二行' }, { turn_id: TURN, call_id: CALL }))
    assertEqual(toolOutputFor(state, CALL), '第一行\n第二行')
    const failed = { ...call, status: 'failed', error: { code: 'approval_denied', message: '研究者拒绝了该操作' }, duration_ms: 42 }
    state = applyNotification(state, eventNote(5, 'tool/failed', { tool_call: failed }, { turn_id: TURN, call_id: CALL }))
    assertEqual(state.toolCalls[CALL].status, 'failed')
    assertEqual(state.toolCalls[CALL].error.code, 'approval_denied')
    assertEqual(state.toolCalls[CALL].duration_ms, 42)
    assert(state.notices.some((notice) => notice.level === 'error' && notice.text.includes('approval_denied')))
  })

  test('审批：请求 -> 待办列表 -> 结论后移出待办', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    const approval = {
      approval_id: 'ap_000000000000000000000001',
      thread_id: THREAD,
      turn_id: TURN,
      call_id: CALL,
      status: 'pending',
      action: { tool: 'run_command', arguments: { cmd: 'ls' }, kind: 'side_effect', risk: '高危' },
      risk: '高危',
      created_at: AT,
      decided_at: null,
      decided_by: null,
      decision_scope: null,
      contract: 'agent.v2.contract.v1',
    }
    state = applyNotification(state, eventNote(2, 'approval/requested', { approval }, { turn_id: TURN }))
    assertEqual(pendingApprovals(state, THREAD).length, 1)
    state = applyNotification(
      state,
      eventNote(3, 'approval/granted', { approval: { ...approval, status: 'granted', decision_scope: 'once' } }, { turn_id: TURN }),
    )
    assertEqual(pendingApprovals(state, THREAD).length, 0)
    assertEqual(state.approvals[approval.approval_id].decision_scope, 'once')
  })

  test('Turn 状态与活动 Turn 的重建', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    assertEqual(state.threads[THREAD].active_turn_id, TURN)
    assertEqual(state.turns[TURN].status, 'running')
    state = applyNotification(
      state,
      turnEvent(3, 'turn/completed', 'completed', { from: 'running', stop_reason: 'end_turn' }),
    )
    assertEqual(state.turns[TURN].status, 'completed')
    assertEqual(state.threads[THREAD].active_turn_id, null)
  })

  test('失败 Turn 会留下错误通知与错误信息', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(
      state,
      turnEvent(3, 'turn/failed', 'failed', {
        from: 'running',
        error: { code: 'model_stream_error', message: '401 鉴权失败' },
      }),
    )
    assertEqual(state.turns[TURN].error.message, '401 鉴权失败')
    assert(state.notices.some((notice) => notice.level === 'error'))
  })

  test('计划 Item 成为最新计划', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(
      state,
      itemAdded(2, makeItem({
        item_id: 'it_plan',
        sequence: 2,
        type: 'plan',
        payload: { title: '三阶段', steps: [{ index: 1, title: '梳理', status: 'pending' }] },
      })),
    )
    assertEqual(latestPlan(state, THREAD).title, '三阶段')
    state = applyNotification(
      state,
      itemAdded(3, makeItem({
        item_id: 'it_plan2',
        sequence: 3,
        type: 'plan',
        payload: { title: '三阶段', steps: [{ index: 1, title: '梳理', status: 'completed' }] },
      })),
    )
    assertEqual(latestPlan(state, THREAD).steps[0].status, 'completed')
    assertEqual(latestPlan(state, THREAD).item_id, 'it_plan2')
  })

  test('子 Agent 树：创建、完成、失败、中断', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(
      state,
      eventNote(2, 'agent/child_created', { child_thread_id: 'th_child_1', name: '文献调研', path: [THREAD, 'th_child_1'] }),
    )
    state = applyNotification(
      state,
      eventNote(3, 'agent/child_created', { child_thread_id: 'th_child_2', name: '复现实验', path: [THREAD, 'th_child_2'] }),
    )
    assertEqual(childList(state, THREAD).length, 2)
    state = applyNotification(
      state,
      eventNote(4, 'agent/child_completed', { child_thread_id: 'th_child_1', status: 'completed', summary: '找到 42 篇', item_id: 'it_sub' }),
    )
    state = applyNotification(
      state,
      eventNote(5, 'agent/child_failed', { child_thread_id: 'th_child_2', status: 'failed', error: { code: 'child_failed', message: '缺依赖' } }),
    )
    const children = childList(state, THREAD)
    assertEqual(children[0].status, 'completed')
    assertEqual(children[0].summary, '找到 42 篇')
    assertEqual(children[1].status, 'failed')
    assertEqual(children[1].error.message, '缺依赖')
  })

  test('压缩事件驱动摘要列表与生效标记', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, eventNote(2, 'compaction/started', { trigger: 'manual', covered_until: 5 }, { turn_id: TURN }))
    assert(state.notices.some((notice) => notice.text.includes('开始压缩')))
    state = applyNotification(
      state,
      eventNote(3, 'compaction/completed', { summary_id: 'sum_1', summary: '摘要内容', covered_until: 5, snapshot_sequence: 5 }, { turn_id: TURN }),
    )
    state = applySummaryList(state, THREAD, [
      { summary_id: 'sum_1', text: '摘要内容', covered_until: 5, snapshot_sequence: 5, trigger: 'manual', created_at: AT, edited: false, active: true },
    ])
    assertEqual(state.summaries[THREAD][0].text, '摘要内容')
    assertEqual(state.summaries[THREAD][0].active, true)
    state = applyNotification(state, eventNote(4, 'compaction/edited', { summary_id: 'sum_1', text: '研究者修订' }))
    assertEqual(state.summaries[THREAD][0].text, '研究者修订')
    assertEqual(state.summaries[THREAD][0].edited, true)
    state = applyNotification(state, eventNote(5, 'compaction/failed', { error: { code: 'summarize_failed', message: '上游不可用' } }))
    assert(state.notices.some((notice) => notice.level === 'error' && notice.text.includes('原上下文保持不变')))
  })

  test('applyEvent 与 applyNotification 等价', () => {
    const note = threadCreated()
    const viaNotification = applyNotification(createAgentState(), note)
    const viaEvent = applyEvent(createAgentState(), {
      contract: 'agent.v2.contract.v1',
      event_id: note.event_id,
      sequence: 1,
      type: 'thread/created',
      created_at: AT,
      thread_id: THREAD,
      turn_id: null,
      item_id: null,
      call_id: null,
      idempotency_key: null,
      payload: note.params.payload,
    })
    assertDeepEqual(viaEvent.cursors, viaNotification.cursors)
    assertEqual(viaEvent.applied, viaNotification.applied)
  })

  test('连接状态补丁不影响事件游标', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = setConnection(state, { status: 'reconnecting', attempts: 2, message: '断线' })
    assertEqual(state.connection.attempts, 2)
    assertEqual(state.cursors[THREAD], 1)
  })

  test('没有 thread_id 的事件通知记错误，不静默丢弃', () => {
    const state = applyNotification(createAgentState(), {
      contract: 'agent.v2.protocol.v1',
      kind: 'notification',
      method: 'event',
      event_id: 'ev_x',
      sequence: 1,
      thread_id: null,
      params: { type: 'model/delta', created_at: AT, payload: { text: 'x' } },
    })
    assert(state.notices.some((notice) => notice.level === 'error'))
    assertEqual(state.applied, 0)
  })
  test('迟到的 delta 不会把已完成的 Turn 改回运行中（规则 4）', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(
      state,
      turnEvent(3, 'turn/completed', 'completed', { from: 'running', stop_reason: 'end_turn' }),
    )
    const before = state.lateDropped
    // 迟到的正文增量与「又开始跑」的旧状态
    state = applyNotification(state, eventNote(4, 'model/delta', { text: '迟到片段' }, { turn_id: TURN }))
    state = applyNotification(state, turnStarted(5))
    assertEqual(state.turns[TURN].status, 'completed', '终态是权威，不能被迟到事件改回')
    assert(state.lateDropped > before, '被丢弃的迟到事件要计数，便于验收取证')
    assertEqual(state.stream[`${THREAD}:${TURN}`] ?? '', '', '迟到增量不得进入正文缓冲')
    assertEqual(state.cursors[THREAD], 5, '游标仍必须前进，否则缺口永远补不平')
  })

  test('中断后忽略迟到的正文与工具增量，但仍接受终态与审批（规则 5）', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(
      state,
      turnEvent(3, 'turn/interrupted', 'interrupted', { from: 'running', cancel_reason: '用户停止' }),
    )
    assertEqual(state.turns[TURN].status, 'interrupted')

    state = applyNotification(state, eventNote(4, 'model/delta', { text: '停止后还在流' }, { turn_id: TURN }))
    state = applyNotification(
      state,
      itemAdded(5, makeItem({ item_id: 'it_late', sequence: 5, payload: { text: '停止后才落地的正文' } })),
    )
    assertEqual(Object.keys(state.items).length, 0, '中断后落地的正文 Item 也必须忽略')

    // 结构化事件照常应用：审批卡仍然要能出现
    const approval = {
      approval_id: 'ap_1',
      thread_id: THREAD,
      turn_id: TURN,
      call_id: CALL,
      status: 'pending',
      action: { tool: 'run_command', arguments: {}, kind: 'side_effect', risk: '高危' },
      risk: '高危',
      created_at: AT,
      decided_at: null,
      decided_by: null,
      decision_scope: null,
      contract: 'agent.v2.contract.v1',
    }
    state = applyNotification(state, eventNote(6, 'approval/requested', { approval }, { turn_id: TURN }))
    assertEqual(pendingApprovals(state, THREAD).length, 1)
    assertEqual(state.cursors[THREAD], 6)
  })

  test('已完成的 Turn 仍然接受既有 Item（Item 是既成事实）', () => {
    let state = applyNotification(createAgentState(), threadCreated())
    state = applyNotification(state, turnStarted())
    state = applyNotification(state, turnEvent(3, 'turn/completed', 'completed', { from: 'running' }))
    state = applyNotification(state, itemAdded(4, makeItem({ sequence: 4, payload: { text: '结论' } })))
    assertEqual(orderedItems(state, THREAD).length, 1)
    assertEqual(assistantTextFor(state, THREAD, TURN), '结论')
  })
}
