/** User-visible message safety and stream merge regressions. */

import { assert, assertEqual, test } from './_harness.mjs'
import { looksLikeResearch, pendingAssistantText, toBlocks } from './.test-build/src/components/agent-v2/blocks.js'
import { humanError, mergeAssistantContent, sanitizeAssistantText } from './.test-build/src/utils/messages.js'

export function register() {
  test('研究关键词不会把普通追问误判为研究启动', () => {
    assertEqual(looksLikeResearch('文献调研怎么做'), false)
    assertEqual(looksLikeResearch('为什么会有演示入口'), false)
    assertEqual(looksLikeResearch('帮我调研有关优化时间复杂度'), true)
    assertEqual(looksLikeResearch('开始文献调研'), true)
  })

  test('完成快照后不重新显示带光标的重复正文', () => {
    const base = {
      item_id: 'it_text',
      thread_id: 'th_1',
      turn_id: 'tu_1',
      sequence: 1,
      call_id: null,
      subagent_thread_id: null,
      created_at: '2026-10-11T00:00:00.000Z',
      contract: 'agent.v2.contract.v1',
      type: 'assistant_text',
    }
    const blocks = toBlocks(
      [{ ...base, payload: { text: '你好，我是助手。' } }],
      { toolCallOf: () => null, toolOutputOf: () => '', approvalOf: () => null },
    )
    assertEqual(pendingAssistantText(blocks, '你好，我是助手。'), '')
    assertEqual(pendingAssistantText(blocks, '你好，我是助手。还有补充'), '还有补充')
  })

  test('旧版执行器提示不会作为助手正文展示', () => {
    const value = '已收到请求。\n这次文件操作还没有执行，因为本地执行环境暂时不可用。启动本地执行服务后，请重新发送这条请求。\n请稍后继续。'
    const result = sanitizeAssistantText(value)
    assertEqual(result, '已收到请求。\n请稍后继续。')
    assert(!result.includes('本地执行环境暂时不可用'), '内部执行器状态不得出现在正文')
  })

  test('fatal ReadError 诊断不会进入正文或失败提示', () => {
    const raw = '已完成前半段。\n[fatal] ReadError: socket closed while reading debug_state\n后续说明仍然保留。'
    const result = sanitizeAssistantText(raw)
    assert(result.includes('已完成前半段。'))
    assert(result.includes('后续说明仍然保留。'))
    assert(!result.includes('[fatal]'))
    assert(!result.includes('ReadError'))
    assert(!result.includes('debug_state'))
    assertEqual(
      humanError({ code: 'fatal', message: 'ReadError' }),
      '这次回答没有完整完成，请重新发送；已经完成的内容会保留。',
    )
  })

  test('思考区只显示明确摘要，不泄露完整内部推理', () => {
    const base = {
      item_id: 'it_reasoning',
      thread_id: 'th_1',
      turn_id: 'tu_1',
      sequence: 1,
      call_id: null,
      subagent_thread_id: null,
      created_at: '2026-10-11T00:00:00.000Z',
      contract: 'agent.v2.contract.v1',
      type: 'reasoning',
    }
    const hidden = toBlocks(
      [{ ...base, payload: { text: 'private reasoning must stay hidden' } }],
      { toolCallOf: () => null, toolOutputOf: () => '', approvalOf: () => null },
    )
    assertEqual(hidden.length, 0, '没有摘要字段时不应渲染 reasoning 内容')
    const visible = toBlocks(
      [{ ...base, payload: { text: 'private reasoning', summary: '正在整理关键事实' } }],
      { toolCallOf: () => null, toolOutputOf: () => '', approvalOf: () => null },
    )
    assertEqual(visible.length, 1)
    assertEqual(visible[0].text, '正在整理关键事实')
  })

  test('工具诊断不会清空已经流式输出的正文', () => {
    const answer = '已经整理好文件内容，并列出了下一步建议。'
    const result = sanitizeAssistantText(
      `${answer}\n\nAll connection attempts failed while running files_on_computer`,
    )
    assert(result.includes(answer), '此前已经输出的正文必须保留')
    assert(!result.includes('All connection attempts failed'), '内部网络诊断不得出现在正文')
    assert(!result.includes('files_on_computer'), '内部工具名不得出现在正文')
    assert(!result.includes('自动恢复'), '正文净化不能推断执行器状态或插入失败提示')
  })

  test('介绍工具和协议名称的普通正文完整保留', () => {
    const answer = '我按类别介绍当前可用能力。\n\n电脑文件：`files_on_computer`；命令：`run_on_computer`。\n还可以解析 reasoning_content、tool_call_id、request_id 等字段。'
    let streamed = ''
    for (const character of answer) {
      streamed += character
      assertEqual(sanitizeAssistantText(streamed), streamed)
    }
    assertEqual(sanitizeAssistantText(answer), answer)
  })

  test('诊断结束后继续输出的正常内容不会丢失或收到伪造失败提示', () => {
    const answer = '已整理第一部分。\nAll connection attempts failed while running files_on_computer\n第二部分：你可以继续询问。'
    assertEqual(sanitizeAssistantText(answer), '已整理第一部分。\n第二部分：你可以继续询问。')
  })

  test('最终纠正快照不会重复已经输出的介绍段落', () => {
    const opening = '我按类别说清楚，下面介绍当前会话实际可用的能力和使用方法。\n'
    assertEqual(
      mergeAssistantContent(`${opening}文件工具：\`files_on_com`, `${opening}文件工具：\`files_on_computer\`。介绍完成。`),
      `${opening}文件工具：\`files_on_computer\`。介绍完成。`,
    )
  })

  test('较短的最终工具轮快照不会删除先前已经输出的正文', () => {
    const opening = '已经为你检查知识库里的相关文件，下面说明检查结果与后续建议。\n'
    const streamed = `${opening}第一轮检查完成，文件A完整。\n第二轮检查完成，文件B也完整。`
    const finalRound = `${opening}最后一轮检查完成。`
    const result = mergeAssistantContent(streamed, finalRound)
    assert(result.includes('文件A完整'), '保留前一工具轮的正文')
    assert(result.includes('文件B也完整'), '保留后续工具轮的正文')
    assert(result.includes('最后一轮检查完成'), '保留最终轮的补充')
    assertEqual(mergeAssistantContent(streamed, opening), streamed)
  })

  test('移除引用的异常栈后仍保留继续生成的正常正文', () => {
    const answer = '已完成文件检查。\nTraceback (most recent call last):\n  File "runner.py", line 3, in run\n    raise RuntimeError("connection")\nRuntimeError: connection\n\n现在继续说明检查结果，文件内容没有丢失。'
    const result = sanitizeAssistantText(answer)
    assert(result.includes('已完成文件检查'), '保留异常前正文')
    assert(result.includes('现在继续说明检查结果'), '保留异常后正文')
    assert(!result.includes('runner.py'), '移除原始异常栈')
    assert(!result.includes('RuntimeError:'), '移除原始异常类型')
  })

  test('done 快照只补充内容，不重复覆盖或追加已发送的后缀', () => {
    const streamed = '先给出已经完成的分析。'
    assertEqual(mergeAssistantContent(streamed, '工具暂未完成'), `${streamed}\n\n工具暂未完成`)
    assertEqual(mergeAssistantContent(streamed, streamed), streamed)
    assertEqual(mergeAssistantContent(`${streamed}工具暂未完成`, '工具暂未完成'), `${streamed}工具暂未完成`)
  })
}
