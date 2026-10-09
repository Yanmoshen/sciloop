# Agent 2 交付报告：工具、权限、沙箱与宿主机执行

## 1. 基线与隔离

| 项 | 值 |
|---|---|
| 分支 | `codex/agent-tools` |
| worktree | `D:\aicoding竞赛-worktrees\agent-tools` |
| 契约冻结标签 | `agent-v2-contract-v1`（`6a9fa00`） |
| 实现基线 | `codex/agent-runtime` 的 `4d7bec5`（契约标签是其祖先，已用 `git merge-base --is-ancestor` 校验） |
| 并行说明 | **本线未等待也未依赖 Agent 3**；全部后端用本目录 fake 注入，测试离线可跑 |

未修改：`server/contracts/agent_v2/`（只读）、`server/services/{agent_runtime_v2,agent_threads_v2,model_gateway_v2}/`、
`server/api/v1/`、`web/`、`server/main.py`、旧 `mcp_tools.py`、旧 `policy.py`。
**未删除或迁移任何对话与知识库数据**（本线只写临时目录与测试目录）。

## 2. 交付物

```text
server/services/tool_registry_v2/      工具声明、校验、执行边界、事件转换、旧→新映射
    definition.py / registry.py / events.py / mapping.py
    builtin/{host_tools,service_tools}.py
server/services/approval_v2/           风险规则、审批状态机、持续批准、令牌
    risk.py / manager.py / grants.py
server/services/sandbox_v2/            策略、路径边界、命令参数路径检查
    policy.py / paths.py / manager.py
server/services/host_execution_v2/     进程生命周期、进程树终止、重启扫描、幂等
    models.py / runner.py / manager.py
server/services/agent_tree_tools_v2/   子 Agent 工具入口（spawn/send/wait/interrupt/close）
server/tests/agent_v2/tools/           61 个用例 + Windows 探针
```

## 3. 工具清单与权限矩阵

| 工具 | 类别 | 并行 | 幂等键 | 超时 |
|---|---|---|---|---|
| `host.file.list` / `host.file.read` | read_only | 是 | 无 | 60s |
| `web.search` / `web.fetch` | read_only | 是 | 无 | 30s |
| `kb.query` / `skill.load` | read_only | 是 | 无 | 60s |
| `host.file.write` / `host.file.move` | workspace_write | 否 | call_id | 60s |
| `kb.write` | workspace_write | 否 | call_id | 60s |
| `host.exec` | execution | 否 | thread+turn+call | ≤600s |
| `skill.run` / `mcp.call` | execution | 否 | thread+turn+call | 600s / 120s |
| `spawn_agent` / `send_message` / `wait_agent` / `interrupt_agent` / `close_agent` | execution | 否 | call_id | 30s（wait 120s） |
| `host.file.delete` | **high_risk** | 否 | call_id | 60s |

旧 → 新映射（集成期口径）：`query_library→kb.query`、`search_academic|search_web→web.search`、
`fetch_url→web.fetch`、`load_skill→skill.load`、`run_command|run_on_computer→host.exec`、
`files_on_computer→host.file.{list,read,write,move,delete}`；新增 `kb.write`、`mcp.call` 与五个子 Agent 工具。
`files_on_computer` 原有的 `copy` 语义由 `host.file.read + host.file.write` 组合表达。

## 4. 沙箱与审批口径

- `workspace-write`（默认与测试基线）：工作区内读写执行；工作区外**只读可用、写入要授权**；
  凭据 / 版本库元数据（`.env`、`.git`、`*.pem` …）**写保护**、读取要授权；
- `danger-full-access`：任意目录放行 + 命令自动批准，但**超时、取消、输出上限、审计一律保留**；
- `read-only` 仅作显式兼容能力，不是默认测试模式；
- 路径先 `realpath` 规范化再判根边界 → **符号链接无法绕过**；
- **命令参数里的路径也检查**：写命令（`mkdir`/`rm`/`cp`/`New-Item` …）的参数按写入判定，
  重定向目标（`> out.txt`）按写入判定，其余按读取；
- 审批：同 `call_id` 只有一个最终结论；「批准一次」只释放当前 Call；
  「当前对话始终批准」保存 **规范化 argv + cwd + 会话作用域**，任一变化即不命中；
  拒绝/取消/过期都会收敛 PENDING；**审批令牌只在服务端流通，模型可见参数里没有它**。

## 5. 测试命令与结果

```bash
# 容器（权威环境：Python 3.11 + jsonschema 4.26 + pytest 8.4 + pytest-asyncio）
docker run --rm -v "D:/aicoding竞赛-worktrees/agent-tools:/src" -w /src/server \
  sciloop-backend:latest python -m pytest tests/agent_v2/tools -q

# 整个 agent.v2 套件（含 Agent 1 的运行时用例，确认互不干扰）
docker run --rm -v "D:/aicoding竞赛-worktrees/agent-tools:/src" -w /src/server \
  sciloop-backend:latest python -m pytest tests/agent_v2 -q

# ruff（镜像内 0.16.8）
docker run --rm -v "D:/aicoding竞赛-worktrees/agent-tools/server:/src" -w /src \
  sciloop-backend:latest ruff check services/tool_registry_v2 services/approval_v2 \
  services/sandbox_v2 services/host_execution_v2 services/agent_tree_tools_v2 tests/agent_v2/tools

# Windows 真机探针（Windows 进程树终止的验收证据）
python server/tests/agent_v2/tools/windows_probe.py
```

结果：

- `tests/agent_v2/tools`：**60 passed / 1 skipped**（skipped = Windows 专属用例，在 Linux 容器里跳过）
- `tests/agent_v2`（含 Agent 1）：**165 passed / 1 skipped / 共 166**
- ruff：`All checks passed!`
- Windows 探针：`result: pass`，日志见下

### 5.1 Windows 进程树终止日志（探针实测）

```json
{
  "powershell": { "status": "succeeded", "stdout": "probe-ok", "exit_code": 0 },
  "timeout_kill": {
    "status": "timeout",
    "kill_evidence": {
      "method": "taskkill", "ok": true,
      "stdout": "成功: 已终止 PID 30284 (属于 PID 28900 子进程)的进程。\n成功: 已终止 PID 28900 (属于 PID 35532 子进程)的进程。"
    },
    "child_pid": 30284, "child_alive_after_kill": false, "heartbeat_stopped": true
  },
  "interrupt_kill": { "status": "interrupted", "child_alive_after_kill": false, "heartbeat_stopped": true },
  "restart_scan": { "scanned_status": "unknown", "replayed": true, "replay_status": "unknown" }
}
```

`heartbeat_stopped` = 孙进程每 0.15s 改写的心跳文件在终止后不再变化；`child_alive_after_kill` = 用
`taskkill /T` / `killpg` 后孙进程确实消失。两条独立证据都成立才判 pass。

### 5.2 越界路径日志（节选）

```
命令参数越界（写命令）  : mkdir <工作区外>/x      → require_approval
只读命令 + 越界参数     : cat <工作区外>/x        → allow（工作区外只读）
重定向目标越界         : echo hi > <工作区外>/a   → require_approval
凭据写保护             : <工作区>/.env 写        → deny
版本库元数据           : <工作区>/.git/config 写  → deny
软链越界               : 工作区/link → 区外目录   → require_approval（路径解析为区外真实路径）
```

### 5.3 审批事件样例

```json
{
  "approval_id": "ap_...", "thread_id": "th_...", "turn_id": "tu_...", "call_id": "call_...",
  "status": "pending", "risk": "high_risk",
  "action": {
    "tool": "host.exec", "argv": ["rm", "-rf", "/ws/victim.txt"], "cwd": "/ws",
    "executable": "rm", "targets": [],
    "risk_categories": ["delete"], "thread_id": "th_..."
  }
}
```

拒绝后重跑：`/ws/victim.txt` **仍然存在**（无实际副作用），审批状态 `denied`。

## 6. 已知限制

1. 命令参数路径识别是**启发式**（写命令表 + 重定向 + 路径外形）：把路径拼进
   `python -c "open('/etc/passwd','w')"` 这类字符串里不会被静态识别——那是沙箱之外的事，
   需要进程级隔离（本任务包范围外）。
2. `pid_alive` 在无 `/proc` 的容器里回退到 `os.kill(pid, 0)`，此时僵尸进程会被判为"存活"。
3. MCP 桥接当前提供通用 `mcp.call`（类别 execution）；按工具逐个桥接时应改为继承该工具权限类别。
4. 依赖注入型工具（搜索 / 抓取 / 知识库 / 技能 / MCP）在未注入后端时返回
   `backend_unavailable` 结构化错误——这是刻意的：不伪造成功、不伪造网络调用。

## 7. 数据变更

无。本线未写入 `knowledge-base/`，未删除任何对话，未执行任何真实删除/安装/系统修改测试
（全部使用临时目录与 fake executor）。
