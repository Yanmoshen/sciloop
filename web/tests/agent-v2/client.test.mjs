/**
 * 客户端测试：自动重连、游标续订、请求幂等键、响应与通知分发。
 *
 * 用注入的假 socket，不依赖浏览器、不依赖网络。
 */

import { AgentV2Client, AgentV2RequestError, unwrap } from './.test-build/src/api/agent-v2.js'
import { assert, assertDeepEqual, assertEqual, test, wait } from './_harness.mjs'

/** 可脚本化的假 WebSocket。 */
class FakeSocket {
  constructor(url) {
    this.url = url
    this.sent = []
    this.closed = null
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    FakeSocket.instances.push(this)
  }

  send(data) {
    this.sent.push(JSON.parse(data))
  }

  close(code, reason) {
    this.closed = { code, reason }
  }

  /** 模拟服务端握手完成。 */
  open() {
    this.onopen?.({})
  }

  /** 模拟服务端推一条帧。 */
  deliver(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  /** 模拟断线。 */
  drop(code = 1006) {
    this.onclose?.({ code, reason: '' })
  }

  static instances = []
  static latest() {
    return FakeSocket.instances[FakeSocket.instances.length - 1]
  }
  static reset() {
    FakeSocket.instances = []
  }
}

function makeClient(overrides = {}) {
  FakeSocket.reset()
  const events = []
  const client = new AgentV2Client({
    url: 'ws://test/api/v2/agent/ws',
    socketFactory: (url) => new FakeSocket(url),
    baseBackoffMs: 1,
    maxBackoffMs: 4,
    requestTimeoutMs: 200,
    clientId: 'test-client',
    ...overrides,
  })
  client.subscribe((event) => events.push(event))
  return { client, events }
}

function responseFor(request, result = {}) {
  return {
    contract: 'agent.v2.protocol.v1',
    kind: 'response',
    id: request.id,
    ok: true,
    result,
    error: null,
    replayed: false,
  }
}

export function register() {
  test('连接成功后状态为 open', async () => {
    const { client } = makeClient()
    client.connect()
    assertEqual(client.status, 'connecting')
    FakeSocket.latest().open()
    assertEqual(client.status, 'connected')
    client.close()
  })

  test('同一连接内不会重复创建 socket', async () => {
    const { client } = makeClient()
    client.connect()
    client.connect()
    assertEqual(FakeSocket.instances.length, 1)
    client.close()
  })

  test('只读请求不带幂等键，变更请求自动带键', async () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeSocket.latest()
    socket.open()

    void client.request('thread/list', {})
    void client.request('turn/start', { thread_id: 'th_x', text: 'hi' })

    assertEqual(socket.sent.length, 2)
    assertEqual(socket.sent[0].idempotency_key, null, '只读请求不应带幂等键')
    assert(socket.sent[1].idempotency_key.startsWith('test-client:'), '变更请求必须带幂等键')
    assertEqual(socket.sent[1].contract, 'agent.v2.protocol.v1')
    assertEqual(socket.sent[1].kind, 'request')
    client.close()
  })

  test('响应按 id 配对并解包', async () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeSocket.latest()
    socket.open()
    const promise = client.request('thread/list', {})
    socket.deliver(responseFor(socket.sent[0], { threads: [{ thread_id: 'th_1' }] }))
    const result = unwrap(await promise)
    assertDeepEqual(result.threads, [{ thread_id: 'th_1' }])
    client.close()
  })

  test('失败响应抛出带 code 的错误', async () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeSocket.latest()
    socket.open()
    const promise = client.request('thread/resume', { thread_id: 'th_x' })
    socket.deliver({
      contract: 'agent.v2.protocol.v1',
      kind: 'response',
      id: socket.sent[0].id,
      ok: false,
      result: null,
      error: { code: 'cursor_expired', message: 'cursor 已过期', data: { reason: 'cursor_ahead' } },
      replayed: false,
    })
    let caught = null
    try {
      unwrap(await promise)
    } catch (error) {
      caught = error
    }
    assert(caught instanceof AgentV2RequestError, '必须是带 code 的客户端错误')
    assertEqual(caught.code, 'cursor_expired')
    assertEqual(caught.data.reason, 'cursor_ahead')
    client.close()
  })

  test('通知会分发给订阅者', async () => {
    const { client, events } = makeClient()
    client.connect()
    const socket = FakeSocket.latest()
    socket.open()
    socket.deliver({
      contract: 'agent.v2.protocol.v1',
      kind: 'notification',
      method: 'event',
      event_id: 'ev_1',
      sequence: 1,
      thread_id: 'th_1',
      params: { type: 'thread/created', created_at: 'x', payload: {} },
    })
    const notifications = events.filter((event) => event.type === 'notification')
    assertEqual(notifications.length, 1)
    assertEqual(notifications[0].notification.method, 'event')
    client.close()
  })

  test('断线后自动重连并递增尝试次数', async () => {
    const { client, events } = makeClient()
    client.connect()
    FakeSocket.latest().open()
    FakeSocket.latest().drop(1006)
    assertEqual(client.status, 'reconnecting')
    assertEqual(client.attempts, 1)
    await wait(10)
    assertEqual(FakeSocket.instances.length, 2, '退避后必须新建连接')
    FakeSocket.latest().open()
    assertEqual(client.status, 'connected')
    assertEqual(client.attempts, 0)
    const statuses = events.filter((event) => event.type === 'status').map((event) => event.status)
    assert(statuses.includes('reconnecting'), '必须上报重连状态')
    client.close()
  })

  test('重连时未决请求带同一个幂等键重发', async () => {
    const onOpenFlags = []
    const { client } = makeClient({
      onOpen: (_client, reconnected) => onOpenFlags.push(reconnected),
    })
    client.connect()
    FakeSocket.latest().open()
    const promise = client.request('turn/start', { thread_id: 'th_1', text: '只提交一次' })
    const firstKey = FakeSocket.latest().sent[0].idempotency_key
    assert(firstKey, '变更请求必须有幂等键')

    // 断线（响应未到），自动重连
    FakeSocket.latest().drop(1006)
    await wait(10)
    const second = FakeSocket.latest()
    second.open()

    assertEqual(second.sent.length, 1, '重连后应重发未决请求')
    assertEqual(second.sent[0].idempotency_key, firstKey, '重发必须复用同一个幂等键')
    assertEqual(second.sent[0].id, FakeSocket.latest().sent[0].id, '请求 id 保持不变')

    second.deliver({
      contract: 'agent.v2.protocol.v1',
      kind: 'response',
      id: second.sent[0].id,
      ok: true,
      result: { turn: { turn_id: 'tu_1' }, created: true, reused: false },
      error: null,
      replayed: true,
    })
    const result = unwrap(await promise)
    assertEqual(result.turn.turn_id, 'tu_1')
    assertEqual(client.pendingCount(), 0)
    assertDeepEqual(onOpenFlags, [false, true], '第二次打开必须是「重连」')
    client.close()
  })

  test('未连接时请求会排队，连上后自动发出', async () => {
    const { client } = makeClient()
    client.connect()
    // 尚未 open
    const promise = client.request('thread/list', {})
    assertEqual(FakeSocket.latest().sent.length, 0)
    assertDeepEqual(client.pendingMethods(), ['thread/list'])
    FakeSocket.latest().open()
    assertEqual(FakeSocket.latest().sent.length, 1)
    FakeSocket.latest().deliver(responseFor(FakeSocket.latest().sent[0], { threads: [] }))
    await promise
    client.close()
  })

  test('请求超时会拒绝，不无限挂起', async () => {
    const { client } = makeClient({ requestTimeoutMs: 5 })
    client.connect()
    FakeSocket.latest().open()
    const promise = client.request('agent/wait', { thread_id: 'th_1', child_thread_ids: ['th_2'] })
    // 立刻挂上 handler：否则等待期间会出现「无处理的拒绝」把进程带崩
    const settled = promise.then(
      () => null,
      (error) => error,
    )
    await wait(15)
    const error = await settled
    assert(error instanceof Error, '超时必须拒绝而不是静默挂起')
    assert(error.message.includes('超时'), `错误信息应说明超时：${error.message}`)
    assertEqual(client.pendingCount(), 0, '超时后必须从待决队列移除')
    client.close()
  })

  test('主动关闭后不再自动重连', async () => {
    const { client } = makeClient()
    client.connect()
    FakeSocket.latest().open()
    client.close()
    assertEqual(client.status, 'closed')
    await wait(10)
    assertEqual(FakeSocket.instances.length, 1, '主动关闭后不得重连')
  })

  test('令牌会拼进连接地址', async () => {
    const { client } = makeClient({ url: 'ws://test/ws', token: 'tok-1' })
    client.connect()
    assert(FakeSocket.latest().url.includes('owner_token=tok-1'))
    client.close()
  })
}
