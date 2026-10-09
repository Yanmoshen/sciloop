# agent.v2 契约说明（冻结基线）

契约版本：`agent.v2.contract.v1`　冻结标记：`agent-v2-contract-v1`

本目录是**跨 Agent 共享的唯一接口定义**。Agent 2（工具入口 / 审批）与 Agent 3
的 worktree 从冻结标记创建，只读本目录；实现分别落在
`server/services/agent_runtime_v2/` 等平行目录里，互不修改。

## 1. 目录内容

| 文件 | 作用 |
|---|---|
| `version.py` | 契约版本常量与可读版本判定 |
| `ids.py` | 稳定 ID 生成/解析/校验 |
| `enums.py` | 全部封闭枚举 + Turn 状态迁移表 |
| `errors.py` | 错误类型（含结构化事件损坏错误） |
| `models.py` | Thread / Turn / Item / ToolCall / Event / MemoryRecord / ApprovalRequest / ModelRequest / 流条目 |
| `cancellation.py` | `CancelToken` 取消令牌 |
| `clock.py` | 时钟抽象（`SystemClock` / `FakeClock`） |
| `validate.py` | JSON Schema 加载与校验（纯标准库） |
| `fake.py` | `ModelProvider` / `ToolExecutor` 协议 + 最小 fake 实现 |
| `*.schema.json` | 8 份机器可读 schema |

导入方式（`server/` 是导入根）：

```python
from contracts.agent_v2 import Event, Turn, TurnStatus, new_id, CONTRACT_VERSION
```

## 2. 稳定 ID

格式：`<prefix>_<13 位十六进制毫秒><10 位随机十六进制>`，总长 24 字符。

| 对象 | 前缀 | 对象 | 前缀 |
|---|---|---|---|
| Thread | `th_` | 事件 | `ev_` |
| Turn | `tu_` | 审批请求 | `ap_` |
| Item | `it_` | 记忆 | `mem_` |
| 工具调用 | `call_` | 压缩/摘要 | `cmp_` / `sum_` |

前缀保证同类型内**字典序即时间序**，游标与快照因此可以直接对齐。

## 3. Turn 状态机

```
queued ──▶ running ──▶ completed
   │          │  ▲
   │          │  └── waiting_approval / waiting_input ──┐
   │          ▼                                         │
   └────▶ interrupted ──▶ running ────────────────────┘
   │          │
   └────▶ failed ◀┘
```

- 活动态（占用「同线程唯一活动 Turn」名额）：`running`、`waiting_approval`、`waiting_input`。
- 终态：`completed`、`failed`（无出边）。
- **`interrupted -> completed` 被刻意禁止**：中断后若允许直接置成功，就无法保证
  「中断模型流时不会产生最终成功事件」。恢复必须显式走 `interrupted -> running`。
- 表外迁移一律抛 `IllegalTurnTransition`。

## 4. 事件模型

```jsonc
{
  "contract": "agent.v2.contract.v1",
  "event_id": "ev_...",
  "sequence": 42,              // 线程内单调递增、不跳号
  "type": "tool/started",      // 见 EventType（49 个取值）
  "created_at": "2026-10-09T07:36:47.123Z",
  "thread_id": "th_...",       // 必填：任何事件都能回溯到线程
  "turn_id": "tu_...",         // 可选
  "item_id": "it_...",         // 可选
  "call_id": "call_...",       // 可选：工具事件靠它配对
  "idempotency_key": null,     // 非空则可幂等重复提交
  "payload": {}
}
```

落盘布局：

```
knowledge-base/conversations/<可读名称-id>/events.jsonl
knowledge-base/conversations/<可读名称-id>/snapshots/<sequence>.json
knowledge-base/memories/<scope>s/<scope_id>/<memory_id>.json
```

约定：**数据库只保存索引、状态与租约**，不得再存一份互相竞争的 Agent 历史。

## 5. 模型流（供应商无关）

`text_delta` / `reasoning_delta` / `tool_call_delta` / `tool_call_completed` /
`usage` / `completed` / `error`。

`error.error_class` 是封闭四值：`retryable`、`context_overflow`、`invalid_request`、`fatal`。

**供应商差异只能收敛在 provider 适配器内部**：适配器负责把自家异常翻译成
`StreamError(error_class=...)`。TurnRuntime 中不得出现任何供应商专有分支
（由 `test_model_loop.py::test_turn_runtime_has_no_vendor_specific_branches` 静态守卫）。

## 6. ToolCall 统一结构

一个结构同时承载请求 / 成功 / 失败 / 超时 / 取消 / 参数非法：

- `kind=read_only` → 允许并行；
- `kind=side_effect` → 必须串行；
- 成功态禁止带 `error`，`failed|timeout|invalid_arguments` 必须带 `error`（构造期即断言）。

## 7. 破坏性变更政策

以下操作**必须**提升契约版本并重新冻结：

- 删除字段；
- 改变字段含义；
- 枚举重命名或语义改变；
- 把可选字段改成必填。

新增**可选**字段属于非破坏性变更，但仍需在变更说明里登记。
`test_contracts.py` 会比对「schema 枚举 ↔ Python 枚举」零漂移，任何一侧漏改都会失败。
