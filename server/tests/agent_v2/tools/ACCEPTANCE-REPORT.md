# Agent 2 交付报告：Codex 风格权限、审批、沙箱与宿主执行

配套文档：《Agent2-权限审批沙箱与宿主执行-计划验收.md》　版本：2026-10-09

## 1. 基线与隔离

| 项 | 值 |
|---|---|
| 分支 | `codex/agent-tools` |
| worktree | `D:\aicoding竞赛-worktrees\agent-tools` |
| 契约冻结标签 | `agent-v2-contract-v1`（`6a9fa00`，只读未改） |
| 实现基线 | `codex/agent-runtime` 的 `4d7bec5`（契约标签为其祖先，已校验） |
| 交付 commit | `a240d7c`（本轮对齐）← `3518bbb`（首轮交付） |
| 与 Agent 3 的关系 | **不等待、不依赖**：搜索/知识库/技能/MCP 全部靠 `ToolRegistry.bind(...)` 注入，未注入返回 `backend_unavailable` 结构化错误，测试全程离线 |

`git diff --name-only agent-v2-contract-v1...HEAD` 命中范围：`server/services/{tool_registry_v2,approval_v2,sandbox_v2,host_execution_v2,agent_tree_tools_v2}/`、`server/tests/agent_v2/tools/`。
未修改：契约目录、Agent 1 运行时/线程/网关/压缩/记忆/提示词、`api/v1`、`api/v2`、`web/`、`main.py`、`mcp_server/`、旧 `agent/{policy,approvals,sandbox,host_runner}.py`、迁移与 Docker 文件。

## 2. 工具清单与权限矩阵

| 工具 | permission_class | side_effect | 并行 | 幂等 | 超时 | 输出上限 | 取消 |
|---|---|---|---|---|---|---|---|
| `host.command` | exec | process | 否 | thread+turn+call | ≤600s | 64KB | 是 |
| `host.file.list` / `host.file.read` | read | none | 是 | 无 | 60s | 64KB | 否 |
| `host.file.write` / `host.file.move` | workspace_write | filesystem | 否 | call_id | 60s | 64KB | 否 |
| `host.file.delete` | **dangerous** | filesystem | 否 | call_id | 60s | 64KB | 否 |
| `search.query` / `search.fetch` | read | network | 是 | 无 | 30s | 64KB | 是 |
| `knowledge.search` | read | none | 是 | 无 | 60s | 64KB | 否 |
| `knowledge.write` | workspace_write | filesystem | 否 | call_id | 60s | 64KB | 否 |
| `skill.list` | read | none | 是 | 无 | 60s | 64KB | 否 |
| `skill.run` | exec | process | 否 | thread+turn+call | 600s | 64KB | 是 |
| `mcp.call` | exec | external | 否 | call_id | 120s | 64KB | 是 |
| `agent.spawn` / `agent.send` / `agent.wait` / `agent.interrupt` / `agent.close` | exec | external | 否 | call_id | 30s（wait 120s） | 64KB | 是 |

每个工具都另带：`version`、`input_schema`/`output_schema`（Draft 2020-12，官方 `jsonschema` 校验）、
`audit_fields`。**只有「read + parallel_safe + 副作用为 none/network」才允许并行**，其余逐个串行。

旧 → 新映射：`query_library→knowledge.search`、`search_academic|search_web→search.query`、
`fetch_url→search.fetch`、`load_skill→skill.list`、`run_command|run_on_computer→host.command`、
`files_on_computer→host.file.{list,read,write,move,delete}`；新增 `knowledge.write`、`mcp.call`、
`agent.*` 五件套。

## 3. 测试命令与通过数

环境：宿主 Python 3.14（pytest 9.0.3 + pytest-asyncio + jsonschema 4.26）——**真 Windows**，
因此 Windows 专属用例真实执行而不是跳过。

```bash
# 工具线
python -m pytest tests/agent_v2/tools -q
# → 81 passed / 1 skipped（略过项：无法创建 symlink/junction 时的保护性 skip）

# 全量 agent.v2（含 Agent 1 运行时用例，确认互不干扰）
python -m pytest tests/agent_v2 -q
# → 186 passed / 1 skipped / 共 187

# 静态检查
ruff check services/tool_registry_v2 services/approval_v2 services/sandbox_v2 \
           services/host_execution_v2 services/agent_tree_tools_v2 tests/agent_v2/tools
# → All checks passed!（宿主机 ruff 0.15.13）

# Windows 进程树证据
python server/tests/agent_v2/tools/windows_probe.py
# → result: pass
```

分文件通过数：`test_wp_requirements` 21、`test_registry` 15、`test_approval` 11、
`test_host_execution` 11、`test_sandbox` 9(+1 skip)、`test_agent_tree_tools` 8、`test_mapping` 6。

## 4. Windows 进程树日志（探针实测，节选）

```json
"kill_evidence": {
  "steps": [
    { "phase": "soft", "method": "taskkill-soft", "returncode": 128,
      "stderr": "错误: 无法终止 PID 22508 (属于 PID 29608 子进程)的进程。\n原因: 只能强行终止这个进程(带 /F 选项)。" },
    { "phase": "hard", "method": "taskkill-force", "returncode": 0,
      "stdout": "成功: 已终止 PID 22508 (属于 PID 29608 子进程)的进程。\n成功: 已终止 PID 29608 (属于 PID 34872 子进程)的进程。" }
  ],
  "exited_after": "hard"
},
"child_pid": 22508, "child_alive_after_kill": false, "heartbeat_stopped": true
```

- 软终止真的被系统拒绝（需要 `/F`），随后硬终止成功 → **两段终止确实生效**，不是一次 SIGKILL；
- 孙进程（心跳写入者）在终止后**心跳停止**、`pid_alive` 为 false（僵尸被正确判死）；
- Turn 中断路径：`terminated=[ex_...]`、`status=interrupted`、孙进程同样停止。

## 5. 越界路径日志（节选）

```
命令参数越界（写命令）   : mkdir <工作区外>/x           → require_approval
只读 + 越界参数          : cat <工作区外>/x             → allow（工作区外只读）
重定向目标越界           : echo hi > <工作区外>/a       → require_approval
凭据写/读                : <工作区>/.env                → deny（规则 cred.env，带审计原因）
凭据读（其余规则）        : <工作区>/.ssh/id_rsa          → deny（规则 cred.id-rsa）
版本库元数据             : <工作区>/.git/config          → 写 deny；读 allow（git 需要读）
软链 / junction 越界     : <工作区>/escape → 区外目录     → require_approval，resolved_paths 指向区外
不存在目标               : <工作区>/新/深层/file.txt     → allow（按父目录 realpath 判定）
大小写/分隔符变体         : <工作区>/SUB//a.txt 等 4 种    → 全部 allow 且 inside_workspace=true
可疑内联代码             : python -c "open('x','w')..."  → require_approval（escalation 说明原因）
无害内联代码             : python -c "print(1)"          → allow
```

## 6. 审批事件样例

```json
{
  "approval_id": "ap_...", "thread_id": "th_...", "turn_id": "tu_...", "call_id": "call_...",
  "tool_name": "host.command", "status": "pending", "risk": "high_risk",
  "normalized_argv": ["rm", "-rf", "/ws/build"], "executable": "rm", "cwd": "/ws",
  "target_paths": [], "risk_categories": ["delete"], "requested_scope": "once",
  "created_at": "...", "expires_at": "..."     // created_at + 900s
}
```

- `approve_once`：只释放该 `call_id`（`released_call_id` 回执），不写持续规则；
- `approve_for_thread`：写入 `executable=rm` + `prefix=("rm","-rf")` + `arg_constraints=exact(argv)` +
  `cwd` + `thread`；**换参数 / 换目录 / 换会话都不命中**；`prefix` 模式下额外参数不得是路径；
- `deny`：工具不启动（文件仍在）；`cancel` / `expire`：pending 收敛；
- 重复裁决幂等：批后可拒不可改判，`decisions()` 里同一 `call_id` 只有一个结论；
- 令牌绑定 `thread/turn/call/工具`，一次性核销，审计输出打码，**从不进入模型可见面**。

## 7. 能力探测结果（如实报告，不伪造隔离）

```json
// 沙箱层（sandbox_v2.capability()）
{ "platform": "windows", "strong_isolation": false, "policy_layer": true,
  "mechanisms": ["policy-layer", "approval-gate", "taskkill-process-tree",
                 "acl-sandbox=unavailable", "job-object=unavailable"],
  "notes": ["未实现 ACL/AppContainer 级写限制：策略层拒绝与审批是唯一防线，被批准的命令在被批准后拥有该用户权限"] }

// 执行层（host_execution_v2.capability()）
{ "process_tree_kill": true, "soft_then_hard": true, "job_object": false, "acl_sandbox": false }
```

Linux 侧（`sandbox_v2/linux.py`）会额外探测 landlock 是否可用，但**不启用**，并明确标注 `landlock=available(unused)`。

## 8. 平台限制

1. 未实现系统级强隔离（Windows ACL/AppContainer、Linux landlock/seccomp/cgroup）：
   **策略层 + 审批是唯一防线**；被批准的命令以当前用户权限运行。
2. 参数路径识别是启发式（写命令表 + 重定向 + 路径外形）；把路径拼进 `python -c` 字符串里
   无法静态识别——这类**可疑内联代码会升级到审批**，但批准后仍靠运行时自律。
3. `pid_alive` 在无 `/proc` 的环境回退到 `os.kill(pid, 0)`，此时僵尸进程会被判为存活。
4. `mcp.call` 目前是通用入口（类别 exec）；按工具逐个桥接时应改为继承该工具的权限类别。
5. 依赖注入型工具（搜索/抓取/知识库/技能/MCP）未注入后端时返回 `backend_unavailable`。

## 9. 数据变更

**无**。未写入 `knowledge-base/`、未删除任何对话、未执行真实删除/安装/系统修改测试
（破坏性操作全部使用临时目录与 fake executor）。
