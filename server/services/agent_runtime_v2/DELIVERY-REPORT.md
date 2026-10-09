# Agent 1 交付报告：核心运行时 / 系统提示词 / 研究节点 / 上下文

日期：2026-10-09　执行范围：本文件对应 `docs/Agent1-核心运行时与系统提示词-计划验收.md`
本轮选定策略（用户裁决）：**只做与事件命名无关的部分**；事件命名冲突等与 Agent 3 协调后再动。

## 1. 隔离与基线

| 项 | 值 |
|---|---|
| 分支 | `codex/agent-runtime` |
| worktree | `D:\aicoding竞赛-worktrees\agent-runtime` |
| 契约标签基线 | `agent-v2-contract-v1` = `6a9fa00` |
| 本线起始实现 | `4d7bec5`（沿用既有运行时实现，按计划书 §2.1 要求在此注明） |
| 本轮提交 | 见 `git log -1`（分支已推送 `origin/codex/agent-runtime`） |
| 契约版本 | `agent.v2.contract.v1`（**未升版本**） |

主工作区那 5 个用户未提交文件（`server/api/v1/chat.py`、`server/services/conversations.py`、
`server/services/research/dialog.py`、`web/src/views/HomeView.vue`、`web/src/views/KnowledgeBaseView.vue`）
**未被复制、覆盖、回滚或格式化**。

## 2. 本轮对「冻结契约目录」的唯一改动（需协调 Agent 确认）

`server/contracts/agent_v2/models.py`：新增两个**模块内私有**缓存
（`_type_hints` / `_declared_field_names`，`functools.cache`），供 `ContractModel.from_dict` 使用。

- **性质**：纯内部性能优化，**不新增/删除/改名任何字段、枚举、Schema 或公开签名**；
  磁盘格式与 `to_dict()` 输出逐字节不变。
- **证据**：149 条测试全通过；`git diff --stat server/contracts` 仅 `models.py | 19 +++-`。
- **原因**：事件重放会反序列化上千次对象，`typing.get_type_hints` 每次重新解析注解，
  是长对话重放的主要耗时（20 轮用例 49.6s → 8.06s）。
- **若协调方要求冻结目录零改动**：删掉这两个缓存函数、把 `_type_hints(cls)` 换回
  `get_type_hints(cls)` 即可，代价是测试变慢，功能不受影响。

## 3. 本轮新增 / 修改文件

**新增**
```
server/services/agent_prompt_v2/{__init__,blocks,instructions,context,renderer}.py   # WP-03
server/services/agent_runtime_v2/research.py                                          # WP-05 适配层
server/services/agent_compaction_v2/{recap,policy,service}.py                          # WP-06
server/services/agent_memory_v2/models.py                                              # WP-07 信封
server/tests/agent_v2/runtime/{test_prompt_v2,test_memory_v2,test_compaction_recap,test_twenty_rounds}.py
```
**修改**
```
server/services/agent_memory_v2/{store,__init__}.py      # 落盘改 records.jsonl
server/services/agent_compaction_v2/{compaction,__init__}.py  # 接入 recap 与策略
server/services/agent_events_v2/store.py                 # 解析缓存（按 size+mtime 失效）
server/services/agent_threads_v2/repository.py           # 工具事件不再重写派生缓存
server/services/agent_runtime_v2/__init__.py             # 导出研究节点
server/contracts/agent_v2/models.py                      # 见 §2
server/tests/agent_v2/runtime/test_compaction_memory.py  # 适配新记忆布局
```

**未触碰**：`server/api/v1/`、`server/api/v2/`、`web/`、`server/services/agent/*`、
`tool_registry_v2/`、`approval_v2/`、`sandbox_v2/`、`host_execution_v2/`、
`server/main.py`、`server/mcp_server/`、迁移、Docker 文件、依赖锁文件。

## 4. 测试与 lint 证据

本机 Docker Linux 引擎当时未运行，改用隔离 venv（与镜像同为「带官方 jsonschema」的权威口径）：

```bash
# 环境：Python 3.13.14 + pytest 9.1.1 + jsonschema 4.26 + ruff 0.16.8
python -m pytest server/tests/agent_v2 -q
# → tests=149 passed=149 failed=0 errors=0 skipped=0

python -m ruff check contracts services/agent_*_v2 services/agent_prompt_v2 tests/agent_v2
# → All checks passed!（新增目录零告警）
# 仓库其余 21 处告警属其他线的既有问题，未触碰
```

分文件通过数：contracts 27、prompt+研究门 20、memory 9、compaction+recap 13、
twenty_rounds 2、model_loop 13、tools 12、threads 12、event_store 10、agent_tree 11、
stability 8、compaction_memory 12。

## 5. 计划书自动化场景对照

| 场景（§6） | 状态 | 证据 |
|---|---|---|
| 20 轮自然语言对话，全部完成，无后台任务与活动锁 | ✅ | `test_twenty_rounds.py`（含一次中途压缩；断言无泄漏任务、无租约泄漏、序号连续） |
| 纯文本 / 单工具 / 多工具并行串行 / 失败 / 超时 / 取消 / 恢复 | ✅ | `test_tools.py`、`test_stability.py` |
| 研究意图先确认；未回答不会检索 | ✅ | `test_prompt_v2.py`（确认门 6 条） |
| Stop 后模型/工具/审批同时结束，事件可从最后序号恢复 | ⚠️ 部分 | 取消令牌已下发到模型与工具（`test_threads.py`）；**审批等待的取消**本轮未接线到 Agent 2 的 `ApprovalManager`（见 §7） |
| 事件幂等、游标补拉、坏事件报告、重启扫描 | ✅ | `test_event_store.py` |
| 压缩成功/失败/继续十轮/摘要编辑恢复 | ✅ | `test_compaction_memory.py`、`test_stability.py`、`test_compaction_recap.py` |
| 用户/项目/对话记忆隔离、用户编辑保护 | ✅ | `test_memory_v2.py` |
| 并发 Turn 拒绝、重复输入不重复执行 | ✅ | `test_threads.py`、`test_stability.py` |
| fake 执行器不连网、不写真实知识库、不起宿主命令 | ✅ | 全部测试用 `FakeProvider`/`FakeToolExecutor`/`tmp_path` |

### 20 轮对话日志摘要（形态轮换）

```
第 1/6/11/16 轮：双工具（只读并行 + 副作用串行）→ completed，2 iterations
第 2/7/12/17 轮：纯文本直接回答        → completed，1 iteration
第 3/8/13/18 轮：单工具                → completed，2 iterations
第 4/9/14/19 轮：工具失败回喂模型        → completed（失败不伪装成功、不中断循环）
第 5/10/15/20 轮：可重试错误→重试→完成   → completed，retries=1
第 10 轮后：手动压缩成功，摘要覆盖点写入事件；第 11..20 轮照常完成
终态：20 个 Turn 全部 completed，active_turn=None，序号连续，无租约/任务泄漏
```

### 压缩前后摘要（recap）

压缩完成事件现在携带结构化 `recap`：

```json
{"goal": "…", "decisions": [...], "authorizations": [...],
 "completed": [{"text": "…", "status": "tested"}],
 "in_progress": [{"text": "…", "status": "proposed"}],
 "blocked": [...], "errors": [...], "important_paths": [...],
 "next_steps": [...], "research_state": "…", "memory_refs": [...]}
```

硬约束已落地并有测试：状态标记只允许 `proposed/queued/implemented/tested/published/installed`；
**`proposed`/`queued` 出现在 `completed` 里会当场失败**（不允许把计划当完成）。
模型没给结构时回落原文，历史行为不变。

### 中断恢复日志

```
start_turn → running
token.cancel("user_interrupt") → 事件 turn/interrupted（无 turn/completed、无 model/completed）
租约释放（lease 文件 released=true）、held_lease_count()==0
state().turns[turn].status == interrupted；事务可继续开新 Turn
```

## 6. 关键设计决定与理由

1. **研究确认门用 v1 已有事件落地**：`input/requested` + `turn/waiting_input`，
   研究状态写在 Thread 设置的 `research` 键里。**不自造契约外事件名**
   （有测试守卫 `test_research_events_use_only_frozen_v1_names`），
   等命名轮统一改名。
2. **提示词分层 + 清洗**：`base instructions → runtime facts → task context → research context`
   固定顺序；`research context` 只在 `research_confirmed` 为真时出现；
   禁用键与含稳定 ID/traceback 的值一律丢弃（宁可少给）。
3. **记忆落盘改 append-only `records.jsonl`**：契约 `MemoryRecord` 是冻结的，
   所以「创建者/置信度/内容哈希/修订号」放在**存储信封**里，对外仍返回契约对象，
   Agent 3 的调用面不变。
4. **策略与执行解耦**：`CompactionPolicy` 只回答「该不该压、为什么」，
   完成事件带 `trigger_policy` 载荷，压缩原因可审计。

## 7. 未做项与阻塞项（诚实清单）

| 项 | 状态 | 说明 |
|---|---|---|
| WP-01 新事件名（`assistant/delta`、`tool/call_started`、`memory/recorded`、`turn/running`、`thread/settings_updated`…） | 🔴 **阻塞** | 与已冻结、且被 Agent 2/3 + 前端 reducer 消费的 v1 名冲突。改名需契约 v2 + 同步 A3 前端。**已请用户裁决，本轮按「先做不受影响部分」执行** |
| WP-04 `cancelled` 错误类 | 🔴 延后 | 属新增枚举值，同样需要契约版本决策，与该轮命名一并处理 |
| WP-01 `compacting` Turn 状态 | 🔴 延后 | 同上 |
| WP-04 preamble / 工具事件与正文分离的**路由接线** | ⚠️ 未接线 | 提示词侧已就绪；`server/api/v1/chat.py` 属禁止修改，需协调 Agent 落地或改由 Agent 3 的 v2 路由承载（§2.3 的适配接口条款） |
| WP-05 旧 `research/*` 的替换 | ⚠️ 部分 | 新适配层已提供；旧 `services/research/dialog.py` 在用户未提交文件里，不得触碰 |
| 审批等待的取消（Stop 同时收束模型/工具/审批） | ⚠️ 部分 | 工具与模型已连通取消令牌；审批挂起是**领域状态**，需要 Agent 2 `ApprovalManager.cancel` 协同，当前由 `AgentTree.cancel`/`repo.release_turn` 保证锁不残留 |
| 真实供应商适配器 | ❌ 未实现 | 仅接口桩 + 错误分类钩子（计划书明确不属于本线） |
| coverage 命令 | ⚠️ 未跑 | venv 未装 `pytest-cov`；Docker 恢复后可按 §6 的命令补跑 |

## 8. 数据变更说明

- **未写入任何真实 `knowledge-base/` 数据**：全部测试使用 `tmp_path`。
- 记忆落盘格式变了（`<scope>s/<id>/<id>.json` → `<scope>/<id>/records.jsonl`），
  但本 worktree 里没有真实记忆数据；若主工作区已有旧格式记忆文件，迁移需要单独一步
  （读取旧目录 → 写成 records.jsonl），本轮**未做迁移**，仅提供新写入路径。
- 事件文件格式未变。

## 9. 性能记录（本轮顺带修掉的真实瓶颈）

| 场景 | 优化前 | 优化后 |
|---|---|---|
| 20 轮对话（含压缩） | 49.56s | **8.06s** |
| 压缩后继续 10 轮多工具 | 29.07s | **6.90s** |
| 20 轮后重启一致 | 13.88s | **4.16s** |

三处改动：①事件解析结果按 `(size, mtime_ns)` 缓存，文件未变不重复解析；
②`get_type_hints` 结果缓存（重放的主要耗时）；③工具事件不再重写派生缓存 `thread.json`。

**仍存在的风险**：`ThreadState` 每次操作都全量重放（O(n)），事件数到万级时需要改为
增量状态机 + 定期快照。已在测试里以「20 轮」为规模上限，未压测万级事件。
